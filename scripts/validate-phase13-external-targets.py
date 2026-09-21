#!/usr/bin/env python3
"""Run an isolated Web/native external-development Slice 2 proof."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.web.app import create_app

PASSWORD = "phase13-external-proof-admin"
ORIGIN = {"Origin": "http://127.0.0.1"}
PROTOCOL = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "phase13-proof",
        "version": "1",
    },
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class _KodiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    methods: ClassVar[list[str]] = []
    mutations: ClassVar[int] = 0

    def do_POST(self) -> None:
        _require(self.path == "/jsonrpc", "verifier used a non-canonical path")
        expected = "Basic " + base64.b64encode(b"kodi:proof-secret").decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        method = request["method"]
        self.methods.append(method)
        if method == "JSONRPC.Ping":
            result: object = "pong"
        elif method == "JSONRPC.Version":
            result = {"version": {"major": 13, "minor": 0, "patch": 0}}
        elif method == "Application.GetProperties":
            _require(
                request.get("params") == {"properties": ["name", "version"]},
                "verifier requested unexpected properties",
            )
            result = {
                "name": "Kodi",
                "version": {
                    "major": 21,
                    "minor": 3,
                    "tag": "stable",
                    "tagversion": "",
                },
            }
        elif method == "Addons.GetAddons":
            result = {
                "addons": [
                    {
                        "addonid": "plugin.video.proof",
                        "name": "Proof",
                        "version": "1.0.0",
                        "enabled": True,
                        "type": "xbmc.python.pluginsource",
                    }
                ],
                "limits": {"start": 0, "end": 1, "total": 1},
            }
        elif method == "JSONRPC.Introspect":
            requested = request["params"]["filter"]["id"]
            result = {"methods": {requested: {"type": "method"}}}
        elif method == "Input.Home":
            type(self).mutations += 1
            result = "OK"
        else:
            raise RuntimeError("verifier called an unapproved JSON-RPC method")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _mcp_call(
    client: TestClient, token: str, name: str, arguments: dict[str, object]
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL,
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": META},
        },
        follow_redirects=False,
    )
    _require(response.status_code == 200, "MCP call did not return a bounded result")
    return cast(dict[str, Any], response.json())


def main() -> None:
    _KodiHandler.methods = []
    _KodiHandler.mutations = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KodiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="kodi-lab-phase13-") as temporary:
            root = Path(temporary)
            data_root = root / "data"
            development_root = root / "development"
            data_root.mkdir()
            development_root.mkdir()
            engine = create_engine(f"sqlite:///{root / 'proof.db'}")
            Base.metadata.create_all(engine)
            factory = create_session_factory(engine)
            auth = WebAuthenticationService(factory)
            auth.set_password(PASSWORD)
            encoded_key = base64.urlsafe_b64encode(b"p" * 32).rstrip(b"=").decode()
            settings = Settings(
                data_root=data_root,
                development_root=development_root,
                external_target_master_key=encoded_key,
                external_target_allowed_ports=(server.server_port,),
                external_target_test_loopback_enabled=True,
            )
            proxy_names = (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
            )
            prior_proxy = {name: os.environ.get(name) for name in proxy_names}
            for name in proxy_names:
                if name.lower() == "no_proxy":
                    os.environ[name] = ""
                else:
                    os.environ[name] = "http://127.0.0.1:1"
            try:
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=cast(Any, object()),
                    auth=auth,
                )
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    login = client.post(
                        "/login",
                        data={"password": PASSWORD},
                        headers=ORIGIN,
                        follow_redirects=False,
                    )
                    _require(login.status_code == 303, "Web login failed")
                    page = client.get("/external-targets")
                    match = re.search(r'data-csrf-token="([^"]+)"', page.text)
                    _require(match is not None, "CSRF token unavailable")
                    assert match is not None
                    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": match.group(1)}
                    registered_response = client.post(
                        "/api/external-targets",
                        headers=headers,
                        json={
                            "logical_name": "Disposable loopback Kodi",
                            "protocol_kind": "jsonrpc_http",
                            "host": "127.0.0.1",
                            "port": server.server_port,
                            "authentication_mode": "username_password",
                            "configured_capabilities": [
                                "target_inspect",
                                "addon_inventory",
                                "semantic_remote",
                            ],
                            "username": "kodi",
                            "password": "proof-secret",
                        },
                    )
                    _require(
                        registered_response.status_code == 201,
                        "registration failed",
                    )
                    registered = registered_response.json()
                    target_id = registered["target_id"]
                    _require(
                        registered["administrative_state"] == "disabled"
                        and registered["verification_state"] == "unverified",
                        "registration did not fail closed",
                    )
                    observed = client.post(
                        f"/api/external-targets/{target_id}/probe",
                        headers=headers,
                        json={},
                    ).json()
                    _require(
                        observed["kodi_name"] == "Kodi"
                        and observed["kodi_version"] == "21.3"
                        and observed["verification_state"] == "unverified",
                        "native probe evidence was incorrect or trusted early",
                    )
                    confirmed = client.post(
                        f"/api/external-targets/{target_id}/confirm",
                        headers=headers,
                        json={
                            "confirmed": True,
                            "observation_id": observed["pending_observation_id"],
                        },
                    ).json()
                    _require(
                        confirmed["verification_state"] == "verified",
                        "explicit confirmation failed",
                    )
                    enabled = client.post(
                        f"/api/external-targets/{target_id}/enabled",
                        headers=headers,
                        json={"enabled": True},
                    ).json()
                    _require(
                        enabled["administrative_state"] == "enabled"
                        and "password" not in enabled
                        and "secret_reference" not in enabled,
                        "safe enabled status projection failed",
                    )

                    context = cast(FastAPI, client.app).state.kodi_lab
                    administration = context.mcp_administration
                    principal = administration.create_client(
                        display_name="Phase 13 external MCP proof",
                        preset_id=ADDON_DEVELOPMENT_PRESET_ID,
                    )
                    administration.assign_target_preset(
                        UUID(target_id), ADDON_DEVELOPMENT_PRESET_ID
                    )
                    administration.grant_target(principal.id, UUID(target_id))
                    token = administration.issue_credential(principal.id).token
                    listed = _mcp_call(client, token, "target_list", {})
                    _require(
                        listed["result"]["structuredContent"]["targets"][0]["target_id"]
                        == target_id,
                        "granted external target was not discoverable",
                    )
                    status = _mcp_call(
                        client, token, "target_status", {"target_id": target_id}
                    )
                    _require(
                        status["result"].get("isError") is not True,
                        "external target status failed",
                    )
                    addons = _mcp_call(
                        client, token, "addon_list", {"target_id": target_id}
                    )
                    _require(
                        addons["result"]["structuredContent"]["addons"][0]["addon_id"]
                        == "plugin.video.proof",
                        "external add-on inventory failed",
                    )
                    remote_arguments = {
                        "request_id": "4f86bc6b-95d1-4e7d-b34b-1f7f76e820a1",
                        "target_id": target_id,
                        "action": "home",
                    }
                    first_remote = _mcp_call(
                        client, token, "remote_action", remote_arguments
                    )
                    replay_remote = _mcp_call(
                        client, token, "remote_action", remote_arguments
                    )
                    _require(
                        first_remote["result"]["structuredContent"]
                        == replay_remote["result"]["structuredContent"]
                        and _KodiHandler.mutations == 1,
                        "external remote replay failed: "
                        f"first={first_remote!r} replay={replay_remote!r} "
                        f"mutations={_KodiHandler.mutations}",
                    )
                    administration.remove_target_grant(principal.id, UUID(target_id))
                    denied = _mcp_call(
                        client, token, "target_status", {"target_id": target_id}
                    )
                    _require(
                        denied["result"].get("isError") is True,
                        "grant revocation did not deny immediately",
                    )
                    administration.grant_target(principal.id, UUID(target_id))

                    client.post(
                        f"/api/external-targets/{target_id}/enabled",
                        headers=headers,
                        json={"enabled": False},
                    )
                    rotated = client.post(
                        f"/api/external-targets/{target_id}/credentials",
                        headers=headers,
                        json={
                            "authentication_mode": "username_password",
                            "username": "kodi",
                            "password": "rotated-proof-secret",
                        },
                    ).json()
                    _require(
                        rotated["verification_state"] == "needs_reverification",
                        "credential rotation did not require re-verification",
                    )
                    deactivated = client.post(
                        f"/api/external-targets/{target_id}/deactivate",
                        headers=headers,
                        json={"confirmed": True},
                    ).json()
                    events = client.get(
                        f"/api/external-targets/{target_id}/events"
                    ).json()
                    _require(
                        deactivated["administrative_state"] == "deactivated"
                        and events[-1]["event_kind"] == "deactivated",
                        "deactivation did not preserve semantic history",
                    )
            finally:
                for name, value in prior_proxy.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            database_bytes = (root / "proof.db").read_bytes()
            _require(
                b"proof-secret" not in database_bytes,
                "plaintext credential entered SQLite",
            )
            secret_root = data_root / "secrets" / "external-targets"
            _require(
                secret_root.exists() and not tuple(secret_root.iterdir()),
                "deactivation did not retire the active secret",
            )
            _require(
                "JSONRPC.Introspect" in _KodiHandler.methods,
                "remote capability was not read-only verified",
            )
            _require(
                len(tuple(build_mcp_tool_registry().names())) == 15,
                "MCP catalog changed",
            )
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "target_id": target_id,
                        "kodi_version": "21.3",
                        "probe_method_count": len(_KodiHandler.methods),
                        "mcp_tool_count": 15,
                        "mcp_external_visible": True,
                        "remote_mutation_count": _KodiHandler.mutations,
                        "final_state": "deactivated",
                    },
                    sort_keys=True,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()

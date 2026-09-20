#!/usr/bin/env python3
"""Bounded live Kodi 21.3 proof for replay-safe MCP remote control."""

# ruff: noqa: E402 -- bytecode suppression precedes project imports.

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID, uuid4

sys.dont_write_bytecode = True

import httpx2
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.auth import WebAuthenticationService
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import (
    ObserveManagedKodiRuntimeResult,
    build_foundation_registry,
)
from kodi_lab.broker.provisioning import (
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.screenshots import SqliteScreenshotCaptureResolver
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.remote import BrowserRemoteAction, KodiBrowserRemoteAdapter
from kodi_lab.mcp.stdio_proxy import (
    ENDPOINT_ENVIRONMENT_VARIABLE,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app

PASSWORD = "phase11-mcp-remote-live-password"
ORIGIN = "http://127.0.0.1"
STDIO_EXECUTABLE = Path(sys.executable).with_name("kodi-lab-mcp-stdio")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    _require(spec is not None and spec.loader is not None, f"{name} import failed")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CountingAdapter:
    def __init__(self, delegate: KodiBrowserRemoteAdapter, actions: list[str]) -> None:
        self._delegate = delegate
        self._actions = actions

    def dispatch(self, action: BrowserRemoteAction, invocation_id: UUID) -> None:
        self._actions.append(action.value)
        self._delegate.dispatch(action, invocation_id)


class _CountingAdapterFactory:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def __call__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
    ) -> _CountingAdapter:
        return _CountingAdapter(
            KodiBrowserRemoteAdapter(observation, profile), self.actions
        )


@contextmanager
def _live_loopback_server(app: ASGIApp) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = cast(tuple[str, int], listener.getsockname())[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=False
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("loopback MCP server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        _require(not thread.is_alive(), "loopback MCP server did not stop")


@asynccontextmanager
async def _direct_client(endpoint: str, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
    ) as http:
        streams = streamable_http_client(
            endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="auto", cache=None) as client:
            yield client


def _latest_hash(client: TestClient, instance_id: UUID) -> str:
    response = client.get(f"/api/instances/{instance_id}/screenshots/latest")
    _require(response.status_code == 200, "latest screenshot unavailable")
    return hashlib.sha256(response.content).hexdigest()


async def _exercise_transports(
    *,
    endpoint: str,
    token: str,
    target_id: UUID,
    dispatches: list[str],
) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=str(STDIO_EXECUTABLE),
        env={
            TOKEN_ENVIRONMENT_VARIABLE: token,
            ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
        },
        cwd=Path.cwd(),
    )
    async with (
        _direct_client(endpoint, token) as direct,
        Client(parameters, mode="auto", cache=None) as stdio,
    ):
        tool_names = [item.name for item in (await direct.list_tools()).tools]
        _require(
            tool_names
            == [
                "addon_list",
                "remote_action",
                "screenshot_capture",
                "target_list",
                "target_status",
            ],
            "unexpected control-client catalog",
        )

        http_request_id = str(uuid4())
        http_arguments = {
            "request_id": http_request_id,
            "target_id": str(target_id),
            "action": "down",
        }
        http_first = await direct.call_tool("remote_action", http_arguments)
        _require(not http_first.is_error, "HTTP navigation action failed")
        _require(
            cast(dict[str, Any], http_first.structured_content)["screenshot_refresh"][
                "result"
            ]
            == "succeeded",
            "HTTP navigation did not refresh the durable screenshot",
        )
        after_http_count = len(dispatches)
        http_replay = await stdio.call_tool("remote_action", http_arguments)
        _require(not http_replay.is_error, "stdio replay failed")
        _require(
            http_first.structured_content == http_replay.structured_content,
            "HTTP to stdio replay result changed",
        )
        _require(
            len(dispatches) == after_http_count == 1,
            "HTTP to stdio replay dispatched more than once",
        )

        stdio_request_id = str(uuid4())
        stdio_arguments = {
            "request_id": stdio_request_id,
            "target_id": str(target_id),
            "action": "up",
        }
        stdio_first = await stdio.call_tool("remote_action", stdio_arguments)
        _require(not stdio_first.is_error, "stdio navigation action failed")
        _require(
            cast(dict[str, Any], stdio_first.structured_content)["screenshot_refresh"][
                "result"
            ]
            == "succeeded",
            "stdio navigation did not refresh the durable screenshot",
        )
        after_stdio_count = len(dispatches)
        stdio_replay = await direct.call_tool("remote_action", stdio_arguments)
        _require(not stdio_replay.is_error, "HTTP replay failed")
        _require(
            stdio_first.structured_content == stdio_replay.structured_content,
            "stdio to HTTP replay result changed",
        )
        _require(
            len(dispatches) == after_stdio_count == 2,
            "stdio to HTTP replay dispatched more than once",
        )

        player_request_id = str(uuid4())
        no_player = await direct.call_tool(
            "remote_action",
            {
                "request_id": player_request_id,
                "target_id": str(target_id),
                "action": "play_pause",
            },
        )
        _require(no_player.is_error, "idle playback unexpectedly succeeded")
        _require(
            cast(dict[str, object], no_player.structured_content).get("error")
            == "remote_no_active_player",
            "idle playback returned the wrong classification",
        )
        no_player_count = len(dispatches)
        no_player_replay = await stdio.call_tool(
            "remote_action",
            {
                "request_id": player_request_id,
                "target_id": str(target_id),
                "action": "play_pause",
            },
        )
        _require(no_player_replay.is_error, "no-player replay changed outcome")
        _require(
            len(dispatches) == no_player_count,
            "no-player replay entered the adapter twice",
        )

        return {
            "tool_catalog": tool_names,
            "http_first_stdio_replay": {
                "request_id": http_request_id,
                "invocation_id": cast(dict[str, object], http_first.structured_content)[
                    "invocation_id"
                ],
                "dispatch_count_after_pair": after_http_count,
                "stored_result_equal": True,
            },
            "stdio_first_http_replay": {
                "request_id": stdio_request_id,
                "invocation_id": cast(
                    dict[str, object], stdio_first.structured_content
                )["invocation_id"],
                "dispatch_count_after_pair": after_stdio_count,
                "stored_result_equal": True,
            },
            "no_player": {
                "classification": "remote_no_active_player",
                "duplicate_redispatched": False,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path("<project-root>-phase11-mcp-remote-evidence.json"),
    )
    arguments = parser.parse_args()
    _require(os.geteuid() == 0, "live validation must run as root")
    project = Path(__file__).resolve().parents[1]
    parity = _load(
        project / "scripts" / "validate-phase10-screenshot-parity.py", "parity"
    )
    helper = parity._phase8_helpers(project)
    helper.__dict__["PASSWORD"] = PASSWORD
    helper.__dict__["ORIGIN"] = ORIGIN
    protected_before = helper._protected_fingerprint()
    _require(parity._managed_network_count() == 0, "managed network was not empty")
    _require(
        protected_before["production_data_root_absent"]
        and protected_before["production_runtime_root_absent"],
        "production roots were present",
    )
    target_ids = {str(item["release"]): uuid4() for item in helper.RUNTIMES}
    target_id = target_ids["21.3"]
    engine = None
    evidence: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(
            prefix="kodi-lab-phase11-mcp-remote-", dir="/var/tmp"
        ) as temporary:
            root = Path(temporary)
            data_root = root / "data"
            (data_root / "database").mkdir(parents=True)
            runtime_root = root / "runtime"
            runtime_root.mkdir(mode=0o700)
            os.environ["KODI_LAB_DATA_ROOT"] = str(data_root)
            os.environ["KODI_LAB_RUNTIME_ROOT"] = str(runtime_root)
            settings = Settings()
            command.upgrade(Config(str(project / "alembic.ini")), "head")
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            parity._seed_exact_curated_runtimes(factory, target_ids, helper.RUNTIMES)
            adapter = DockerManagedContainerAdapter()
            registry = build_foundation_registry(
                adapter,
                provisioning_resolver=SqliteProvisioningResolver(factory),
                provisioning_provider=adapter,
                deprovisioning_provider=adapter,
                revision_resolver=SqliteManagedInstanceRevisionResolver(factory),
                runtime_observation_resolver=SqliteRuntimeObservationResolver(factory),
                runtime_observation_provider=adapter,
                screenshot_capture_resolver=SqliteScreenshotCaptureResolver(
                    factory, data_root=data_root
                ),
                screenshot_capture_provider=(
                    parity.DockerManagedScreenshotCaptureProvider()
                ),
            )
            counting = _CountingAdapterFactory()
            with helper._broker_server(root, registry) as broker:
                auth = WebAuthenticationService(factory)
                auth.set_password(PASSWORD)
                app = create_app(
                    settings=settings,
                    session_factory=factory,
                    broker=broker,
                    auth=auth,
                    browser_remote_adapter_factory=counting,
                )
                with TestClient(app, base_url=ORIGIN) as web:
                    headers = helper._login(web)
                    _require(
                        web.post(
                            f"/api/instances/{target_id}/provision",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "provision failed",
                    )
                    _require(
                        web.post(
                            f"/api/instances/{target_id}/start",
                            json={},
                            headers=headers,
                        ).status_code
                        == 200,
                        "start failed",
                    )
                    parity._wait_visual_ready(web, target_id)
                    baseline = web.post(
                        f"/api/instances/{target_id}/screenshots",
                        json={},
                        headers=headers,
                    )
                    _require(baseline.status_code == 200, "baseline capture failed")
                    baseline_hash = _latest_hash(web, target_id)
                    _require(
                        web.post(
                            "/jsonrpc",
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "Input.Down",
                            },
                            headers=headers,
                        ).status_code
                        == 404,
                        "raw JSON-RPC became reachable",
                    )

                    administration = cast(
                        McpAdministrationService,
                        app.state.kodi_lab.mcp_administration,
                    )
                    mcp_client = administration.create_client(
                        display_name="Phase 11 live control",
                        preset_id=FULL_LAB_CONTROL_PRESET_ID,
                    )
                    administration.assign_target_preset(
                        target_id, FULL_LAB_CONTROL_PRESET_ID
                    )
                    administration.grant_target(mcp_client.id, target_id)
                    issued = administration.issue_credential(mcp_client.id)
                    with _live_loopback_server(app) as endpoint:
                        transport = asyncio.run(
                            _exercise_transports(
                                endpoint=endpoint,
                                token=issued.token,
                                target_id=target_id,
                                dispatches=counting.actions,
                            )
                        )
                        navigation_hash = _latest_hash(web, target_id)
                        _require(
                            navigation_hash != baseline_hash,
                            "navigation did not change the visual hash",
                        )
                        administration.set_client_enabled(mcp_client.id, False)

                        async def denied_after_disable() -> bool:
                            async with httpx2.AsyncClient(
                                headers={
                                    "Authorization": f"Bearer {issued.token}",
                                    "Accept": "application/json, text/event-stream",
                                    "Content-Type": "application/json",
                                    "MCP-Protocol-Version": "2026-07-28",
                                    "Mcp-Method": "tools/call",
                                    "Mcp-Name": "remote_action",
                                },
                                follow_redirects=False,
                            ) as http:
                                response = await http.post(
                                    endpoint,
                                    json={
                                        "jsonrpc": "2.0",
                                        "id": 99,
                                        "method": "tools/call",
                                        "params": {
                                            "name": "remote_action",
                                            "arguments": {
                                                "request_id": str(uuid4()),
                                                "target_id": str(target_id),
                                                "action": "left",
                                            },
                                            "_meta": {
                                                "io.modelcontextprotocol/"
                                                "protocolVersion": "2026-07-28",
                                                "io.modelcontextprotocol/"
                                                "clientCapabilities": {},
                                                "io.modelcontextprotocol/clientInfo": {
                                                    "name": "live-proof",
                                                    "version": "1",
                                                },
                                            },
                                        },
                                    },
                                )
                            return response.status_code == 401

                        _require(
                            asyncio.run(denied_after_disable()),
                            "disabled client retained remote authority",
                        )
                    administration.revoke_credential(
                        mcp_client.id, issued.credential.id
                    )
                    administration.remove_target_grant(mcp_client.id, target_id)
                    queued = web.post(
                        f"/api/instances/{target_id}/delete",
                        json={"confirmed": True, "preserve_snapshots": True},
                        headers=headers,
                    )
                    _require(queued.status_code == 202, "cleanup queue failed")
                    deleted = parity._wait_delete(web, queued.json()["operation_id"])
                    _require(deleted["state"] == "succeeded", "cleanup failed")

            evidence = {
                "schema": "kodi-lab-phase11-mcp-remote-live-v1",
                "runtime": "Kodi 21.3 Current",
                "target_id": str(target_id),
                "transport_replay": transport,
                "adapter_dispatches": counting.actions,
                "adapter_dispatch_count": len(counting.actions),
                "navigation_refresh": {
                    "baseline_capture_id": baseline.json()["capture_id"],
                    "baseline_sha256": baseline_hash,
                    "final_sha256": navigation_hash,
                    "visual_hash_changed": True,
                    "refreshes_per_new_navigation": 1,
                    "refreshes_per_duplicate": 0,
                },
                "disabled_client_denied": True,
                "raw_jsonrpc_exposed": False,
            }
            engine.dispose()
            engine = None
    finally:
        parity._cleanup_created(list(target_ids.values()))
        if engine is not None:
            engine.dispose()

    _require(parity._managed_network_count() == 0, "managed network was not cleaned")
    protected_after = helper._protected_fingerprint()
    changed = sorted(
        key
        for key in protected_before
        if protected_before[key] != protected_after.get(key)
    )
    _require(not changed, f"protected categories changed: {changed!r}")
    evidence["protected_isolation"] = {
        "protected_fingerprints_unchanged": True,
        "production_roots_absent": True,
        "managed_resources_cleaned": True,
    }
    payload = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()
    arguments.evidence_path.write_bytes(payload)
    print(
        json.dumps(
            {
                "evidence_path": str(arguments.evidence_path),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

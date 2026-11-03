"""Real sockets separate LAN browsers from unchanged MCP authentication/grants."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kodi_lab.config import Settings
from kodi_lab.web import run
from kodi_lab.web.app import create_app
from tests.web import conftest
from tests.web.conftest import INSTANCE_ID, PASSWORD, UNRELATED_INSTANCE_ID
from tests.web.test_mcp import META, PROTOCOL, _administration, _authority


@pytest.fixture
def listeners(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[tuple[TestClient, httpx.Client, httpx.Client]]:
    # Reserve ephemeral sockets; main() receives these exact bound descriptors.
    with (
        socket.create_server(("127.0.0.1", 0)) as local,
        socket.create_server(("127.0.0.1", 0)) as lan,
    ):
        ports = (local.getsockname()[1], lan.getsockname()[1])
        settings: Settings | None = None

        def configured(**kwargs: Any) -> FastAPI:
            nonlocal settings
            settings = kwargs["settings"]
            assert settings is not None
            settings.web_access_mode = "lan-http"
            settings.web_port, settings.web_lan_port = ports
            return create_app(**kwargs)

        monkeypatch.setattr(conftest, "create_app", configured)
        fixture_client: TestClient = request.getfixturevalue("client")
        app = cast(FastAPI, fixture_client.app)
        assert settings is not None
        monkeypatch.setattr(run, "Settings", lambda **kw: settings)
        monkeypatch.setattr(run, "create_database_engine", lambda _: None)
        monkeypatch.setattr(run, "create_session_factory", lambda _: None)
        monkeypatch.setattr(run, "BrokerClient", lambda _: None)
        monkeypatch.setattr(run, "create_app", lambda **kw: app)
        monkeypatch.setattr(
            socket,
            "create_server",
            lambda address: {ports[0]: local, ports[1]: lan}[address[1]],
        )
        config_factory = uvicorn.Config

        def config(application: Any, **options: Any) -> uvicorn.Config:
            # TestClient owns the real application lifespan, as in stdio tests.
            # Even deliberately trusting all forwarded peers cannot change the
            # kernel destination port or bypass the LAN listener boundary.
            return config_factory(
                application,
                **{
                    **options,
                    "log_config": None,
                    "access_log": False,
                    "lifespan": "off",
                    "forwarded_allow_ips": "*",
                    "timeout_graceful_shutdown": 1,
                },
            )

        monkeypatch.setattr(uvicorn, "Config", config)
        servers: list[uvicorn.Server] = []
        original_run = uvicorn.Server.run

        def capture(server: uvicorn.Server, **kwargs: Any) -> None:
            servers.append(server)
            original_run(server, **kwargs)

        monkeypatch.setattr(uvicorn.Server, "run", capture)
        thread = threading.Thread(target=run.main)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not servers or not servers[0].started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            with (
                httpx.Client(
                    base_url=f"http://127.0.0.1:{ports[0]}", trust_env=False
                ) as local_client,
                httpx.Client(
                    base_url=f"http://127.0.0.1:{ports[1]}", trust_env=False
                ) as lan_client,
            ):
                yield fixture_client, local_client, lan_client
        finally:
            if servers:
                servers[0].should_exit = True
            thread.join(5)
            assert not thread.is_alive()


def call(
    client: httpx.Client,
    token: str = "",
    method: str = "tools/list",
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    payload = {"_meta": META, **(params or {})}
    return client.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": PROTOCOL,
            "Mcp-Method": method,
            **({"Mcp-Name": str(payload["name"])} if method == "tools/call" else {}),
            "Accept": "application/json, text/event-stream",
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **(headers or {}),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": payload},
    )


def test_local_mcp_auth_permissions_and_target_grants_survive_lan_mode(
    listeners: tuple[TestClient, httpx.Client, httpx.Client],
) -> None:
    fixture, local, _ = listeners
    principal, token = _authority(fixture)
    for credential in ("", "invalid"):
        denied = call(local, credential)
        assert (
            denied.status_code == 401 and denied.headers["WWW-Authenticate"] == "Bearer"
        )
    tools = call(local, token)
    assert tools.status_code == 200
    assert "target_status" in {tool["name"] for tool in tools.json()["result"]["tools"]}
    result = call(local, token, "tools/call", {"name": "target_list", "arguments": {}})
    targets = result.json()["result"]["structuredContent"]["targets"]
    assert [item["target_id"] for item in targets] == [str(INSTANCE_ID)]
    allowed = call(
        local,
        token,
        "tools/call",
        {"name": "target_status", "arguments": {"target_id": str(INSTANCE_ID)}},
    )
    assert allowed.status_code == 200
    assert allowed.json()["result"]["structuredContent"]["target"]["target_id"] == str(
        INSTANCE_ID
    )
    neighbor = call(
        local,
        token,
        "tools/call",
        {
            "name": "target_status",
            "arguments": {"target_id": str(UNRELATED_INSTANCE_ID)},
        },
    )
    assert neighbor.json()["result"]["isError"] is True
    forbidden = call(
        local,
        token,
        "tools/call",
        {
            "name": "remote_action",
            "arguments": {
                "target_id": str(INSTANCE_ID),
                "action": "home",
                "request_id": str(uuid4()),
            },
        },
    )
    assert forbidden.json()["result"]["isError"] is True
    assert forbidden.json()["result"]["content"][0]["text"] == "permission_denied"
    admin = _administration(fixture)
    admin.remove_target_grant(principal, INSTANCE_ID)
    assert (
        call(
            local, token, "tools/call", {"name": "target_list", "arguments": {}}
        ).json()["result"]["structuredContent"]["targets"]
        == []
    )
    admin.set_client_enabled(principal, False)
    assert call(local, token).status_code == 401


def test_lan_listener_denies_mcp_even_with_token_and_forged_headers(
    listeners: tuple[TestClient, httpx.Client, httpx.Client],
) -> None:
    fixture, local, lan = listeners
    _, token = _authority(fixture)
    assert call(local, token).status_code == 200
    assert lan.get("/login").status_code == 200
    signed_in = lan.post(
        "/login",
        data={"password": PASSWORD},
        headers={"Origin": str(lan.base_url).rstrip("/")},
    )
    assert (
        signed_in.status_code == 303
        and "secure" not in signed_in.headers["set-cookie"].lower()
    )
    assert lan.get("/kodi").status_code == 200
    for forged in (
        {},
        {"Host": "127.0.0.1:18100"},
        {"Origin": "http://127.0.0.1:18100"},
        {
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Port": "8100",
            "X-Forwarded-Host": "127.0.0.1:18100",
            "Forwarded": "for=127.0.0.1;host=127.0.0.1:18100;proto=https",
        },
    ):
        denied = call(lan, token, headers=forged)
        assert denied.status_code == 403
        assert denied.json() == {"detail": "mcp_requires_local_listener"}
    for method in ("GET", "HEAD", "OPTIONS", "DELETE", "PUT", "PATCH"):
        assert (
            lan.request(
                method, "/mcp", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 403
        )
    assert call(local, token).status_code == 200


def test_lan_mode_rejects_collapsed_listener_ports() -> None:
    with pytest.raises(ValidationError, match="distinct listener ports"):
        Settings(web_access_mode="lan-http", web_port=8100, web_lan_port=8100)

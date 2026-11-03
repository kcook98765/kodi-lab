"""Explicit HTTP modes retain browser authority and isolate LAN MCP."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kodi_lab.config import Settings
from kodi_lab.web.app import create_app
from tests.web import conftest
from tests.web.conftest import INSTANCE_ID, PASSWORD
from tests.web.test_novnc import viewer
from tests.web.view_attempt_fixture import view_attempt


@pytest.fixture(params=["local-http", "lan-http"])
def http_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    mode = request.param
    host = "192.168.1.20" if mode == "lan-http" else "127.0.0.1"

    def configured(**kwargs: Any) -> Any:
        kwargs["settings"].web_access_mode = mode
        kwargs["settings"].web_allowed_hosts = (host,)
        service = viewer()
        service.require_https = kwargs["settings"].web_requires_https
        service.enabled_instances = frozenset({INSTANCE_ID})
        kwargs["novnc_viewer"] = service
        return create_app(**kwargs)

    monkeypatch.setattr(conftest, "create_app", configured)
    client: TestClient = request.getfixturevalue("client")
    client.base_url = f"http://{host}:18100"
    client.headers["Origin"] = str(client.base_url).rstrip("/")
    return client


def sign_in(client: TestClient) -> str:
    assert client.get("/login").status_code == 200
    response = client.post(
        "/login", data={"password": PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "domain=" not in cookie
    page = client.get("/")
    assert page.status_code == 200
    csrf = re.search(r'name="kodi-lab-csrf" content="([^"]+)"', page.text)
    assert csrf is not None
    return csrf.group(1)


def test_legacy_configuration_retains_https() -> None:
    assert Settings(_env_file=None).web_requires_https  # type: ignore[call-arg]
    assert not Settings(web_access_mode="local-http").web_requires_https


def test_http_login_csrf_origin_host_and_logout(http_client: TestClient) -> None:
    csrf = sign_in(http_client)
    assert http_client.post("/logout", data={}).status_code == 403
    assert http_client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert (
        http_client.post(
            "/logout", data={"csrf_token": csrf}, headers={"Origin": "http://evil.test"}
        ).status_code
        == 403
    )
    assert http_client.get("/", headers={"Host": "evil.test"}).status_code == 400
    response = http_client.post(
        "/logout", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert (
        response.status_code == 303
        and "secure" not in response.headers["set-cookie"].lower()
    )
    assert http_client.get("/", follow_redirects=False).status_code == 303


def test_forwarding_headers_cannot_change_http_origin(http_client: TestClient) -> None:
    result = http_client.post(
        "/login",
        data={"password": PASSWORD},
        headers={
            "Origin": str(http_client.base_url).replace("http:", "https:").rstrip("/"),
            "X-Forwarded-Proto": "https",
            "Forwarded": "proto=https",
        },
    )
    assert result.status_code == 403 and "set-cookie" not in result.headers


def test_mcp_exposure_is_independent_of_browser_login(http_client: TestClient) -> None:
    sign_in(http_client)
    del http_client.headers["Origin"]
    for host in (http_client.base_url.netloc.decode(), "127.0.0.1:18100"):
        reply = http_client.post(
            "/mcp",
            headers={
                "Host": host,
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        if http_client.base_url.host == "127.0.0.1":
            assert (
                reply.status_code == 401
                and reply.headers["WWW-Authenticate"] == "Bearer"
            )
        else:
            assert reply.status_code == 403
            assert reply.json() == {"detail": "mcp_requires_local_listener"}


def test_authorized_http_websocket_keeps_one_use_ticket(
    http_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.web import novnc

    csrf = sign_in(http_client)
    path = f"/api/instances/{INSTANCE_ID}"
    assert (
        http_client.post(
            path + "/novnc-start", json={"attempt": str(view_attempt()), "rate": 2}
        ).status_code
        == 403
    )
    result = http_client.post(
        path + "/novnc-start",
        json={"attempt": str(view_attempt()), "rate": 2},
        headers={"X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 200
    ticket = result.json()["ticket"]
    calls: list[Any] = []

    async def relay(
        websocket: Any, service: Any, item: Any, current: Any, authenticated: Any
    ) -> None:
        assert websocket.url.scheme == "ws" and authenticated()
        calls.append(item)
        await websocket.accept(subprotocol="novnc")
        await websocket.close()

    monkeypatch.setattr(novnc, "relay", relay)
    url = (
        str(http_client.base_url).replace("http:", "ws:").rstrip("/") + path + "/novnc"
    )
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        http_client.websocket_connect(
            url,
            headers={"Origin": "http://evil.test"},
            subprotocols=["novnc", "view." + ticket],
        ),
    ):
        pass
    assert denied.value.code == 4403 and not calls
    with http_client.websocket_connect(url, subprotocols=["novnc", "view." + ticket]):
        pass
    assert len(calls) == 1
    with (
        pytest.raises(WebSocketDisconnect) as replay,
        http_client.websocket_connect(url, subprotocols=["novnc", "view." + ticket]),
    ):
        pass
    assert replay.value.code == 4403 and len(calls) == 1

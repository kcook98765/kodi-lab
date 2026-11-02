"""Real Uvicorn trust middleware retains HTTPS, origin, CSRF and viewer authority."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from kodi_lab.web.app import create_app
from scripts import install
from tests.web import conftest
from tests.web.conftest import INSTANCE_ID, PASSWORD
from tests.web.test_novnc import viewer
from tests.web.view_attempt_fixture import view_attempt


# Reuse existing isolated viewer/broker doubles, never a live Kodi.
@pytest.fixture
def enabled_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    create = create_app

    def configured(**kwargs: Any) -> Any:
        service = viewer()
        service.require_https = True
        service.enabled_instances = frozenset({INSTANCE_ID})
        kwargs["novnc_viewer"] = service
        return create(**kwargs)

    monkeypatch.setattr(conftest, "create_app", configured)
    return request.getfixturevalue("client")  # type: ignore[no-any-return]


PEER = "192.168.241.2"
ORIGIN = "https://127.0.0.1:18443"


class PlainUpstream:
    """Model the plaintext Caddy-to-Uvicorn hop while the browser uses TLS cookies."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = {
                **scope,
                "scheme": "ws" if scope["type"] == "websocket" else "http",
            }
        await self.app(scope, receive, send)


def through_proxy(app: Any, peer: str = PEER) -> TestClient:
    return TestClient(
        PlainUpstream(ProxyHeadersMiddleware(app, trusted_hosts=PEER)),
        client=(peer, 32000),
        base_url=ORIGIN,
        headers={"X-Forwarded-Proto": "https", "Origin": ORIGIN},
        follow_redirects=False,
    )


@pytest.fixture
def proxy(client: TestClient) -> Iterator[TestClient]:
    proxied = through_proxy(client.app)
    yield proxied
    proxied.close()


def sign_in(proxy: TestClient) -> str:
    response = proxy.post("/login", data={"password": PASSWORD})
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/"):
        assert attribute in cookie
    assert "domain=" not in cookie
    match = re.search(r'name="kodi-lab-csrf" content="([^"]+)"', proxy.get("/").text)
    assert match is not None
    return match.group(1)


def test_locked_uvicorn_uses_the_exact_environment_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert uvicorn.__version__ == "0.53.0"
    configured = install.ingress_config("192.168.241.0/29", 18443, ["127.0.0.0/8"])
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", configured["caddy_ip"])
    assert uvicorn.Config(lambda: None).forwarded_allow_ips == PEER
    monkeypatch.delenv("FORWARDED_ALLOW_IPS")
    assert uvicorn.Config(lambda: None).forwarded_allow_ips == "127.0.0.1,::1"


@pytest.mark.parametrize(
    "peer",
    [
        PEER,
        "192.168.241.1",
        "192.168.241.3",
        "172.18.0.1",
        "10.250.0.10",
        "127.0.0.1",
        "::1",
        "192.168.241.4",
    ],
)
@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_only_exact_caddy_peer_can_forward_scheme_and_client(
    peer: str, kind: str
) -> None:
    observed: list[dict[str, Any]] = []

    async def record(scope: Any, receive: Any, send: Any) -> None:
        observed.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        else:
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close"})

    client = through_proxy(record, peer)
    if kind == "http":
        client.get("/", headers={"X-Forwarded-For": "203.0.113.9"})
    else:
        with client.websocket_connect(
            "wss://127.0.0.1:18443/", headers={"X-Forwarded-For": "203.0.113.9"}
        ):
            pass
    scope = observed[0]
    expected = (
        ("https" if kind == "http" else "wss")
        if peer == PEER
        else ("http" if kind == "http" else "ws")
    )
    assert scope["scheme"] == expected
    assert scope["client"][0] == ("203.0.113.9" if peer == PEER else peer)
    client.close()


def test_https_login_cookie_rotation_csrf_and_logout(proxy: TestClient) -> None:
    csrf = sign_in(proxy)
    first = dict(proxy.cookies)
    assert proxy.post("/logout", data={}).status_code == 403
    assert proxy.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert (
        proxy.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Origin": "http://127.0.0.1:18443"},
        ).status_code
        == 403
    )
    result = proxy.post("/logout", data={"csrf_token": csrf})
    assert result.status_code == 303
    assert "secure" in result.headers["set-cookie"].lower()
    assert proxy.get("/", follow_redirects=False).status_code == 303
    sign_in(proxy)
    assert dict(proxy.cookies) != first
    proxy.cookies.clear()
    proxy.cookies.update(first)
    assert proxy.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:18443",
        "https://evil.test:18443",
        "https://127.0.0.1:18444",
        "https://localhost:18443",
        "null",
        "",
    ],
)
def test_proxy_preserves_exact_login_origin(proxy: TestClient, origin: str) -> None:
    assert (
        proxy.post(
            "/login", data={"password": PASSWORD}, headers={"Origin": origin}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "peer", ["172.18.0.1", "192.168.241.1", "192.168.241.3", "127.0.0.1"]
)
def test_direct_http_cannot_spoof_https_login(client: TestClient, peer: str) -> None:
    direct = through_proxy(client.app, peer)
    assert direct.post("/login", data={"password": PASSWORD}).status_code == 403
    direct.close()


def test_anonymous_mcp_still_demands_bearer_authentication(proxy: TestClient) -> None:
    assert proxy.post("/mcp").status_code == 403
    del proxy.headers["Origin"]
    reply = proxy.post(
        "/mcp",
        headers={
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert reply.status_code == 401
    assert reply.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "proto", ["", "HTTPS", "https,http", "https, http", "ftp", "wss"]
)
def test_malformed_forwarded_scheme_does_not_authorize_login(
    proxy: TestClient, proto: str
) -> None:
    assert (
        proxy.post(
            "/login", data={"password": PASSWORD}, headers={"X-Forwarded-Proto": proto}
        ).status_code
        == 403
    )


def test_missing_origin_can_only_use_valid_referer(proxy: TestClient) -> None:
    del proxy.headers["Origin"]
    assert proxy.post("/login", data={"password": PASSWORD}).status_code == 403
    assert (
        proxy.post(
            "/login",
            data={"password": PASSWORD},
            headers={"Referer": ORIGIN + "/login"},
        ).status_code
        == 303
    )


@pytest.mark.parametrize(
    "origin,peer,query,protocols",
    [
        ("https://evil.test:18443", PEER, "", True),
        ("http://127.0.0.1:18443", PEER, "", True),
        ("https://127.0.0.1:18444", PEER, "", True),
        ("", PEER, "", True),
        (ORIGIN, "172.18.0.1", "", True),
        (ORIGIN, PEER, "?ticket=fake", True),
        (ORIGIN, PEER, "", False),
    ],
)
def test_wss_keeps_origin_transport_ticket_and_ownership_checks(
    enabled_client: TestClient, origin: str, peer: str, query: str, protocols: bool
) -> None:
    proxy = through_proxy(enabled_client.app)
    sign_in(proxy)
    # Changing the peer leaves the same real session cookie in the request.
    proxy._transport.client = (peer, 32000)  # type: ignore[attr-defined]
    with (
        pytest.raises(WebSocketDisconnect) as error,
        proxy.websocket_connect(
            f"wss://127.0.0.1:18443/api/instances/{INSTANCE_ID}/novnc{query}",
            headers={"Origin": origin},
            subprotocols=["novnc", "view.fake"] if protocols else [],
        ),
    ):
        pass
    assert error.value.code == 4403
    proxy.close()


def test_authorized_wss_reaches_mock_relay_once(
    enabled_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.web import novnc

    proxy = through_proxy(enabled_client.app)
    csrf = sign_in(proxy)
    path = f"/api/instances/{INSTANCE_ID}"
    result = proxy.post(
        path + "/novnc-start",
        json={"attempt": str(view_attempt()), "rate": 2},
        headers={"X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 200
    ticket = result.json()["ticket"]
    calls = []

    async def relay(
        websocket: Any, viewer: Any, item: Any, current: Any, authenticated: Any
    ) -> None:
        assert websocket.url.scheme == "wss" and authenticated()
        calls.append(item)
        await websocket.accept(subprotocol="novnc")
        await websocket.close()

    monkeypatch.setattr(novnc, "relay", relay)
    url = f"wss://127.0.0.1:18443{path}/novnc"
    with proxy.websocket_connect(url, subprotocols=["novnc", "view." + ticket]):
        pass
    assert len(calls) == 1
    with (
        pytest.raises(WebSocketDisconnect) as error,
        proxy.websocket_connect(url, subprotocols=["novnc", "view." + ticket]),
    ):
        pass
    assert error.value.code == 4403 and len(calls) == 1
    proxy.close()

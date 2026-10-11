"""Trusted-LAN HTTP is deliberate and never weakens HTTPS readiness."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.mcp import lan


def http_settings(**updates: Any) -> Settings:
    values = {
        "mcp_lan_http_enabled": True,
        "mcp_lan_http_url": "http://lab.home.arpa:18121/mcp",
        "mcp_lan_http_bind_address": "192.168.2.191",
        "mcp_lan_http_allowed_cidr": "192.168.2.0/24",
        **updates,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_http_default_and_https_coexistence() -> None:
    default = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not default.mcp_lan_http_enabled and default.mcp_lan_http_url is None
    settings = http_settings(mcp_lan_url="https://lab.home.arpa/mcp")
    assert settings.mcp_lan_url == "https://lab.home.arpa/mcp"
    assert settings.mcp_lan_http_url == "http://lab.home.arpa:18121/mcp"


@pytest.mark.parametrize(
    "updates",
    [
        {"mcp_lan_http_enabled": False},
        {"mcp_lan_http_url": None},
        {"mcp_lan_http_bind_address": None},
        {"mcp_lan_http_allowed_cidr": None},
        {"mcp_lan_http_url": "https://lab.home.arpa/mcp"},
        {"mcp_lan_http_url": "http://0.0.0.0/mcp"},
        {"mcp_lan_http_url": "http://127.0.0.1/mcp"},
        {"mcp_lan_http_url": "http://8.8.8.8/mcp"},
        {"mcp_lan_http_url": "http://[fd00::1]/mcp"},
        {"mcp_lan_http_url": "http://lab.home.arpa/mcp?token=secret"},
        {"mcp_lan_http_url": "http://user:secret@lab.home.arpa/mcp"},
        {"mcp_lan_http_url": "http://192.168.2.190:18121/mcp"},
        {"mcp_lan_http_bind_address": "0.0.0.0"},
        {"mcp_lan_http_bind_address": "127.0.0.1"},
        {"mcp_lan_http_bind_address": "8.8.8.8"},
        {"mcp_lan_http_bind_address": "fd00::1"},
        {"mcp_lan_http_bind_address": "192.168.2.0"},
        {"mcp_lan_http_allowed_cidr": "0.0.0.0/0"},
        {"mcp_lan_http_allowed_cidr": "192.168.2.191/24"},
        {"mcp_lan_http_allowed_cidr": "10.0.0.0/24"},
        {"mcp_lan_http_allowed_cidr": "192.0.0.0/8"},
    ],
)
def test_unsafe_or_incomplete_http_configuration_fails_closed(
    updates: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as error:
        http_settings(**updates)
    assert "secret" not in safe_settings_error(error.value)


def test_http_readiness_requires_explicit_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("HTTP attempted network access without explicit bind")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert not asyncio.run(
        lan.check_lan_endpoint("http://lab.home.arpa/mcp", instance_id="instance")
    ).ready


@pytest.mark.parametrize(
    "addresses",
    [[], ["8.8.8.8"], ["192.168.2.190"], ["192.168.2.191", "192.168.2.190"], ["::1"]],
)
def test_http_dns_must_match_only_the_explicit_interface(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 18121))
            for address in addresses
        ],
    )

    def forbidden(**kwargs: Any) -> Any:
        pytest.fail("HTTP request escaped the configured private interface")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert not asyncio.run(
        lan.check_lan_endpoint(
            "http://lab.home.arpa:18121/mcp",
            instance_id="instance",
            trusted_http_bind="192.168.2.191",
        )
    ).ready


@pytest.mark.parametrize(
    "status,marker,ready",
    [
        (401, "instance", True),
        (401, "other", False),
        (302, "instance", False),
        (200, "instance", False),
    ],
)
def test_http_anonymous_pinned_readiness_is_only_reachability(
    monkeypatch: pytest.MonkeyPatch, status: int, marker: str, ready: bool
) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.2.191", 18121))
        ],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://192.168.2.191:18121/mcp"
        assert request.headers["Host"] == "lab.home.arpa:18121"
        assert (
            "Authorization" not in request.headers and "Cookie" not in request.headers
        )
        return httpx.Response(
            status,
            headers={
                "WWW-Authenticate": "Bearer",
                lan.MCP_INSTANCE_HEADER: marker,
                "Location": "http://attacker.invalid/mcp",
            },
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert not kwargs["trust_env"] and not kwargs["follow_redirects"]
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = asyncio.run(
        lan.check_lan_endpoint(
            "http://lab.home.arpa:18121/mcp",
            instance_id="instance",
            trusted_http_bind="192.168.2.191",
        )
    )
    assert result.ready is ready
    if ready:
        assert "not server identity or encryption" in result.message
    else:
        assert "HTTP endpoint" in result.message and "certificate" not in result.message

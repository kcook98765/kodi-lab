"""LAN configuration/readiness fails closed without credentials or Kodi I/O."""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.mcp import lan
from kodi_lab.web.run import McpAccessLogFilter, application_logging_config


@pytest.mark.parametrize(
    "url",
    [
        "https://192.168.2.191:18443/mcp",
        "https://lab.home.arpa/mcp",
        "https://10.2.3.4/mcp",
        "https://172.16.1.2:443/mcp",
    ],
)
def test_explicit_lan_configuration(url: str) -> None:
    assert Settings(_env_file=None, mcp_lan_url=url).mcp_lan_url == url  # type: ignore[call-arg]
    assert Settings(_env_file=None, mcp_lan_url="").mcp_lan_url is None  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.2.191/mcp",
        "https://127.0.0.1/mcp",
        "https://8.8.8.8/mcp",
        "https://0.0.0.0/mcp",
        "https://[::1]/mcp",
        "https://localhost/mcp",
        "https://*.home.arpa/mcp",
        "https://Lab.home.arpa/mcp",
        "https://lab.home.arpa/mcp/",
        "https://lab.home.arpa/api/mcp",
        "https://user:secret@lab.home.arpa/mcp",
        "https://lab.home.arpa/mcp?token=secret",
        "https://lab.home.arpa/mcp?",
        "https://lab.home.arpa/mcp#secret",
        "https://lab.home.arpa:0/mcp",
        "https://lab.home.arpa:65536/mcp",
        "https://lab.home.arpa:0443/mcp",
        "https://lab.home.arpa/mcp\n",
    ],
)
def test_bad_lan_configuration_is_rejected_without_echo(url: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, mcp_lan_url=url)  # type: ignore[call-arg]
    message = safe_settings_error(error.value)
    assert "KODI_LAB_MCP_LAN_URL" in message
    assert url not in message and "secret" not in message


def test_ca_path_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KODI_LAB_MCP_LAN_CA_FILE", "")
    assert Settings(_env_file=None).mcp_lan_ca_file is None  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_lan_ca_file=Path("relative.pem"))  # type: ignore[call-arg]


def test_readiness_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unconfigured endpoint attempted networking")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    status = asyncio.run(lan.check_lan_endpoint(None, instance_id="this-process"))
    assert status.url is None and not status.ready


@pytest.mark.parametrize(
    "status,instance,authenticate,ready",
    [
        (401, "this-process", "Bearer", True),
        (401, "other-process", "Bearer", False),
        (401, "this-process", "Basic", False),
        (200, "this-process", "Bearer", False),
        (301, "this-process", "Bearer", False),
        (403, "this-process", "Bearer", False),
    ],
)
def test_readiness_has_no_token_or_redirect_and_requires_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    instance: str,
    authenticate: str,
    ready: bool,
) -> None:
    requests: list[httpx.Request] = []
    original = httpx.AsyncClient
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.2.191", 443))
        ],
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert request.url.query == b""
        return httpx.Response(
            status,
            headers={
                lan.MCP_INSTANCE_HEADER: instance,
                "WWW-Authenticate": authenticate,
                "Location": "https://other.home.arpa/mcp",
            },
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["verify"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["verify"].check_hostname
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = asyncio.run(
        lan.check_lan_endpoint("https://192.168.2.191/mcp", instance_id="this-process")
    )
    assert result.ready is ready
    assert len(requests) == 1


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["8.8.8.8"],
        ["127.0.0.1"],
        ["192.168.2.191", "8.8.8.8"],
        ["192.168.2.191", "::1"],
    ],
)
def test_readiness_rejects_non_lan_dns(
    monkeypatch: pytest.MonkeyPatch, addresses: list[str]
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ],
    )

    def forbidden(**kwargs: Any) -> None:
        pytest.fail("unsafe DNS reached HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert not asyncio.run(
        lan.check_lan_endpoint("https://lab.home.arpa/mcp", instance_id="this-process")
    ).ready


def test_missing_ca_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.2.191", 443))
        ],
    )
    assert not asyncio.run(
        lan.check_lan_endpoint(
            "https://192.168.2.191/mcp",
            instance_id="this-process",
            ca_file=tmp_path / "absent.pem",
        )
    ).ready


def test_access_logging_does_not_echo_mcp_query_or_path_tokens() -> None:
    secret = "credential-that-must-not-be-logged"
    for path in (
        f"/mcp?token={secret}",
        f"/mcp/{secret}",
        f"/mcp/?bearer_token={secret}",
    ):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("peer", "POST", path, "1.1", 401),
            None,
        )
        assert McpAccessLogFilter().filter(record)
        assert secret not in record.getMessage()
        assert "POST /mcp HTTP/1.1" in record.getMessage()
    assert application_logging_config()["handlers"]["access"]["filters"] == [
        "mcp_access"
    ]


def test_dns_probe_pins_private_address_and_preserves_tls_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.AsyncClient
    lookups: list[str] = []

    def resolve(host: str, *args: Any) -> list[Any]:
        lookups.append(host)
        # Any subsequent lookup would return a public address.
        address = "192.168.2.191" if len(lookups) == 1 else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "192.168.2.191"
        assert request.headers["Host"] == "lab.home.arpa"
        assert request.extensions["sni_hostname"] == "lab.home.arpa"
        assert "Authorization" not in request.headers
        return httpx.Response(
            401,
            headers={
                lan.MCP_INSTANCE_HEADER: "this-process",
                "WWW-Authenticate": "Bearer",
            },
        )

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["verify"].check_hostname
        return original(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert asyncio.run(
        lan.check_lan_endpoint("https://lab.home.arpa/mcp", instance_id="this-process")
    ).ready
    assert lookups == ["lab.home.arpa"]

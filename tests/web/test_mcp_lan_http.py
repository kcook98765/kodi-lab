"""MCP-only plaintext opt-in preserves the shared HTTPS/stdio authority."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.mcp.lan import (
    McpLanStatus,
)
from kodi_lab.web import app as app_module
from kodi_lab.web.app import create_app

from .conftest import (
    FakeBroker,
    FakeExternalKodi,
    login,
)
from .test_kodi_views import household as household
from .test_mcp import _authority, _post
from .test_mcp_lan import (
    LAN_URL,
    REPOSITORY,
)

HTTP_URL = "http://192.168.50.10:18121/mcp"


def _http_app(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    *,
    url: str = HTTP_URL,
    tls_url: str = LAN_URL,
    enabled: bool = True,
) -> FastAPI:
    context = cast(FastAPI, client.app).state.kodi_lab
    bind = urlsplit(url).hostname or ""
    settings = Settings(
        _env_file=None,
        **{  # type: ignore[call-arg]
            **context.settings.model_dump(),
            "mcp_lan_url": tls_url,
            "mcp_lan_http_enabled": enabled,
            "mcp_lan_http_url": url if enabled else None,
            "mcp_lan_http_bind_address": bind if enabled else None,
            "mcp_lan_http_allowed_cidr": str(
                ipaddress.IPv4Network(f"{bind}/24", strict=False)
            )
            if enabled
            else None,
        },
    )
    return create_app(
        settings=settings,
        session_factory=database,
        broker=broker,
        auth=context.auth,
        external_target_transport=httpx.MockTransport(external_kodi.handle),
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_http_opt_in_adds_only_exact_http_authority_and_preserves_https(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    enabled: bool,
) -> None:
    app = _http_app(client, database, broker, external_kodi, enabled=enabled)
    with TestClient(app, base_url="https://127.0.0.1") as authority:
        _, token = _authority(authority)
        for endpoint, expected in [(LAN_URL, 200), (HTTP_URL, 200 if enabled else 421)]:
            authority.base_url = endpoint.removesuffix("/mcp")
            assert (
                _post(
                    authority,
                    token,
                    "tools/list",
                    extra_headers={"Origin": endpoint.removesuffix("/mcp")},
                ).status_code
                == expected
            )
            if enabled:
                for method in ["GET", "POST", "DELETE", "OPTIONS"]:
                    assert (
                        authority.request(
                            method,
                            "/mcp",
                            headers={"Origin": "http://attacker.invalid"},
                        ).status_code
                        == 403
                    )
        authority.base_url = HTTP_URL.removesuffix("/mcp")
        for origin in [
            "http://192.168.50.10:18122",
            "https://192.168.50.10:18121",
            "null",
        ]:
            assert _post(
                authority, token, "tools/list", extra_headers={"Origin": origin}
            ).status_code in {403, 421}
        assert (
            _post(
                authority,
                token,
                "tools/list",
                extra_headers={"Host": "192.168.50.10:18122"},
            ).status_code
            == 421
        )


@contextmanager
def _http_proxy(root: Path, settings: Settings, upstream: str) -> Iterator[str]:
    if shutil.which("nginx") is None:
        pytest.skip("nginx unavailable for disposable HTTP proxy")
    root.mkdir()
    url = settings.mcp_lan_http_url
    assert settings.mcp_lan_http_enabled and url
    parsed = urlsplit(url)
    socket_directory = tempfile.TemporaryDirectory(prefix="kl-http-")
    settings.mcp_lan_http_readiness_socket = (
        Path(socket_directory.name) / "readiness.sock"
    )
    replacements = {
        "PRIVATE_LAN_IPV4": settings.mcp_lan_http_bind_address,
        "HTTP_PORT": str(parsed.port or 80),
        "MCP_HOST": parsed.hostname,
        "MCP_AUTHORITY": parsed.netloc,
        "AUTHORIZED_LAN_CIDR": settings.mcp_lan_http_allowed_cidr,
        "LOOPBACK_WEB_PORT": str(urlsplit(upstream).port),
        "READINESS_SOCKET_PATH": str(settings.mcp_lan_http_readiness_socket),
    }
    template = (REPOSITORY / "deployment/mcp-lan-http-nginx.conf.template").read_text()
    for marker, value in replacements.items():
        assert value
        template = template.replace(f"@{marker}@", value)
    assert "@" not in template
    config = root / "nginx.conf"
    config.write_text(
        f"pid {root}/nginx.pid;\nerror_log /dev/null crit;\n"
        "events { worker_connections 64; }\nhttp { access_log off;\n"
        f"client_body_temp_path {root}/body;\nproxy_temp_path {root}/proxy;\n"
        f"{template}\n}}\n"
    )
    subprocess.run(["nginx", "-t", "-c", str(config)], capture_output=True, check=True)
    process = subprocess.Popen(
        ["nginx", "-c", str(config), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(
                    (parsed.hostname or "", parsed.port or 80), timeout=0.2
                ):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("disposable HTTP proxy did not start") from None
                time.sleep(0.02)
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)
        socket_directory.cleanup()


def _free_port(bind: str) -> int:
    with socket.socket() as listener:
        listener.bind((bind, 0))
        return cast(int, listener.getsockname()[1])


@pytest.mark.parametrize("healthy", [False, True])
def test_agents_separates_http_internal_health_from_https_and_lan_acceptance(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    monkeypatch: pytest.MonkeyPatch,
    healthy: bool,
) -> None:
    app = _http_app(client, database, broker, external_kodi)
    checks: list[str] = []

    async def https(url: str | None, **kwargs: object) -> McpLanStatus:
        assert url == LAN_URL and "trusted_http_bind" not in kwargs
        checks.append("https")
        return McpLanStatus(url, True, "HTTPS verification unchanged")

    async def internal(url: str | None, **kwargs: object) -> McpLanStatus:
        assert url == HTTP_URL and kwargs["bind_address"] == "192.168.50.10"
        assert "readiness_socket" in kwargs
        checks.append("internal")
        return McpLanStatus(url, healthy, "Internal health; separate LAN test required")

    monkeypatch.setattr(app_module, "check_lan_endpoint", https)
    monkeypatch.setattr(app_module, "check_http_proxy", internal)
    with TestClient(app, base_url="https://127.0.0.1") as authority:
        login(authority)
        page = authority.get("/mcp-clients")
        assert page.status_code == 200 and checks == ["https", "internal"]
        assert "separate LAN test required" in page.text
        assert 'value="lan" data-agent-transport checked' in page.text
        assert "Reachable HTTP endpoint" not in page.text
        if healthy:
            assert (
                "internal health verified; LAN access requires a client test"
                in page.text
            )
        else:
            assert "internal health unverified" in page.text


def _browser_check(authority: TestClient, upstream: str) -> None:
    root = Path(os.environ["KODI_LAB_TEST_HTTP_BROWSER_EVIDENCE"])
    context = cast(FastAPI, authority.app).state.kodi_lab
    login(authority)
    scenarios = {}
    for name, https_enabled, http_enabled in [
        ("both", True, True),
        ("http-only", False, True),
        ("local-only", False, False),
    ]:
        https_url = context.settings.mcp_lan_url
        context.settings.mcp_lan_http_enabled = http_enabled
        if not https_enabled:
            context.settings.mcp_lan_url = None
        page = authority.get("/mcp-clients")
        assert page.status_code == 200
        scenarios[name] = page.text
        context.settings.mcp_lan_url = https_url
    context.settings.mcp_lan_http_enabled = True
    (root / "browser-fixtures.json").write_text(
        json.dumps({"baseURL": upstream.removesuffix("/mcp"), "html": scenarios})
    )
    result = subprocess.run(
        ["node", str(REPOSITORY / "tests/web/trusted_lan_http_browser.cjs")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        pytest.fail(
            "Disposable browser check failed: " + result.stderr[:1500], pytrace=False
        )
    (root / "browser-gate.txt").write_text(result.stdout)

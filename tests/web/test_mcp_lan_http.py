"""MCP-only plaintext opt-in preserves the shared HTTPS/stdio authority."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.config import Settings
from kodi_lab.mcp.lan import check_lan_endpoint, is_private_lan_address
from kodi_lab.storage.models import ExternalProductionPolicyRow
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    FakeBroker,
    FakeExternalKodi,
    coordinator_factory,
    login,
)
from .test_kodi_views import household as household
from .test_mcp import _administration, _authority, _post
from .test_mcp_lan import (
    LAN_URL,
    REPOSITORY,
    _launch_ryzen,
    _ryzen_configuration,
    _tls_proxy,
)
from .test_mcp_stdio import _live_loopback_server, _make_target_ready

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
        coordinator_factory=coordinator_factory,
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
    with TestClient(app, base_url="http://127.0.0.1") as authority:
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
    replacements = {
        "PRIVATE_LAN_IPV4": settings.mcp_lan_http_bind_address,
        "HTTP_PORT": str(parsed.port or 80),
        "MCP_HOST": parsed.hostname,
        "MCP_AUTHORITY": parsed.netloc,
        "AUTHORIZED_LAN_CIDR": settings.mcp_lan_http_allowed_cidr,
        "LOOPBACK_WEB_PORT": str(urlsplit(upstream).port),
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


def _free_port(bind: str) -> int:
    with socket.socket() as listener:
        listener.bind((bind, 0))
        return cast(int, listener.getsockname()[1])


def test_real_http_and_https_sdk_with_direct_ryzen_and_disposable_credentials(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    household: UUID,
    tmp_path: Path,
) -> None:
    bind = os.environ.get("KODI_LAB_TEST_LAN_IPV4", "")
    if not is_private_lan_address(bind):
        pytest.skip("set KODI_LAB_TEST_LAN_IPV4 to the actual private test interface")
    _make_target_ready(client)
    url = f"http://{bind}:{_free_port(bind)}/mcp"
    tls_url = f"https://{bind}:{_free_port(bind)}/mcp"
    app = _http_app(client, database, broker, external_kodi, url=url, tls_url=tls_url)
    settings = app.state.kodi_lab.settings
    tls_root = tmp_path / "tls"
    tls_root.mkdir()
    with (
        TestClient(app, base_url="http://127.0.0.1") as authority,
        _live_loopback_server(app) as (upstream, _),
        _http_proxy(tmp_path / "http", settings, upstream),
        _tls_proxy(
            tls_root,
            bind,
            upstream,
            port=urlsplit(tls_url).port,
            authorized_cidr=str(ipaddress.IPv4Network(f"{bind}/24", strict=False)),
        ) as (_, ca),
    ):
        settings.mcp_lan_ca_file = ca
        admin = _administration(authority)
        _, token = _authority(authority)
        reader = admin.create_client(
            display_name="Disposable HTTP Home reader",
            preset_id=PRODUCTION_INSPECTION_PRESET_ID,
        )
        admin.grant_target(reader.id, household)
        admin.assign_target_preset(household, PRODUCTION_INSPECTION_PRESET_ID)
        with database() as session:
            policy = session.get(ExternalProductionPolicyRow, str(household))
            assert policy
            generation = policy.identity_generation
        scope = admin.grant_production_inspection_scope(
            reader.id, household, identity_generation=generation
        )
        credential = admin.issue_credential(reader.id)
        assert asyncio.run(
            check_lan_endpoint(
                url,
                instance_id=app.state.kodi_lab.mcp_transport.instance_id,
                trusted_http_bind=bind,
            )
        ).ready
        assert asyncio.run(
            check_lan_endpoint(
                tls_url,
                instance_id=app.state.kodi_lab.mcp_transport.instance_id,
                ca_file=ca,
            )
        ).ready
        assert not asyncio.run(
            check_lan_endpoint(
                tls_url, instance_id=app.state.kodi_lab.mcp_transport.instance_id
            )
        ).ready
        with httpx.Client(trust_env=False, follow_redirects=False) as wire:
            for path in ["/login", "/api/mcp/clients", "/api/instances", "/mcp/"]:
                assert wire.get(url.removesuffix("/mcp") + path).status_code == 404
            assert wire.post(url + "?token=placeholder", json={}).status_code == 400
            # Even a real Web login cookie cannot authenticate MCP.
            login(authority)
            wire.cookies.update(dict(authority.cookies))
            cookie_response = wire.post(
                url,
                headers={"MCP-Protocol-Version": "2025-11-25"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert cookie_response.status_code == 401
            assert (
                wire.post(url, headers={"Host": "wrong.invalid"}, json={}).status_code
                == 421
            )
            assert (
                wire.post(
                    url, headers={"Origin": "http://attacker.invalid"}, json={}
                ).status_code
                == 403
            )

        async def sdk_checks() -> None:
            for endpoint in [url, tls_url]:
                for revision in ["2025-11-25", "2026-07-28"]:
                    async with (
                        httpx2.AsyncClient(
                            verify=ssl.create_default_context(cafile=ca),
                            trust_env=False,
                            headers={"Authorization": f"Bearer {token}"},
                        ) as http,
                        Client(
                            streamable_http_client(
                                endpoint, http_client=http, terminate_on_close=False
                            ),
                            mode="legacy" if revision == "2025-11-25" else revision,
                            cache=None,
                        ) as sdk,
                    ):
                        assert sdk.protocol_version == revision
                        tools = await sdk.list_tools()
                        assert "target_status" in {tool.name for tool in tools.tools}
                        result = await sdk.call_tool("target_list", {})
                        assert not result.is_error and str(INSTANCE_ID) in json.dumps(
                            result.model_dump(mode="json")
                        )

        asyncio.run(sdk_checks())
        if os.environ.get("KODI_LAB_TEST_RYZEN") == "1":
            config = _ryzen_configuration(
                authority, database, household, credential.token, url, ca
            )
            config["trusted_lan_http"] = True
            _launch_ryzen(config, "ryzen-http-acceptance.json")
            admin.revoke_production_inspection_scope(scope.scope_id)
            _launch_ryzen(
                {**config, "scope_revoked_only": True},
                "ryzen-http-scope-revocation.json",
            )
        if os.environ.get("KODI_LAB_TEST_HTTP_BROWSER_EVIDENCE"):
            _browser_check(authority, upstream)
        admin.revoke_credential(reader.id, credential.credential.id)
        admin.set_client_enabled(reader.id, False)
        assert external_kodi.mutations == 0


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

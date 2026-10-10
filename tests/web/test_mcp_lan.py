"""Direct LAN HTTP reuses authority; TLS proxy exposes only the MCP route."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.mcp.lan import McpLanStatus, check_lan_endpoint, is_private_lan_address
from kodi_lab.storage.models import ExternalProductionPolicyRow, McpCredentialRow
from kodi_lab.web import app as app_module
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    FakeExternalKodi,
    coordinator_factory,
    login,
)
from .test_kodi_views import household as household
from .test_mcp import _administration, _authority, _post
from .test_mcp_stdio import _live_loopback_server, _make_target_ready

REPOSITORY = Path(__file__).resolve().parents[2]
LAN_URL = "https://192.168.50.10:18443/mcp"


def _lan_app(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    url: str = LAN_URL,
) -> FastAPI:
    context = cast(FastAPI, client.app).state.kodi_lab
    return create_app(
        settings=context.settings.model_copy(update={"mcp_lan_url": url}),
        session_factory=database,
        broker=broker,
        auth=context.auth,
        coordinator_factory=coordinator_factory,
        external_target_transport=httpx.MockTransport(external_kodi.handle),
    )


@pytest.fixture
def lan_client(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
) -> Iterator[TestClient]:
    with TestClient(
        _lan_app(client, database, broker, external_kodi),
        base_url=LAN_URL.removesuffix("/mcp"),
    ) as lan_client:
        yield lan_client


def test_direct_http_checks_origin_before_auth_on_all_methods(
    lan_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for method in ("POST", "GET", "DELETE", "OPTIONS", "HEAD", "PATCH"):
        assert (
            lan_client.request(
                method,
                "/mcp",
                headers={
                    "Origin": "https://attacker.invalid/token-that-must-not-be-logged"
                },
            ).status_code
            == 403
        )
    assert "token-that-must-not-be-logged" not in caplog.text
    _, token = _authority(lan_client)
    assert (
        _post(
            lan_client,
            token,
            "tools/list",
            extra_headers={"Origin": LAN_URL.removesuffix("/mcp")},
        ).status_code
        == 200
    )
    for origin in (
        "null",
        "http://192.168.50.10:18443",
        "https://192.168.50.10:18444",
        "https://192.168.50.10:18443/",
        "https://192.168.50.10:18443.evil",
    ):
        assert (
            _post(
                lan_client, token, "tools/list", extra_headers={"Origin": origin}
            ).status_code
            == 403
        )
    for host in ("192.168.50.10:18444", "192.168.50.10:bad", "192.168.50.10"):
        assert (
            _post(
                lan_client, token, "tools/list", extra_headers={"Host": host}
            ).status_code
            == 421
        )
    for header in ("Host", "Origin", "Authorization", "MCP-Protocol-Version"):
        response = lan_client.post(
            "/mcp",
            headers=[
                (header, "one"),
                (header, "two"),
                ("Content-Type", "application/json"),
            ],
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert response.status_code in {400, 403, 421}
    for path in ("/mcp/", "/api/mcp", "/sse"):
        assert lan_client.post(path, follow_redirects=False, json={}).status_code in {
            404,
            405,
            307,
        }
    alias = lan_client.post("/mcp/", follow_redirects=False, json={})
    if alias.status_code == 307:
        assert alias.headers["Location"] == LAN_URL
        assert _post(lan_client, "", "tools/list").status_code == 401
    assert lan_client.get("/mcp").status_code == 405


@pytest.mark.parametrize("ready", [False, True])
def test_agents_prefers_only_verified_lan_and_never_recovers_credentials(
    client: TestClient,
    lan_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
) -> None:
    login(client)
    lan_client.cookies = client.cookies
    cast(FastAPI, lan_client.app).state.kodi_lab.settings.web_port = 8180

    async def checked(url: str | None, **kwargs: Any) -> McpLanStatus:
        assert url == LAN_URL and kwargs["instance_id"]
        return McpLanStatus(url, ready, "Test endpoint verification result")

    monkeypatch.setattr(app_module, "check_lan_endpoint", checked)
    _, token = _authority(lan_client)
    page = lan_client.get("http://127.0.0.1/mcp-clients").text
    configuration = re.search(r'<pre id="agent-config"[^>]*>(.*?)</pre>', page, re.S)
    assert configuration is not None
    payload = json.loads(configuration.group(1))
    assert token not in page
    assert 'data-agent-local-port value="8180"' in page
    if ready:
        assert payload == {
            "type": "http",
            "url": LAN_URL,
            "headers": {"Authorization": "Bearer PASTE_NEW_TOKEN_HERE"},
        }
        assert 'value="lan" data-agent-transport checked' in page
        assert "Verified MCP endpoint" in page and "Copy MCP URL" in page
        assert "private certificate authority" in page
        assert "http://127.0.0.1:8180/mcp" in page
    else:
        assert payload["command"] == "kodi-lab-mcp-stdio"
        assert payload["env"]["KODI_LAB_MCP_URL"] == "http://127.0.0.1:8180/mcp"
        assert 'value="lan" data-agent-transport disabled' in page
        assert "verification failed" in page
        assert "data-copy-agent-url" not in page


@contextmanager
def _tls_proxy(
    root: Path,
    bind: str,
    upstream_url: str,
    *,
    port: int | None = None,
    authorized_cidr: str | None = None,
) -> Iterator[tuple[str, Path]]:
    if shutil.which("nginx") is None:
        pytest.skip("nginx unavailable for isolated TLS integration")
    if port is None:
        with socket.socket() as listener:
            listener.bind((bind, 0))
            port = listener.getsockname()[1]
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Isolated MCP test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(bind))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    ca = root / "certificate.pem"
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file = root / "key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    template = (REPOSITORY / "deployment/mcp-lan-nginx.conf.template").read_text()
    replacements = {
        "PRIVATE_LAN_IPV4": bind,
        "TLS_PORT": str(port),
        "MCP_HOST": bind,
        "MCP_AUTHORITY": f"{bind}:{port}",
        "CERTIFICATE_CHAIN_FILE": str(ca),
        "CERTIFICATE_KEY_FILE": str(key_file),
        "AUTHORIZED_LAN_CIDR": authorized_cidr or f"{bind}/32",
        "LOOPBACK_WEB_PORT": str(httpx.URL(upstream_url).port),
    }
    for marker, value in replacements.items():
        template = template.replace(f"@{marker}@", value)
    config = root / "nginx.conf"
    config.write_text(
        f"pid {root}/nginx.pid;\nerror_log /dev/null crit;\n"
        f"events {{ worker_connections 64; }}\nhttp {{\naccess_log off;\n"
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
                with socket.create_connection((bind, port), timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("isolated TLS proxy did not start") from None
                time.sleep(0.02)
        yield f"https://{bind}:{port}/mcp", ca
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_real_sdk_through_private_tls_proxy_has_auth_and_home_scope_parity(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    external_kodi: FakeExternalKodi,
    household: UUID,
    tmp_path: Path,
) -> None:
    bind = os.environ.get("KODI_LAB_TEST_LAN_IPV4", "")
    if not is_private_lan_address(bind):
        pytest.skip(
            "set KODI_LAB_TEST_LAN_IPV4 to this test host's private LAN address"
        )
    _make_target_ready(client)
    with socket.socket() as listener:
        listener.bind((bind, 0))
        port = listener.getsockname()[1]
    url = f"https://{bind}:{port}/mcp"
    remote_enabled = os.environ.get("KODI_LAB_TEST_RYZEN") == "1"
    app = _lan_app(client, database, broker, external_kodi, url)
    with (
        TestClient(app, base_url="http://127.0.0.1") as authority,
        _live_loopback_server(app) as (upstream, _),
        _tls_proxy(
            tmp_path,
            bind,
            upstream,
            port=port,
            authorized_cidr=str(ipaddress.IPv4Network(f"{bind}/24", strict=False))
            if remote_enabled
            else None,
        ) as (url, ca),
    ):
        cast(FastAPI, authority.app).state.kodi_lab.settings.web_port = urlsplit(
            upstream
        ).port
        _exercise_proxy(authority, database, external_kodi, household, url, ca)


def _exercise_proxy(
    authority: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
    home: UUID,
    url: str,
    ca: Path,
) -> None:
    context = cast(FastAPI, authority.app).state.kodi_lab
    administration = _administration(authority)
    internal_id, internal_token = _authority(authority)
    home_client = administration.create_client(
        display_name="Isolated LAN Home reader",
        preset_id=PRODUCTION_INSPECTION_PRESET_ID,
    )
    administration.grant_target(home_client.id, home)
    administration.assign_target_preset(home, PRODUCTION_INSPECTION_PRESET_ID)
    with database() as session:
        policy = session.get(ExternalProductionPolicyRow, str(home))
        assert policy is not None
        generation = policy.identity_generation
    scope = administration.grant_production_inspection_scope(
        home_client.id, home, identity_generation=generation
    )
    home_credential = administration.issue_credential(home_client.id)
    context.settings.mcp_lan_ca_file = ca
    assert asyncio.run(
        check_lan_endpoint(
            url, instance_id=context.mcp_transport.instance_id, ca_file=ca
        )
    ).ready
    assert not asyncio.run(
        check_lan_endpoint(url, instance_id=context.mcp_transport.instance_id)
    ).ready
    if os.environ.get("KODI_LAB_TEST_BROWSER_EVIDENCE"):
        _check_browser(authority, url)
    tls = ssl.create_default_context(cafile=ca)
    with httpx.Client(verify=tls, trust_env=False, follow_redirects=False) as wire:
        for path in ("/login", "/api/mcp/clients", "/mcp/", "/api/instances"):
            assert wire.get(url.removesuffix("/mcp") + path).status_code == 404
        assert wire.post(url + "?token=fake-secret", json={}).status_code == 400
        assert (
            wire.post(
                url, headers={"Origin": "https://attacker.invalid"}, json={}
            ).status_code
            == 403
        )
        for token in (None, "malformed", "a" * 43):
            response = wire.post(
                url,
                headers={
                    "MCP-Protocol-Version": "2025-11-25",
                    **({"Authorization": f"Bearer {token}"} if token else {}),
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 401 and "tools" not in response.json()
        for lifecycle in ("expired", "revoked", "disabled"):
            issued = administration.issue_credential(internal_id)
            if lifecycle == "revoked":
                administration.revoke_credential(internal_id, issued.credential.id)
            elif lifecycle == "disabled":
                administration.set_client_enabled(internal_id, False)
            else:
                with database.begin() as session:
                    row = session.get(McpCredentialRow, str(issued.credential.id))
                    assert row is not None
                    row.created_at = "1999-01-01T00:00:00.000000Z"
                    row.expires_at = "2000-01-01T00:00:00.000000Z"
            response = wire.post(
                url,
                headers={
                    "Authorization": f"Bearer {issued.token}",
                    "MCP-Protocol-Version": "2025-11-25",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 401
            administration.set_client_enabled(internal_id, True)

    remote_configuration = None
    if os.environ.get("KODI_LAB_TEST_RYZEN") == "1":
        remote_configuration = _ryzen_configuration(
            authority, database, home, home_credential.token, url, ca
        )
        _launch_ryzen(remote_configuration, "ryzen-acceptance.json")

    async def exercise() -> None:
        for version in ("2026-07-28", "2025-11-25"):
            for token, target in (
                (internal_token, INSTANCE_ID),
                (home_credential.token, home),
            ):
                async with httpx2.AsyncClient(
                    verify=tls,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Authorization": f"Bearer {token}"},
                ) as http:
                    streams = streamable_http_client(
                        url, http_client=http, terminate_on_close=False
                    )
                    async with Client(
                        streams,
                        mode="legacy" if version == "2025-11-25" else version,
                        cache=None,
                    ) as sdk:
                        assert sdk.protocol_version == version
                        tools = await sdk.list_tools()
                        assert "target_list" in {tool.name for tool in tools.tools}
                        assert "workspace_edit" not in {
                            tool.name for tool in tools.tools
                        }
                        listed = await sdk.call_tool("target_list", {})
                        assert not listed.is_error
                        assert str(target) in json.dumps(listed.model_dump(mode="json"))
                        if target == home:
                            # Respect the unchanged Home health inspection limit.
                            await asyncio.sleep(5.1)
                        health = await sdk.call_tool(
                            "target_status", {"target_id": str(target)}
                        )
                        assert not health.is_error
                        denied = await sdk.call_tool(
                            "target_status", {"target_id": str(UNRELATED_INSTANCE_ID)}
                        )
                        assert denied.is_error
                        assert (
                            cast(Any, denied.content[0]).text
                            == "target_not_found_or_not_authorized"
                        )
                        for tool, arguments in (
                            (
                                "remote_action",
                                {
                                    "target_id": str(target),
                                    "request_id": str(uuid4()),
                                    "action": "left",
                                },
                            ),
                            ("development_source_list", {}),
                        ):
                            denied = await sdk.call_tool(tool, arguments)
                            assert denied.is_error
                            assert (
                                cast(Any, denied.content[0]).text == "permission_denied"
                            )
        administration.revoke_production_inspection_scope(scope.scope_id)
        async with (
            httpx2.AsyncClient(
                verify=tls,
                trust_env=False,
                headers={"Authorization": f"Bearer {home_credential.token}"},
            ) as http,
            Client(
                streamable_http_client(url, http_client=http, terminate_on_close=False),
                mode="2026-07-28",
                cache=None,
            ) as sdk,
        ):
            denied = await sdk.call_tool("target_status", {"target_id": str(home)})
            assert denied.is_error
            assert (
                cast(Any, denied.content[0]).text
                == "target_not_found_or_not_authorized"
            )

    asyncio.run(exercise())
    if remote_configuration is not None:
        _launch_ryzen(
            {**remote_configuration, "scope_revoked_only": True},
            "ryzen-scope-revocation.json",
        )
    assert external_kodi.mutations == 0


def _ryzen_configuration(
    authority: TestClient,
    database: sessionmaker[Session],
    home: UUID,
    home_token: str,
    url: str,
    ca: Path,
) -> dict[str, Any]:
    administration = _administration(authority)
    reader_id, token = _authority(authority)
    expired = administration.issue_credential(reader_id)
    with database.begin() as session:
        row = session.get(McpCredentialRow, str(expired.credential.id))
        assert row is not None
        row.created_at = "1999-01-01T00:00:00.000000Z"
        row.expires_at = "2000-01-01T00:00:00.000000Z"
    revoked = administration.issue_credential(reader_id)
    administration.revoke_credential(reader_id, revoked.credential.id)
    disabled_id, disabled_token = _authority(authority)
    administration.set_client_enabled(disabled_id, False)
    preset = administration.create_custom_preset(
        display_name="Isolated visual-only immutable snapshot",
        template_preset_id=administration.get_client(reader_id).preset.id,
        permissions=frozenset({"target.visual"}),
    )
    insufficient = administration.create_client(
        display_name="Isolated insufficient reader", preset_id=preset.id
    )
    issued = administration.issue_credential(insufficient.id)
    return {
        "endpoint": url,
        "ca_pem": ca.read_text(),
        "token": token,
        "target": str(INSTANCE_ID),
        "home_target": str(home),
        "home_token": home_token,
        "ungranted_target": str(UNRELATED_INSTANCE_ID),
        "insufficient_token": issued.token,
        "bad_tokens": {
            "expired": expired.token,
            "revoked": revoked.token,
            "disabled": disabled_token,
        },
    }


def _check_browser(authority: TestClient, url: str) -> None:
    login(authority)
    page = authority.get("/mcp-clients")
    assert page.status_code == 200 and "Verified MCP endpoint" in page.text
    root = Path(os.environ["KODI_LAB_TEST_BROWSER_EVIDENCE"])
    (root / "agents-fixture.json").write_text(
        json.dumps(
            {
                "endpoint": url,
                "stdio_port": cast(
                    FastAPI, authority.app
                ).state.kodi_lab.settings.web_port,
                "html": page.text,
            }
        )
    )
    result = subprocess.run(
        ["node", str(REPOSITORY / "tests/web/native_lan_mcp_browser.cjs")],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        pytest.fail("Browser check failed: " + result.stderr[:1000], pytrace=False)
    (root / "browser-gate.txt").write_text(result.stdout)


def _launch_ryzen(configuration: dict[str, Any], evidence_name: str) -> None:
    identity = os.environ["KODI_LAB_TEST_RYZEN_IDENTITY"]
    known_hosts = os.environ["KODI_LAB_TEST_RYZEN_KNOWN_HOSTS"]
    source = (REPOSITORY / "scripts/validate-native-lan-mcp-client.py").read_text()
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-p",
            "22",
            "-i",
            identity,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ConnectTimeout=5",
            "kyle@192.168.2.141",
            "python3 -c " + shlex.quote(source),
        ],
        input=json.dumps(configuration),
        text=True,
        capture_output=True,
        timeout=90,
    )
    # Never emit stderr/input or a response which contains a test credential.
    secrets = [
        configuration["token"],
        configuration["home_token"],
        configuration["insufficient_token"],
        *configuration["bad_tokens"].values(),
    ]
    if any(secret in result.stdout + result.stderr for secret in secrets):
        pytest.fail("remote client emitted sensitive input", pytrace=False)
    report = json.loads(result.stdout)
    if result.returncode != 0 or not report.get("passed"):
        pytest.fail(
            "Ryzen MCP check failed: " + report.get("check", "unknown"), pytrace=False
        )
    assert report["hostname"] == "Ryzen" and report["user"] == "kyle"
    assert report["source_address"] == "192.168.2.141"
    root = Path(os.environ["KODI_LAB_TEST_RYZEN_EVIDENCE"])
    (root / evidence_name).write_text(json.dumps(report, indent=2) + "\n")


def test_overlong_and_malformed_wildcard_ports_are_rejected(client: TestClient) -> None:
    for port in ("9" * 4500, "0", "65536", "bad", "8180.evil", "8180/path"):
        for header, value, expected in (
            ("Host", f"127.0.0.1:{port}", 421),
            ("Origin", f"http://127.0.0.1:{port}", 403),
        ):
            response = client.post("/mcp", headers={header: value}, json={})
            assert response.status_code == expected

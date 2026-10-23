"""Agents navigation crosses the real administrator TLS proxy route guards."""

from __future__ import annotations

import re
import shutil
import socket
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import PASSWORD
from tests.web.test_mcp_stdio import _live_loopback_server

REPOSITORY = Path(__file__).resolve().parents[2]


@contextmanager
def _admin_proxy(root: Path, upstream: str) -> Iterator[tuple[str, Path]]:
    for executable in ("nginx", "openssl"):
        if shutil.which(executable) is None:
            pytest.skip(f"{executable} required for isolated administrator TLS proxy")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    certificate = root / "certificate.pem"
    key = root / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=Isolated administrator proxy",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    guards = REPOSITORY / "deployment/admin-lan-route-guards.nginx.conf"
    config = root / "nginx.conf"
    config.write_text(
        f"pid {root}/nginx.pid;\nerror_log /dev/null crit;\n"
        "events { worker_connections 64; }\nhttp {\naccess_log off;\n"
        f"client_body_temp_path {root}/body;\nproxy_temp_path {root}/proxy;\n"
        "server {\n"
        f"listen 127.0.0.1:{port} ssl;\nserver_name 127.0.0.1;\n"
        f"ssl_certificate {certificate};\nssl_certificate_key {key};\n"
        "allow 127.0.0.1;\ndeny all;\n"
        f'if ($http_host != "127.0.0.1:{port}") {{ return 421; }}\n'
        f"include {guards};\nlocation / {{\nproxy_pass {upstream};\n"
        "proxy_set_header Host $http_host;\n"
        "proxy_set_header Origin $http_origin;\n"
        "proxy_set_header X-Forwarded-Proto https;\n"
        "proxy_cookie_flags kodi_lab_session secure httponly samesite=strict;\n"
        "}\n}\n}\n"
    )
    subprocess.run(["nginx", "-t", "-c", str(config)], check=True, capture_output=True)
    process = subprocess.Popen(
        ["nginx", "-c", str(config), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("isolated administrator proxy failed") from None
                time.sleep(0.02)
        yield f"https://127.0.0.1:{port}", certificate
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_agents_navigation_and_authentication_through_tls_proxy(
    client: TestClient, tmp_path: Path
) -> None:
    with (
        _live_loopback_server(client.app) as (mcp_url, _),
        _admin_proxy(tmp_path, mcp_url.removesuffix("/mcp")) as (base, ca),
        httpx.Client(
            base_url=base,
            verify=ssl.create_default_context(cafile=str(ca)),
            trust_env=False,
            follow_redirects=True,
        ) as browser,
    ):
        for path in ("/mcp-clients", "/mcp-clients/", "/api/mcp/clients"):
            assert browser.get(path).status_code == 401
        response = browser.post(
            "/login", data={"password": PASSWORD}, headers={"Origin": base}
        )
        assert response.status_code == 200
        cookie = response.history[0].headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("secure", "httponly", "samesite=strict"))
        destination = re.search(r'<a href="([^"]+)"[^>]*>Agents</a>', response.text)
        assert destination is not None
        assert destination.group(1) == "/mcp-clients"
        for suffix in ("", "/", "?view=all"):
            page = browser.get(destination.group(1) + suffix)
            assert page.status_code == 200
            assert "<h1>Agents</h1>" in page.text
            assert page.headers["cache-control"] == "no-store"
        for path in (
            "/",
            "/kodi",
            "/development",
            "/settings",
            "/api/mcp/clients",
            "/api/mcp/presets",
            "/api/mcp/target-policies",
        ):
            assert browser.get(path).status_code == 200, path
        for path in (
            "/mcp",
            "/mcp?query=1",
            "/mcp/",
            "/mcp/tools",
            "/native-zip-packages/test",
        ):
            for method in ("GET", "POST", "DELETE"):
                assert browser.request(method, path).status_code == 404
        assert (
            browser.post(
                "/api/mcp/clients", json={}, headers={"Origin": base}
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/admin-view",
                data={},
                headers={"Origin": "https://untrusted.invalid"},
            ).status_code
            == 403
        )
        assert (
            browser.get("/login", headers={"Host": "untrusted.invalid"}).status_code
            == 421
        )

"""Combined fresh-install readiness uses disposable TLS and mocked host commands."""

from __future__ import annotations

import io
import json
import socket
import subprocess
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import pytest

from scripts import install
from tests.packaging.test_host_installer import host as ubuntu_host_fixture
from tests.packaging.test_host_installer import mcp_opener
from tests.packaging.test_https_ingress_installer import host as windows_host_fixture
from tests.packaging.test_https_ingress_installer import options
from tests.packaging.test_repository_https import proxy as proxy_fixture

opener = mcp_opener
ubuntu_host = ubuntu_host_fixture
windows_host = windows_host_fixture
proxy = proxy_fixture


@pytest.mark.parametrize("mode", ["local-http", "lan-http", "https", "windows-caddy"])
@pytest.mark.parametrize("failure", ["", "web", "mcp", "provenance", "tls", "content"])
@pytest.mark.parametrize("proxy", ["repo.example.test"], indirect=True)
def test_both_readiness_paths_precede_ready_and_preserve_failures(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    proxy: Any,
    mode: str,
    failure: str,
) -> None:
    windows = mode == "windows-caddy"
    site = request.getfixturevalue("windows_host" if windows else "ubuntu_host")
    web_opener = request.getfixturevalue("opener" if windows else "mcp_opener")
    args = options(site) if windows else site.args()
    args.repository_base_url = proxy.base
    resolve = socket.getaddrinfo

    def local_dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        return resolve(
            "127.0.0.1" if host == "repo.example.test" else host, *args, **kwargs
        )

    monkeypatch.setattr(socket, "getaddrinfo", local_dns)
    if not windows:
        args.web_access_mode = mode
    if mode == "lan-http":
        args.web_lan_ip = "192.168.1.20"
        monkeypatch.setattr("builtins.input", lambda _: args.web_lan_ip)
    if failure == "tls":
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    else:
        monkeypatch.setenv("SSL_CERT_FILE", str(proxy.cert))
    if failure == "content":
        proxy.handler.fault = "tampered"

    events: list[str] = []

    def stage() -> str:
        return str(
            json.loads((site.root / "INSTALLATION.json").read_bytes())[
                "completed_stage"
            ]
        )

    def web_request(value: str | Request, *, timeout: int) -> Any:
        assert stage() == "services_started"
        url = value if isinstance(value, str) else value.full_url
        events.append(url)
        if isinstance(value, str):
            web_opener.open.return_value.__enter__.return_value.status = (
                503 if failure == "web" else 200
            )
            return web_opener.open.return_value
        headers = Message()
        headers["WWW-Authenticate"] = "Bearer"
        lan = "192.168.1.20" in url
        # LAN denial must still be checked when repository HTTPS is enabled.
        code = 403 if lan or failure == "mcp" else 401
        raise HTTPError(
            url,
            code,
            "test",
            headers,
            io.BytesIO(b'{"detail":"mcp_requires_local_listener"}') if lan else None,
        )

    web_opener.open.side_effect = web_request
    monkeypatch.setattr(
        install,
        "build_opener",
        lambda *handlers: (
            build_opener(*handlers)
            if any(isinstance(item, install.RepositoryNoRedirect) for item in handlers)
            else web_opener
        ),
    )
    run = install.run

    def command(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        result = run(command, cwd, capture=capture)
        if "kodi-lab-repository" in command and "verify" in command and capture:
            assert stage() == "services_started"
            events.append("repository-verify")
            if failure == "provenance":
                raise subprocess.CalledProcessError(1, command)
            return json.dumps(proxy.manifests)
        return result

    monkeypatch.setattr(install, "run", command)
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            install.install(args)
    else:
        install.install(args)
    assert stage() == ("services_started" if failure else "ready")
    assert (site.root / "bundle/BUNDLE.json").is_file()
    if failure in {"web", "mcp"}:
        assert "repository-verify" not in events
    else:
        assert events[-1] == "repository-verify"
        assert events[:2] == [
            "http://127.0.0.1:18100/login",
            "http://127.0.0.1:18100/mcp",
        ]
        if mode == "lan-http":
            assert events[2:4] == [
                "http://192.168.1.20:18100/login",
                "http://192.168.1.20:18100/mcp",
            ]
    if not failure:
        for feed in ("stable", "development"):
            assert f"/{feed}/addons.xml" in proxy.seen
            assert f"/{feed}/addons.xml.sha256" in proxy.seen
        assert any("service.kodi_lab_bridge-" in path for path in proxy.seen)
    elif failure == "content":
        assert proxy.seen == ["/stable/addons.xml"]
    else:
        assert proxy.seen == []
    assert not any(
        token in command
        for command in site.commands
        for token in ("down", "stop", "prune")
    )
    if windows:
        data = json.loads((site.root / "INSTALLATION.json").read_bytes())
        assert data["web_access_mode"] == "https"
        assert (site.root / "Caddyfile").read_text() == install.caddyfile(
            data["https_ingress"]
        )

"""Fresh mode selection is explicit, narrow and immutable on restart."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from scripts import install
from tests.packaging.test_host_installer import (
    Host,
)
from tests.packaging.test_host_installer import (
    host as host_fixture,
)
from tests.packaging.test_host_installer import (
    mcp_opener as opener_fixture,
)
from tests.packaging.test_https_ingress_installer import installed_bundle
from tests.packaging.test_linux_v1_bundle import bundle as bundle_fixture
from tests.packaging.test_linux_v1_bundle import bundle_tool

host = host_fixture
mcp_opener = opener_fixture
bundle = bundle_fixture


@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0",
        "::1",
        "8.8.8.8",
        "100.64.1.1",
        "169.254.1.2",
        "192.168.1.2/24",
        "localhost",
        "*",
    ],
)
def test_lan_selection_rejects_nonprivate_or_broad_binds(bind: str) -> None:
    with pytest.raises(ValueError):
        install.web_options(argparse.Namespace(web_lan_ip=bind), "ubuntu", None)
    with pytest.raises(ValueError):
        bundle_tool["validate_web_access"]("lan-http", bind)


def test_default_install_records_http_and_correct_url(
    host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    install.install(host.args())
    data = json.loads((host.root / "INSTALLATION.json").read_text())
    assert (data["web_access_mode"], data["web_bind"]) == ("local-http", "127.0.0.1")
    command = next(cmd for cmd in host.commands if "configure" in cmd)
    assert command[command.index("--web-access-mode") + 1] == "local-http"
    output = capsys.readouterr().out
    assert "http://127.0.0.1:18100/login" in output and "Local HTTP" in output


def test_lan_confirmation_precedes_installation(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = host.args()
    args.web_lan_ip = "192.168.1.20"
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(ValueError, match="not confirmed"):
        install.install(args)
    assert not host.root.exists()
    assert not any("initialize" in cmd or "configure" in cmd for cmd in host.commands)


def test_confirmed_lan_install_binds_exact_ip_and_checks_mcp_isolation(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    mcp_opener: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = host.args()
    args.web_lan_ip = "192.168.1.20"
    binds: list[tuple[str, int]] = []
    monkeypatch.setattr(
        install, "available_port", lambda ip, port: binds.append((ip, port))
    )
    monkeypatch.setattr("builtins.input", lambda _: args.web_lan_ip)
    headers = Message()
    headers["WWW-Authenticate"] = "Bearer"
    mcp_opener.open.side_effect = [
        mcp_opener.open.return_value,
        HTTPError("http://127.0.0.1:18100/mcp", 401, "test", headers, None),
        mcp_opener.open.return_value,
        HTTPError(
            "http://192.168.1.20:18100/mcp",
            403,
            "test",
            headers,
            io.BytesIO(b'{"detail":"mcp_requires_local_listener"}'),
        ),
    ]
    install.install(args)
    assert binds[:2] == [("127.0.0.1", 18100), ("192.168.1.20", 18100)]
    command = next(cmd for cmd in host.commands if "configure" in cmd)
    assert "--confirm-lan-http" in command
    assert command[command.index("--web-published-bind") + 1] == "192.168.1.20"
    output = capsys.readouterr().out
    assert "unencrypted" in output and "http://192.168.1.20:18100/login" in output
    assert "http://127.0.0.1:18100/mcp" in output
    requests = [item.args[0] for item in mcp_opener.open.call_args_list]
    assert [item if isinstance(item, str) else item.full_url for item in requests] == [
        "http://127.0.0.1:18100/login",
        "http://127.0.0.1:18100/mcp",
        "http://192.168.1.20:18100/login",
        "http://192.168.1.20:18100/mcp",
    ]
    assert all(
        "compose.lan-http.yaml" in " ".join(cmd)
        for cmd in host.commands
        if cmd[:2] == ["docker", "compose"]
    )


@pytest.mark.parametrize(
    "repository_url", ["http://192.168.1.20:18120", "https://repo.example.test"]
)
@pytest.mark.parametrize(
    "mode,bind",
    [("local-http", "127.0.0.1"), ("https", "127.0.0.1"), ("lan-http", "192.168.1.20")],
)
def test_bundle_configuration_and_verification_preserve_explicit_mode(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    bind: str,
    repository_url: str,
) -> None:
    data = bundle_tool["verify"](bundle)

    def loaded(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{command[-1]} linux/amd64"
        )

    monkeypatch.setattr(subprocess, "run", loaded)
    configure = bundle_tool["configure"]
    configure(
        bundle,
        data,
        18100,
        "192.168.1.20",
        18120,
        repository_url,
        web_access_mode=mode,
        web_published_bind=bind,
        confirm_lan_http=True,
    )
    before = (bundle / ".env").read_bytes()
    assert f"KODI_LAB_WEB_ACCESS_MODE={mode}\n".encode() in before
    assert f"KODI_LAB_WEB_PUBLISHED_BIND={bind}\n".encode() in before
    bundle_tool["verify"](bundle)
    monkeypatch.undo()
    rendered = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "--env-file",
                str(bundle / ".env"),
                "-f",
                str(bundle / "compose.release.yaml"),
                *(
                    ["-f", str(bundle / "compose.lan-http.yaml")]
                    if mode == "lan-http"
                    else []
                ),
                "config",
                "--format",
                "json",
            ],
            text=True,
            env=install.environment(),
        )
    )
    web = rendered["services"]["web"]
    expected_ports = [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8100,
            "published": "18100",
            "protocol": "tcp",
        }
    ]
    if mode == "lan-http":
        expected_ports.append(
            {
                "mode": "ingress",
                "host_ip": bind,
                "target": 8101,
                "published": "18100",
                "protocol": "tcp",
            }
        )
    assert web["ports"] == expected_ports
    assert web["environment"]["KODI_LAB_WEB_ACCESS_MODE"] == mode
    assert web["environment"]["FORWARDED_ALLOW_IPS"] == ""
    repository = rendered["services"]["repository"]
    assert repository["environment"]["KODI_LAB_REPOSITORY_BASE_URL"] == repository_url
    assert repository["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "192.168.1.20",
            "target": 8110,
            "published": "18120",
            "protocol": "tcp",
        }
    ]
    assert repository["read_only"] is True
    with pytest.raises(ValueError, match="already exists"):
        configure(
            bundle, data, 18100, "192.168.1.20", 18120, "http://192.168.1.20:18120"
        )
    assert (bundle / ".env").read_bytes() == before


def test_direct_bundle_lan_requires_acknowledgement(bundle: Path) -> None:
    with pytest.raises(ValueError, match="acknowledgement"):
        bundle_tool["configure"](
            bundle,
            bundle_tool["verify"](bundle),
            18100,
            "192.168.1.20",
            18120,
            "http://192.168.1.20:18120",
            web_access_mode="lan-http",
            web_published_bind="192.168.1.20",
        )
    assert not (bundle / ".env").exists()


def test_legacy_receipt_cannot_silently_enable_http(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installed"
    data = installed_bundle(bundle, root)
    (root / "compose.windows.yaml").write_text(install.windows_overlay(18121))
    install.save(root, data, "ready")
    monkeypatch.setattr(install, "host_kind", lambda: "windows-wsl2")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    assert "web_access_mode" not in install.load(root)
    env = root / "bundle/.env"
    with env.open("a") as output:
        output.write(
            "KODI_LAB_WEB_ACCESS_MODE=local-http\nKODI_LAB_WEB_PUBLISHED_BIND=127.0.0.1\n"
        )
    with pytest.raises(ValueError, match="Web security configuration differs"):
        install.load(root)

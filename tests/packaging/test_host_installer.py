"""Fresh host installation keeps trust verification and project/state boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from scripts import install
from tests.packaging.test_linux_v1_bundle import bundle as verified_bundle

# Reuse the independently verified bundle fixture, including its real verifier.
bundle = verified_bundle
ROOT = Path(__file__).resolve().parents[2]


class Host:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.commands: list[list[str]] = []
        self.kind = "ubuntu"
        self.fail = ""
        monkeypatch.setattr(install, "host_kind", lambda: self.kind)
        monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
        monkeypatch.setattr(install, "available_port", lambda *_: None)
        monkeypatch.setattr(install, "probe", lambda *_: None)
        monkeypatch.setattr(shutil, "which", lambda value: "/usr/bin/" + value)
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _: SimpleNamespace(free=20 << 30),
        )
        monkeypatch.setattr(install, "run", self.run)

    def run(self, command: list[str], cwd: Path, *, capture: bool = False) -> str:
        self.commands.append(command)
        if self.fail and self.fail in command:
            raise subprocess.CalledProcessError(1, command)
        if command == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        if command[:2] == ["ip", "-j"]:
            return "[]"
        if command[0] == "bash":
            target = Path(command[-1])
            target.mkdir()
            (target / "BUNDLE.json").write_text(json.dumps({"source_commit": "a" * 40}))
        return ""

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            directory=str(self.root),
            repository_bind="192.168.2.20",
            repository_base_url=None,
            repository_port=18120,
            repository_local_port=18121,
            web_port=18100,
        )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path / "new installation", monkeypatch)


@pytest.mark.parametrize("kind", ["ubuntu", "windows-wsl2"])
def test_complete_install_orders_verification_before_initialization(
    host: Host, kind: str
) -> None:
    host.kind = kind
    install.install(host.args())
    data = json.loads((host.root / "INSTALLATION.json").read_text())
    assert data["completed_stage"] == "ready"
    assert data["source_commit"] == "a" * 40
    assert data["engine_id"] == "engine-one"
    assert host.root.stat().st_mode & 0o777 == 0o700
    assert (host.root / "kodi-lab.py").read_bytes() == (
        ROOT / "scripts/install.py"
    ).read_bytes()
    commands = host.commands

    def index(token: str) -> int:
        return next(i for i, command in enumerate(commands) if token in command)

    assert index("verify") < index("load") < index("configure")
    assert index("configure") < index("repository-init") < index("initialize")
    assert (
        index("initialize")
        < index("set-password")
        < index("network-ensure")
        < index("up")
    )
    assert "--wait" in commands[index("up")]
    for command in commands:
        if command[:2] == ["docker", "compose"]:
            assert command[command.index("--project-name") + 1] == data["project"]
            assert command[command.index("--env-file") + 1] == str(
                host.root / "bundle/.env"
            )
    assert (host.root / "compose.windows.yaml").exists() == (kind == "windows-wsl2")


def test_failed_password_setup_preserves_database_and_never_starts_services(
    host: Host,
) -> None:
    host.fail = "set-password"
    with pytest.raises(subprocess.CalledProcessError):
        install.install(host.args())
    receipt = (host.root / "INSTALLATION.json").read_bytes()
    assert json.loads(receipt)["completed_stage"] == "database_initialized"
    assert all(
        "network-ensure" not in command and "up" not in command
        for command in host.commands
    )
    before = list(host.commands)
    with pytest.raises(ValueError, match="partial installs are preserved"):
        install.install(host.args())
    assert before == host.commands
    assert (host.root / "INSTALLATION.json").read_bytes() == receipt


def test_daemon_change_during_build_blocks_initialization(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = iter(["engine-one", "different-engine"])
    monkeypatch.setattr(install, "engine", lambda *_: next(engines))
    with pytest.raises(ValueError, match="changed during installation"):
        install.install(host.args())
    assert not any(
        "initialize" in command or "repository-init" in command
        for command in host.commands
    )


@pytest.mark.parametrize(
    "failure",
    [
        "verify",
        "load",
        "configure",
        "repository-init",
        "initialize",
        "network-ensure",
        "up",
    ],
)
def test_failures_do_not_advance_or_remove_partial_install(
    host: Host, failure: str
) -> None:
    host.fail = failure
    with pytest.raises(subprocess.CalledProcessError):
        install.install(host.args())
    assert (host.root / "INSTALLATION.json").is_file()
    assert (
        json.loads((host.root / "INSTALLATION.json").read_text())["completed_stage"]
        != "ready"
    )
    assert not any(
        "down" in command or "prune" in command or "rm" in command
        for command in host.commands
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"repository_bind": "0.0.0.0"},
        {"repository_bind": "127.0.0.1"},
        {"repository_base_url": "http://user:secret@192.168.2.20:18120"},
        {"web_port": 18120},
        {"web_port": 80},
        {"repository_local_port": 18100},
    ],
)
def test_bad_configuration_is_rejected_before_any_directory_is_created(
    host: Host, changes: dict[str, Any]
) -> None:
    host.kind = "windows-wsl2"
    args = host.args()
    for key, value in changes.items():
        setattr(args, key, value)
    with pytest.raises(ValueError):
        install.install(args)
    assert not host.root.exists()
    assert all(command[0] != "bash" for command in host.commands)


@pytest.mark.parametrize("route", ["10.250.0.0/24", "10.0.0.0/8", "10.250.0.12/32"])
def test_host_route_collision_blocks_installation_before_build(
    host: Host, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    original = host.run

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        if command[0] == "ip":
            return json.dumps([{"dst": route}])
        return original(command, cwd, capture=capture)

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(ValueError, match="overlaps"):
        install.install(host.args())
    assert not host.root.exists()


def test_existing_managed_network_blocks_fresh_install(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        install, "run", lambda *_args, **_kwargs: install.MANAGED_NETWORK
    )
    with pytest.raises(ValueError, match="already exists"):
        install.check_networks(host.root.parent)


@pytest.mark.parametrize("reason", ["disk", "dirty", "busy"])
def test_failed_preflight_has_no_installation_side_effects(
    host: Host, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    if reason == "disk":
        monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    elif reason == "dirty":
        original = host.run

        def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
            if command == ["git", "status", "--porcelain"]:
                return " M README.md"
            return original(command, cwd, capture=capture)

        monkeypatch.setattr(install, "run", run)
    else:

        def busy(*_: object) -> None:
            raise OSError("port in use")

        monkeypatch.setattr(install, "available_port", busy)
    with pytest.raises((ValueError, OSError)):
        install.install(host.args())
    assert not host.root.exists()
    assert not any(command[0] in {"uv", "bash"} for command in host.commands)


@pytest.mark.parametrize(
    "kernel,expected",
    [
        ("6.8.0-generic", "ubuntu"),
        ("6.6.87.2-microsoft-standard-WSL2", "windows-wsl2"),
        ("4.4.0-Microsoft", None),
    ],
)
def test_native_and_wsl2_detection_rejects_wsl1(
    monkeypatch: pytest.MonkeyPatch, kernel: str, expected: str | None
) -> None:
    monkeypatch.setattr(platform, "release", lambda: kernel)
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        platform,
        "freedesktop_os_release",
        lambda: {
            "ID": "ubuntu",
            "VERSION_ID": "24.04",
        },
    )
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    if expected:
        assert install.host_kind() == expected
    else:
        with pytest.raises(ValueError, match="WSL1"):
            install.host_kind()


def test_environment_cannot_override_bundle_or_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPOSE_FILE", "/some/other/compose.yaml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "production")
    monkeypatch.setenv("KODI_LAB_REMOTE_IMAGE", "unreviewed:latest")
    monkeypatch.setenv("GIT_WORK_TREE", "/some/other/source")
    monkeypatch.setenv("GIT_INDEX_FILE", "/some/other/index")
    assert not any(
        key.startswith(("COMPOSE_", "KODI_LAB_")) for key in install.environment()
    )
    assert install.environment()["PATH"] == os.environ["PATH"]
    assert "GIT_WORK_TREE" not in install.environment()
    assert "GIT_INDEX_FILE" not in install.environment()


@pytest.mark.parametrize(
    "endpoint",
    ["tcp://127.0.0.1:2375", "ssh://another-host", "unix:///run/user/1000/docker.sock"],
)
def test_remote_and_alternate_daemons_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, endpoint: str
) -> None:
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    with pytest.raises(ValueError, match="local unix"):
        install.engine(tmp_path, "ubuntu")


@pytest.mark.parametrize(
    "info,host",
    [
        ({"OSType": "windows"}, "windows-wsl2"),
        ({"Architecture": "aarch64"}, "ubuntu"),
        ({"SecurityOptions": ["name=rootless"]}, "ubuntu"),
        ({"OperatingSystem": "Docker Desktop"}, "ubuntu"),
        ({}, "windows-wsl2"),
    ],
)
def test_incompatible_daemons_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, info: dict[str, Any], host: str
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    details = {
        "OSType": "linux",
        "Architecture": "amd64",
        "ID": "engine",
        "SecurityOptions": [],
    } | info
    monkeypatch.setattr(install, "run", lambda *_args, **_kwargs: json.dumps(details))
    with pytest.raises(ValueError, match="rootful Linux amd64"):
        install.engine(tmp_path, host)


def test_paths_reject_source_overlap_ntfs_and_links(tmp_path: Path) -> None:
    for value in [str(ROOT / "installation"), str(ROOT.parent), "/mnt/c/lab", "/"]:
        with pytest.raises(ValueError):
            install.installation_path(value, ROOT)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        install.installation_path(str(link / "new"))


def test_management_rechecks_real_bundle_and_daemon_identity(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "installation"
    root.mkdir()
    shutil.copytree(
        bundle, root / "bundle", ignore=shutil.ignore_patterns("installation")
    )
    manifest = json.loads((root / "bundle/BUNDLE.json").read_text())
    images = manifest["application"]["images"]
    bundle_hash = hashlib.sha256((root / "bundle/BUNDLE.json").read_bytes()).hexdigest()
    (root / "bundle/.env").write_text(
        f"KODI_LAB_REMOTE_IMAGE={images['web']['saved_image_id']}\n"
        f"KODI_LAB_BROKER_IMAGE={images['broker']['saved_image_id']}\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
        "KODI_LAB_REPOSITORY_BIND=192.168.2.20\n"
        "KODI_LAB_REPOSITORY_PORT=18120\n"
        "KODI_LAB_REPOSITORY_BASE_URL=http://192.168.2.20:18120\n"
        f"KODI_LAB_REPOSITORY_BUNDLE_SHA256={bundle_hash}\n"
    )
    data = {
        "schema": install.SCHEMA,
        "project": "kodi-lab-012345abcdef",
        "host": "ubuntu",
        "engine_id": "engine-one",
        "web_port": 18100,
        "source_commit": "c" * 40,
        "repository_bind": "192.168.2.20",
        "repository_port": 18120,
        "repository_base_url": "http://192.168.2.20:18120",
        "bundle_sha256": hashlib.sha256(
            (root / "bundle/BUNDLE.json").read_bytes()
        ).hexdigest(),
    }
    install.save(root, data, "ready")
    monkeypatch.setattr(install, "host_kind", lambda: "ubuntu")
    monkeypatch.setattr(install, "engine", lambda *_: "engine-one")
    assert install.load(root)["project"] == data["project"]
    verifier = root / "bundle/linux-v1-bundle"
    original_verifier = verifier.read_bytes()
    verifier.write_text("raise SystemExit(0)\n")
    with pytest.raises(ValueError, match="verifier identity differs"):
        install.load(root)
    verifier.write_bytes(original_verifier)
    env = root / "bundle/.env"
    original_env = env.read_text()
    env.write_text(original_env.replace("PUBLISHED_PORT=18100", "PUBLISHED_PORT=18150"))
    with pytest.raises(ValueError, match="site configuration differs"):
        install.load(root)
    env.write_text(original_env)
    (root / "bundle/compose.release.yaml").write_text("altered")
    with pytest.raises(subprocess.CalledProcessError):
        install.load(root)
    monkeypatch.setattr(install, "engine", lambda *_: "different-engine")
    with pytest.raises(ValueError, match="Docker engine differs"):
        install.load(root)


@pytest.mark.parametrize("action", ["start", "stop", "status"])
def test_management_reuses_project_and_never_reinitializes(
    host: Host, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    data = {
        "project": "kodi-lab-012345abcdef",
        "host": "ubuntu",
        "completed_stage": "ready",
        "web_port": 18100,
    }
    monkeypatch.setattr(install, "load", lambda _: data)
    monkeypatch.setattr(
        sys, "argv", ["installer", action, "--directory", str(host.root)]
    )
    assert install.main() == 0
    assert len(host.commands) == 1
    command = host.commands[0]
    assert command[command.index("--project-name") + 1] == data["project"]
    assert not any(
        value in command for value in ("initialize", "set-password", "build", "down")
    )


def test_start_refuses_a_partial_installation(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        install, "load", lambda _: {"completed_stage": "database_initialized"}
    )
    monkeypatch.setattr(
        sys, "argv", ["installer", "start", "--directory", str(host.root)]
    )
    assert install.main() == 1
    assert not host.commands


def test_windows_override_replaces_lan_bind_and_preserves_broker_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in {
        "KODI_LAB_REMOTE_IMAGE": "sha256:" + "a" * 64,
        "KODI_LAB_BROKER_IMAGE": "sha256:" + "b" * 64,
        "KODI_LAB_REPOSITORY_BIND": "192.168.2.20",
        "KODI_LAB_REPOSITORY_PORT": "18120",
        "KODI_LAB_REPOSITORY_BASE_URL": "http://192.168.2.20:18120",
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256": "c" * 64,
    }.items():
        monkeypatch.setenv(key, value)
    overlay = tmp_path / "windows.yaml"
    overlay.write_text(install.windows_overlay(18121))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "compose.release.yaml"),
            "-f",
            str(overlay),
            "--profile",
            "lab",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    assert services["repository"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8110,
            "published": "18121",
            "protocol": "tcp",
        }
    ]
    for name, service in services.items():
        sockets = [
            mount
            for mount in service.get("volumes", [])
            if mount["target"] == "/var/run/docker.sock"
        ]
        assert bool(sockets) == (name == "broker")
        assert service.get("privileged", False) is False
    assert not services["broker"].get("ports")


@pytest.mark.parametrize(
    "status,challenge,success",
    [(401, "Bearer", True), (403, "Bearer", False), (401, "", False), (200, "", False)],
)
def test_mcp_readiness_requires_anonymous_bearer_denial(
    monkeypatch: pytest.MonkeyPatch, status: int, challenge: str, success: bool
) -> None:
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.status = 200
    if status != 200:
        headers = Message()
        headers["WWW-Authenticate"] = challenge
        error = HTTPError(
            "http://127.0.0.1/mcp",
            status,
            "test",
            headers,
            None,
        )
        opener.open.side_effect = [opener.open.return_value, error]
    monkeypatch.setattr(install, "build_opener", lambda *_: opener)
    if success:
        install.probe(18100)
    else:
        with pytest.raises(ValueError):
            install.probe(18100)


@pytest.mark.parametrize(
    "extra",
    [
        ["--project-name", "other"],
        ["--env-file=other"],
        ["-f", "other.yaml"],
        ["-pproduction"],
        ["-fother.yaml"],
    ],
)
def test_management_cannot_override_its_project_before_loading_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer", "compose", "--directory", str(tmp_path), "--", "ps", *extra],
    )
    assert install.main() == 1

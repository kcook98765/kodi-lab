"""Permission preparation is confined to disposable, identity-checked mounts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scripts import install
from tests.packaging.test_https_ingress_installer import config


def site(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "project": "kodi-lab-caddy-test-" + uuid.uuid4().hex[:12],
        "host": "windows-wsl2",
        "web_port": 18100,
        "repository_port": 18120,
        "repository_local_port": 18121,
        "https_ingress": config(),
    }
    data["https_ingress_identity"] = install.ingress_identity(data, config())
    install.write_caddyfile(root, config())
    return data


def test_private_generation_ignores_restrictive_umask(tmp_path: Path) -> None:
    mask = os.umask(0o077)
    try:
        data = site(tmp_path)
    finally:
        os.umask(mask)
    path = tmp_path / "Caddyfile"
    assert path.stat().st_uid == os.geteuid()
    assert path.stat().st_mode & 0o777 == 0o640
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == data["https_ingress_identity"]["caddyfile_sha256"]
    )


@pytest.mark.parametrize("tamper", ["mode", "symlink", "hardlink"])
def test_unexpected_bind_rejected_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    data = site(tmp_path)
    path = tmp_path / "Caddyfile"
    if tamper == "mode":
        path.chmod(0o644)
    elif tamper == "hardlink":
        (tmp_path / "linked").hardlink_to(path)
    else:
        path.rename(tmp_path / "target")
        path.symlink_to(tmp_path / "target")
    monkeypatch.setattr(install, "run", lambda *_a, **_kw: pytest.fail("Docker called"))
    with pytest.raises(ValueError, match="Caddyfile"):
        install.prepare_caddy(tmp_path, data)


@pytest.mark.parametrize("tamper", ["name", "driver", "scope", "options", "labels"])
def test_unexpected_volume_identity_never_runs_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    data = site(tmp_path)
    name = str(data["project"]) + "_caddy_data"
    volume: dict[str, Any] = {
        "Name": name,
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Labels": {
            "com.docker.compose.project": data["project"],
            "com.docker.compose.volume": "caddy_data",
        },
    }
    volume[
        {
            "name": "Name",
            "driver": "Driver",
            "scope": "Scope",
            "options": "Options",
            "labels": "Labels",
        }[tamper]
    ] = {"device": "/elsewhere"} if tamper == "options" else "unexpected"

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        if command[:3] == ["docker", "volume", "ls"]:
            return name
        if command[:3] == ["docker", "volume", "inspect"]:
            return json.dumps([volume])
        pytest.fail("unexpected mutation: " + str(command))

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(ValueError, match="identity"):
        install.prepare_caddy(tmp_path, data)


def test_missing_volume_on_start_cannot_replace_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = site(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
        calls.append(command)
        return ""

    monkeypatch.setattr(install, "run", run)
    with pytest.raises(ValueError, match="refusing CA replacement"):
        install.prepare_caddy(tmp_path, data)
    assert len(calls) == 1 and calls[0][:3] == ["docker", "volume", "ls"]


def test_linux_default_has_no_permission_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(install, "run", lambda *_a, **_kw: pytest.fail("Docker called"))
    install.prepare_caddy(tmp_path, {"host": "ubuntu"}, create=True)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def docker_site(tmp_path: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    if os.environ.get("KODI_LAB_TEST_CADDY_PERMISSIONS") != "1":
        pytest.skip("opt-in disposable Docker Caddy fixtures")
    install.run(
        ["docker", "image", "inspect", install.CADDY_IMAGE], tmp_path, capture=True
    )
    data = site(tmp_path)
    try:
        yield tmp_path, data
    finally:
        # Exact fixture names only; never enumerate deletion candidates.
        for role in ("caddy_data", "caddy_config"):
            subprocess.run(
                ["docker", "volume", "rm", data["project"] + "_" + role],
                capture_output=True,
                check=False,
            )


def container(root: Path, data: dict[str, Any], *, admin: bool = False) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "0:0" if admin else "10003:10003",
    ]
    if admin:
        command += ["--cap-add", "CHOWN"]
    for role, target in (("caddy_data", "/data"), ("caddy_config", "/config")):
        command += ["--mount", f"type=volume,src={data['project']}_{role},dst={target}"]
    return command


@pytest.mark.parametrize("skeleton", [False, True])
def test_real_ownership_access_and_repeat_preservation(
    docker_site: tuple[Path, dict[str, Any]], skeleton: bool
) -> None:
    root, data = docker_site
    if skeleton:
        # Simulate Compose's official-image copy-up before our first activation.
        for role in ("caddy_data", "caddy_config"):
            install.run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    "com.docker.compose.project=" + data["project"],
                    "--label",
                    "com.docker.compose.volume=" + role,
                    data["project"] + "_" + role,
                ],
                root,
            )
        install.run(
            [
                *container(root, data, admin=True),
                "--entrypoint",
                "/bin/true",
                install.CADDY_IMAGE,
            ],
            root,
        )
    install.prepare_caddy(root, data, create=True)
    info = (root / "Caddyfile").stat()
    assert info.st_uid == os.geteuid() and info.st_gid == 10003
    assert info.st_mode & 0o777 == 0o640
    probe = [
        *container(root, data),
        "--entrypoint",
        "/bin/sh",
        install.CADDY_IMAGE,
        "-ec",
        "stat -c '%u:%g:%a' /data /data/caddy /config /config/caddy",
    ]
    assert (
        install.run(probe, root, capture=True).splitlines() == ["10003:10003:700"] * 4
    )
    before = install.run(
        [*probe[:-1], "stat -c '%i:%Z:%Y' /data /config"], root, capture=True
    )
    install.prepare_caddy(root, data)
    assert (
        install.run(
            [*probe[:-1], "stat -c '%i:%Z:%Y' /data /config"], root, capture=True
        )
        == before
    )
    assert (root / "Caddyfile").stat().st_ctime_ns == info.st_ctime_ns


@pytest.mark.parametrize("unexpected", ["contents", "owner", "mode", "symlink"])
def test_real_unexpected_volume_state_is_preserved(
    docker_site: tuple[Path, dict[str, Any]], unexpected: str
) -> None:
    root, data = docker_site
    install.prepare_caddy(root, data, create=True)
    # These mutations are confined to our just-created disposable fixtures.
    script = {
        "contents": "touch /data/unexpected",
        "owner": "chown 12345:12345 /data",
        "mode": "chmod 755 /data",
        "symlink": "rmdir /data/caddy; ln -s /config/caddy /data/caddy",
    }[unexpected]
    admin = unexpected == "owner"
    install.run(
        [
            *container(root, data, admin=admin),
            "--entrypoint",
            "/bin/sh",
            install.CADDY_IMAGE,
            "-ec",
            script,
        ],
        root,
    )
    with pytest.raises(subprocess.CalledProcessError):
        install.prepare_caddy(root, data)


def test_real_caddy_restart_keeps_ca_and_configuration(
    docker_site: tuple[Path, dict[str, Any]],
) -> None:
    root, data = docker_site
    install.prepare_caddy(root, data, create=True)
    command = container(root, data)
    command.remove("--rm")
    name = data["project"] + "-restart"
    command += [
        "-d",
        "--name",
        name,
        "--mount",
        f"type=bind,src={root / 'Caddyfile'},dst=/etc/caddy/Caddyfile,readonly",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=128m,mode=700,uid=10003,gid=10003",
        "--entrypoint",
        "/bin/sh",
        install.CADDY_IMAGE,
        "-ec",
        "cp /usr/bin/caddy /tmp/caddy; exec /tmp/caddy run "
        "--config /etc/caddy/Caddyfile --adapter caddyfile",
    ]
    certs = "/data/caddy/pki/authorities/local"
    snapshot = [
        "docker",
        "exec",
        name,
        "/bin/sh",
        "-ec",
        f"sha256sum {certs}/root.crt {certs}/root.key "
        f"{certs}/intermediate.crt {certs}/intermediate.key "
        "/config/caddy/autosave.json",
    ]
    try:
        install.run(command, root)
        for _ in range(50):
            try:
                before = install.run(snapshot, root, capture=True)
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        else:
            pytest.fail("disposable Caddy did not generate its CA")
        install.run(["docker", "restart", name], root)
        install.prepare_caddy(root, data)  # successor/start reuse; no volume creation
        assert install.run(snapshot, root, capture=True) == before
        assert (
            hashlib.sha256((root / "Caddyfile").read_bytes()).hexdigest()
            == data["https_ingress_identity"]["caddyfile_sha256"]
        )
    finally:
        install.run(["docker", "rm", "-f", name], root)

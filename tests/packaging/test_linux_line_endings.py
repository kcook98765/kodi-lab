"""Unix executable bytes must survive a Windows-style Git checkout."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
EXTENSIONLESS = (
    "runtime/example/launch",
    "runtime/example/entrypoint",
    "runtime/example/healthcheck",
)


def _tracked_paths() -> list[str]:
    return (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .rstrip("\0")
        .split("\0")
    )


def _protected_paths() -> list[str]:
    return [
        name
        for name in _tracked_paths()
        if (ROOT / name).read_bytes().startswith(b"#!")
        or name.startswith("src/kodi_lab/runtime/assembly_assets/")
    ]


@pytest.fixture
def windows_checkout(tmp_path: Path) -> Path:
    # A new index and isolated configuration exercise repository policy without
    # inheriting developer attributes, autocrlf, hooks, or Git environment state.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", f"core.attributesFile={os.devnull}", *args],
            cwd=tmp_path,
            env=env,
            stderr=subprocess.PIPE,
        )

    git("init", "--quiet")
    git("config", "core.autocrlf", "true")
    git("config", "core.eol", "crlf")
    (tmp_path / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    for name in _protected_paths():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Also prove Git canonicalizes CRLF contributor input in the index.
        path.write_bytes((ROOT / name).read_bytes().replace(b"\n", b"\r\n"))
    for name in EXTENSIONLESS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    for name in ("unrelated.txt", "windows.cmd"):
        (tmp_path / name).write_bytes(b"unprotected\n")
    git("add", ".")
    for name in [*_protected_paths(), *EXTENSIONLESS]:
        assert b"\r" not in git("show", f":{name}"), name
    # Remove the existing worktree: checkout-index may leave a stat-matching
    # CRLF input untouched even with --force. This must be a fresh checkout.
    for name in [
        ".gitattributes",
        *_protected_paths(),
        *EXTENSIONLESS,
        "unrelated.txt",
        "windows.cmd",
    ]:
        (tmp_path / name).unlink()
    git("checkout-index", "--all", "--force")
    return tmp_path


def test_linux_scripts_have_lf_only_shebangs_and_content() -> None:
    scripts = [
        name
        for name in _tracked_paths()
        if (ROOT / name).read_bytes().startswith(b"#!")
    ]
    assert "runtime/remote-web/launch.sh" in scripts
    for name in scripts:
        data = (ROOT / name).read_bytes()
        assert b"\r" not in data, name
        assert data.partition(b"\n")[0].startswith(b"#!/"), name
        assert b"\n" in data, name


def test_attributes_explicitly_protect_linux_execution_inputs(
    windows_checkout: Path,
) -> None:
    # Current source launchers acquire extensionless names during Docker COPY.
    paths = [*_protected_paths(), *EXTENSIONLESS]
    result = (
        subprocess.check_output(
            ["git", "check-attr", "--cached", "text", "eol", "--", *paths],
            cwd=windows_checkout,
        )
        .decode()
        .splitlines()
    )
    for name in paths:
        assert f"{name}: text: set" in result
        assert f"{name}: eol: lf" in result


def test_windows_checkout_preserves_linux_bytes(windows_checkout: Path) -> None:
    for name in _protected_paths():
        assert (windows_checkout / name).read_bytes() == (ROOT / name).read_bytes(), (
            name
        )
    for name in EXTENSIONLESS:
        assert (windows_checkout / name).read_bytes() == b"#!/bin/sh\nexit 0\n"
    # Negative controls prove autocrlf conversion happened and unrelated text
    # and Windows scripts were not accidentally forced to LF.
    for name in ("unrelated.txt", "windows.cmd"):
        assert (windows_checkout / name).read_bytes() == b"unprotected\r\n"


def test_remote_web_copy_and_default_entrypoint_use_protected_launcher(
    windows_checkout: Path,
) -> None:
    dockerfile = (ROOT / "runtime/remote-web/Dockerfile").read_text()
    source = "runtime/remote-web/launch.sh"
    installed = "/usr/local/bin/kodi-lab-remote-web"
    assert f"COPY {source} {installed}\n" in dockerfile
    assert f"RUN chmod 0755 {installed}\n" in dockerfile
    assert f'ENTRYPOINT ["{installed}"]\n' in dockerfile
    assert (windows_checkout / source).read_bytes().startswith(b"#!/bin/sh\n")

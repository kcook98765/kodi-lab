"""Source-free Compose overlay and host-path boundary tests."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from kodi_lab.development import DevelopmentSourceValidator

REPOSITORY = Path(__file__).parents[2]
TOOL = runpy.run_path(str(REPOSITORY / "scripts/linux-v1-bundle"))


@pytest.mark.parametrize(
    "value",
    ("", ".", "../outside", "/", "/tmp/../outside", "/tmp//outside", "/tmp/'quoted'"),
)
def test_host_source_rejects_unsafe_path_forms(value: str) -> None:
    with pytest.raises(ValueError, match="development source"):
        TOOL["development_source_path"](value, require_directory=False)


def test_host_source_rejects_missing_and_symlink_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    for path in (alias, tmp_path / "missing"):
        with pytest.raises(OSError):
            TOOL["development_source_path"](str(path), require_directory=True)
    assert TOOL["development_source_path"](str(real), require_directory=True) == real


def test_unconfigured_and_unavailable_application_root_are_reported(
    tmp_path: Path,
) -> None:
    assert not DevelopmentSourceValidator(None).root_available()
    assert not DevelopmentSourceValidator(tmp_path / "missing").root_available()
    assert DevelopmentSourceValidator(tmp_path).root_available()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Compose CLI unavailable")
def test_source_free_compose_mount_is_opt_in_fixed_and_space_safe(
    tmp_path: Path,
) -> None:
    for name in ("compose.release.yaml", "compose.development.yaml"):
        (tmp_path / name).write_bytes((REPOSITORY / name).read_bytes())
    source = tmp_path / "Source Trees"
    source.mkdir()
    (tmp_path / ".env").write_text(
        "KODI_LAB_REMOTE_IMAGE=example/web:local\n"
        "KODI_LAB_BROKER_IMAGE=example/broker:local\n"
        "KODI_LAB_PUBLISHED_PORT=18100\n"
    )
    environment = os.environ.copy()
    environment.pop("KODI_LAB_DEVELOPMENT_SOURCE", None)

    def config(*files: str) -> subprocess.CompletedProcess[str]:
        command = ["docker", "compose", "--profile", "lab"]
        for name in files:
            command.extend(("-f", name))
        command.extend(("config", "--format", "json"))
        return subprocess.run(
            command, cwd=tmp_path, env=environment, capture_output=True, text=True
        )

    remote = config("compose.release.yaml")
    assert remote.returncode == 0, remote.stderr
    web = json.loads(remote.stdout)["services"]["web"]
    assert "KODI_LAB_DEVELOPMENT_ROOT" not in web["environment"]
    assert not any(
        item["target"] == "/mnt/kodi-lab-development" for item in web["volumes"]
    )

    empty = config("compose.release.yaml", "compose.development.yaml")
    assert empty.returncode != 0
    assert "KODI_LAB_DEVELOPMENT_SOURCE" in empty.stderr

    (tmp_path / ".env").write_text(
        (tmp_path / ".env").read_text() + "KODI_LAB_DEVELOPMENT_SOURCE=''\n"
    )
    explicitly_empty = config("compose.release.yaml", "compose.development.yaml")
    assert explicitly_empty.returncode != 0
    assert "KODI_LAB_DEVELOPMENT_SOURCE" in explicitly_empty.stderr

    (tmp_path / ".env").write_text(
        (tmp_path / ".env")
        .read_text()
        .replace(
            "KODI_LAB_DEVELOPMENT_SOURCE=''",
            f"KODI_LAB_DEVELOPMENT_SOURCE='{source}'",
        )
    )
    enabled = config("compose.release.yaml", "compose.development.yaml")
    assert enabled.returncode == 0, enabled.stderr
    web = json.loads(enabled.stdout)["services"]["web"]
    mount = next(
        item for item in web["volumes"] if item["target"] == "/mnt/kodi-lab-development"
    )
    assert mount["source"] == str(source)
    assert mount["type"] == "bind"
    assert mount.get("read_only", False) is False
    assert mount["bind"]["create_host_path"] is False
    assert web["environment"]["KODI_LAB_DEVELOPMENT_ROOT"] == (
        "/mnt/kodi-lab-development"
    )
    broker = json.loads(enabled.stdout)["services"]["broker"]
    assert "KODI_LAB_DEVELOPMENT_ROOT" not in broker.get("environment", {})
    assert all(item["source"] != str(source) for item in broker["volumes"])

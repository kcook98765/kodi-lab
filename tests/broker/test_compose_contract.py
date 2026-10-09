"""Compose's two deployment modes preserve the broker boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


def _compose(*args: str) -> dict[str, Any]:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    version = subprocess.run(
        ["docker", "compose", "version"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip("Docker Compose plugin is unavailable")
    result = subprocess.run(
        ["docker", "compose", *args, "config", "--format", "json"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def test_remote_only_compose_has_no_docker_authority() -> None:
    services = _compose()["services"]
    assert set(services) == {"web"}
    web = services["web"]
    assert web["read_only"] is True
    assert web["cap_drop"] == ["ALL"]
    assert web["build"]["target"] == "web"
    assert not web.get("privileged", False)
    assert not any(item["target"] == "/var/run/docker.sock" for item in web["volumes"])
    assert any(
        item["target"] == "/run/kodi-lab" and item["read_only"] is True
        for item in web["volumes"]
    )


def test_lab_compose_confines_docker_mount_to_broker() -> None:
    services = _compose("--profile", "lab")["services"]
    assert set(services) == {"web", "broker"}
    broker = services["broker"]
    assert broker["profiles"] == ["lab"]
    assert broker["build"]["target"] == "broker"
    assert broker["read_only"] is True
    assert broker["cap_drop"] == ["ALL"]
    assert set(broker["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
    assert "no-new-privileges:true" in broker["security_opt"]
    assert not broker.get("privileged", False)
    assert not broker.get("ports")
    assert broker.get("pid") not in {"host"}
    assert broker.get("network_mode") not in {"host"}
    assert {(item["source"], item["target"]) for item in broker["volumes"]} == {
        ("/var/run/docker.sock", "/var/run/docker.sock"),
        ("broker_socket", "/run/kodi-lab"),
        ("remote_state", "/var/lib/kodi-lab"),
    }
    assert broker["healthcheck"]["test"] == ["CMD", "kodi-lab-broker-healthcheck"]
    assert broker["healthcheck"]["timeout"] == "3s"
    assert broker["healthcheck"]["interval"] == "10s"
    assert broker["healthcheck"]["retries"] == 3
    assert broker["healthcheck"]["start_period"] == "15s"


def test_lab_overlay_connects_web_to_managed_network_without_docker_socket() -> None:
    config = _compose(
        "-f", "compose.yaml", "-f", "compose.lab.yaml", "--profile", "lab"
    )
    assert config["networks"]["managed"]["external"] is True
    assert config["networks"]["managed"]["name"] == "kodi-lab-managed-v1"
    web = config["services"]["web"]
    assert set(web["networks"]) == {"default", "managed"}
    assert not any(item["target"] == "/var/run/docker.sock" for item in web["volumes"])
    broker = config["services"]["broker"]
    assert set(broker["networks"]) == {"default"}


def test_release_broker_keeps_the_same_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "KODI_LAB_REMOTE_IMAGE": "sha256:" + "a" * 64,
        "KODI_LAB_BROKER_IMAGE": "sha256:" + "b" * 64,
        "KODI_LAB_REPOSITORY_BIND": "127.0.0.1",
        "KODI_LAB_REPOSITORY_PORT": "18120",
        "KODI_LAB_REPOSITORY_BASE_URL": "http://127.0.0.1:18120",
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256": "c" * 64,
    }.items():
        monkeypatch.setenv(name, value)
    config = _compose("-f", "compose.release.yaml", "--profile", "lab")
    broker = config["services"]["broker"]
    assert broker["healthcheck"] == {
        "test": ["CMD", "kodi-lab-broker-healthcheck"],
        "interval": "10s",
        "timeout": "3s",
        "retries": 3,
        "start_period": "15s",
    }

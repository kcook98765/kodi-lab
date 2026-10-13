"""Reviewed launcher/health/supervisor contract, with deterministic failure cases."""

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kodi_lab.broker.docker_inventory import DOCKER_STOP_TIMEOUT_SECONDS
from kodi_lab.runtime.ostree_assembly import load_assembly_assets

ROOT = (
    Path(__file__).parents[2] / "src/kodi_lab/runtime/assembly_assets/piers-22.0-rc1-r1"
)


def module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "piers_" + name, ROOT / (name + ".py")
    )
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize(
    "change",
    [
        "rc2",
        "stable",
        "beta",
        "bad-ordinal",
        "missing",
        "major",
        "huge",
        "display",
        "valid",
    ],
)
def test_healthcheck_rejects_inexact_identity(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = module("healthcheck")
    version: dict[str, Any] = {
        "major": 22,
        "minor": 0,
        "tag": "releasecandidate",
        "tagversion": "1",
    }
    if change == "rc2":
        version["tagversion"] = "2"
    if change == "stable":
        version["tag"] = "stable"
    if change == "beta":
        version["tag"] = "beta"
    if change == "bad-ordinal":
        version["tagversion"] = True
    if change == "missing":
        version.pop("tagversion")
    if change == "major":
        version["major"] = 23
    raw = json.dumps({"result": {"name": "Kodi", "version": version}}).encode()
    if change == "huge":
        raw = b" " * 8193
    monkeypatch.setattr(
        health.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(raw)
    )

    def display(*args: Any, **kwargs: Any) -> None:
        if change == "display":
            raise subprocess.CalledProcessError(1, "xdpyinfo")

    monkeypatch.setattr(health.subprocess, "run", display)
    if change == "valid":
        health.main()
    else:
        with pytest.raises((RuntimeError, KeyError, subprocess.CalledProcessError)):
            health.main()


def test_stop_budget_below_managed_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "profile_init", module("profile_init"))
    monkeypatch.setitem(sys.modules, "xvfb_guard", module("xvfb_guard"))
    supervisor = module("supervisor")
    assert (
        supervisor.KODI_STOP_SECONDS + supervisor.XVFB_STOP_SECONDS
        < supervisor.TOTAL_STOP_BUDGET_SECONDS
        < DOCKER_STOP_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize("forced", [True, False])
def test_supervisor_forced_termination_is_explicit(
    forced: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "profile_init", module("profile_init"))
    monkeypatch.setitem(sys.modules, "xvfb_guard", module("xvfb_guard"))
    supervisor = module("supervisor")
    signals = []
    monkeypatch.setattr(supervisor.os, "killpg", lambda pid, sig: signals.append(sig))

    class Process:
        pid = 123
        count = 0

        def poll(self) -> None:
            return None

        def wait(self, timeout: float) -> int:
            self.count += 1
            if forced and self.count == 1:
                raise subprocess.TimeoutExpired("kodi", timeout)
            return 0

    assert supervisor.terminate(Process(), 1) is forced
    assert signals == ([15, 9] if forced else [15])


def test_profile_rejects_root_without_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = module("profile_init")
    monkeypatch.setattr(profile.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="UID/GID"):
        profile.initialize()


def test_loader_and_native_paths_are_fixed() -> None:
    assets = load_assembly_assets()
    launcher = assets["launcher.sh"].decode()
    assert "KODI_DATA=/home/kodi/.kodi" in launcher
    assert "LIBGL_DRIVERS_PATH=/opt/mesa/lib/dri" in launcher
    assert "--windowing=x11" in assets["supervisor.py"].decode()
    assert "--audio-backend=alsa" in assets["supervisor.py"].decode()
    assert "sudo" not in launcher and "flatpak" not in launcher

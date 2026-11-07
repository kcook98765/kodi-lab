"""Signal/child/HTTP integration without Kodi images or the host Docker Engine."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kodi_lab.broker.docker_inventory import DOCKER_STOP_TIMEOUT_SECONDS
from kodi_lab.runtime.assembly import build_context
from kodi_lab.runtime.display_capabilities import reviewed_display_capability
from kodi_lab.runtime.recipes import (
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
    recipe_target_id,
)

ROOT = Path(__file__).parents[2]
SUPERVISOR = ROOT / "src/kodi_lab/runtime/assembly_assets/graceful_supervisor_v1.py"
FIXTURE = Path(__file__).parent / "fixtures/shutdown_process.py"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("graceful_stop_test", SUPERVISOR)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def events(root: Path) -> list[dict[str, Any]]:
    file = root / "events.jsonl"
    return (
        [json.loads(line) for line in file.read_text().splitlines()]
        if file.exists()
        else []
    )


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="PID-1 adoption/process-group fixture requires Linux or WSL2",
)
@pytest.mark.parametrize(
    "mode", ["normal", "rejected", "http-hang", "http-trickle", "hang", "display-hang"]
)
def test_stop_waits_for_save_and_reaps_owned_groups(tmp_path: Path, mode: str) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "STOP_FIXTURE_ROOT": str(tmp_path),
        "STOP_FIXTURE_MODE": mode,
        "STOP_FIXTURE_PORT": str(port),
    }
    with (tmp_path / "driver.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE), "driver", str(SUPERVISOR)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 5
            while not any(row["event"] == "kodi-ready" for row in events(tmp_path)):
                assert process.poll() is None, (tmp_path / "driver.log").read_text()
                assert time.monotonic() < deadline, "fixture did not start"
                time.sleep(0.02)
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            if mode == "normal":
                process.send_signal(signal.SIGTERM)
            code = process.wait(timeout=5)
            assert time.monotonic() - started < 4
            rows = events(tmp_path)
            names = [row["event"] for row in rows]
            record = json.loads((tmp_path / "supervisor-cycles.jsonl").read_text())
            assert record["shutdown_request"]
            assert code == (1 if mode in {"hang", "display-hang"} else 0)
            if mode not in {"hang", "display-hang"}:
                assert record["forced_kill"] is False
                assert record["kodi_exit_code"] == 0
            if mode != "hang":
                assert (tmp_path / "saved-setting").read_text() == "pending-value"
                assert "bus-alive-at-save" in names
                assert names.index("settings-saved") < names.index("display-terminated")
                assert names.index("kodi-exit") < names.index("display-terminated")
            else:
                assert "settings-saved" not in names
                assert record["timed_out"] and record["forced_kill"]
            if mode in {"rejected", "http-hang", "http-trickle"}:
                assert record["shutdown_method"] == "SIGTERM-kodi"
                assert "wrapper-terminated" not in names
            else:
                assert record["shutdown_method"] == "Application.Quit"
            assert names.count("quit-request") == 1
            if mode == "display-hang":
                assert record["forced_kill"]
            for row in rows:
                with pytest.raises(ProcessLookupError):
                    os.kill(row["pid"], 0)
            for group in {row["group"] for row in rows}:
                with pytest.raises(ProcessLookupError):
                    os.killpg(group, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            # Only groups created by this exact disposable fixture, if it failed.
            for group in {row["group"] for row in events(tmp_path)}:
                assert group != os.getpgrp()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)


def test_total_stop_budget_fits_existing_broker_timeout() -> None:
    helper = module()
    assert (
        helper.KODI_STOP_SECONDS + helper.DISPLAY_STOP_SECONDS + 3 * helper.REAP_SECONDS
        < helper.TOTAL_STOP_BUDGET_SECONDS
        < DOCKER_STOP_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    "generation,revision", [("matrix", 4), ("nexus", 4), ("omega", 6)]
)
def test_new_recipe_preserves_historical_bytes_and_pins(
    generation: str, revision: int
) -> None:
    current = load_reviewed_recipe(generation)
    assert current.recipe.recipe_revision == revision
    previous_revision = revision - 1
    filename = {
        "matrix": "matrix-19.4-display-r3",
        "nexus": "nexus-20.5-display-r3",
        "omega": "omega-21.3-display-r5",
    }[generation]
    import hashlib

    historical_raw = (
        ROOT / "src/kodi_lab/runtime/recipes" / (filename + ".json")
    ).read_bytes()
    previous = load_recorded_reviewed_recipe(
        generation,
        current.recipe.recipe_id,
        previous_revision,
        "sha256:" + hashlib.sha256(historical_raw).hexdigest(),
    )
    assert recipe_target_id(current) == recipe_target_id(previous)
    # Recorded from exact #246, including the generated historical Dockerfile.
    historical_contexts = {
        "matrix": "114ab9cbca4b16a6ac58bf5b4cddaac3a1758901b890861765447ecdb85eb377",
        "nexus": "8419f24ddb4f3678f49cfae8c142d236a004898f971eae42fdc8d7f8f5ff5dc8",
        "omega": "60df7e974c753242ee1862f9eafd8aa82a34eeaf3d1fd535dd4bd5fdf5d903ba",
    }
    assert (
        hashlib.sha256(build_context(previous)).hexdigest()
        == historical_contexts[generation]
    )
    assert current.recipe.critical_packages == previous.recipe.critical_packages
    assert current.recipe.provider == previous.recipe.provider
    for name in previous.assets.keys() - {"entrypoint.sh"}:
        assert current.assets[name] == previous.assets[name]
    assert current.assets["supervisor.py"] == SUPERVISOR.read_bytes()
    assert (
        b"exec /usr/bin/python3 /usr/local/lib/kodi-lab-supervisor.py"
        in current.assets["entrypoint.sh"]
    )
    assert reviewed_display_capability(current) is not None
    with tarfile.open(fileobj=io.BytesIO(build_context(current))) as archive:
        payload = archive.extractfile("supervisor.py")
        assert payload is not None and payload.read() == SUPERVISOR.read_bytes()
        dockerfile = archive.extractfile("Dockerfile")
        assert (
            dockerfile is not None
            and b"--chown=root:root supervisor.py" in dockerfile.read()
        )


def test_changed_supervisor_bytes_cannot_reuse_reviewed_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import resources
    from types import SimpleNamespace

    trusted = resources.files("kodi_lab.runtime")

    class Package:
        def joinpath(self, *parts: str) -> Any:
            if parts == ("assembly_assets", "graceful_supervisor_v1.py"):
                return SimpleNamespace(
                    read_bytes=lambda: SUPERVISOR.read_bytes() + b"\n"
                )
            return trusted.joinpath(*parts)

    monkeypatch.setattr(resources, "files", lambda _: Package())
    with pytest.raises(ValueError, match="asset differs"):
        load_reviewed_recipe("omega")


@pytest.mark.parametrize(
    "drift", ["executable", "name", "group", "uid", "ambiguous", "kernel"]
)
def test_signal_fallback_rejects_unverified_main_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from types import SimpleNamespace
    from typing import cast

    helper = module()
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in [123, 124] if drift == "ambiguous" else [123]:
        entry = proc / str(pid)
        entry.mkdir()
        (entry / "comm").write_text("forged" if drift == "name" else "kodi.bin")
        (entry / "exe").symlink_to(
            "/unrelated" if drift == "executable" else "/usr/lib/kodi/kodi.bin"
        )
    monkeypatch.setattr(
        helper, "Path", lambda value: proc if value == "/proc" else Path(value)
    )
    calls = 0
    closed: list[int] = []
    sent: list[tuple[int, int]] = []

    def group(pid: int) -> int:
        nonlocal calls
        calls += 1
        return 777 if drift == "group" and calls > 1 else 481

    def open_pid(pid: int) -> int:
        if drift == "kernel":
            raise OSError("unsupported")
        return pid + 1000

    monkeypatch.setattr(helper.os, "getpgid", group)
    monkeypatch.setattr(helper.os, "pidfd_open", open_pid, raising=False)
    monkeypatch.setattr(helper.os, "close", closed.append)
    if drift == "uid":
        owner = os.geteuid()
        monkeypatch.setattr(helper.os, "geteuid", lambda: owner + 1)
    monkeypatch.setattr(
        helper.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
        raising=False,
    )
    assert helper.signal_kodi(cast(Any, SimpleNamespace(pid=481))) is False
    assert not sent
    if drift != "kernel":
        assert sorted(closed) == ([1123, 1124] if drift == "ambiguous" else [1123])

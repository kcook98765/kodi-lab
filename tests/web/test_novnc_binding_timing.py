"""Bounded timing is observational and survives interrupted caller waits."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from kodi_lab import viewer_diagnostics as diagnostics
from kodi_lab.web.novnc import PrototypeViewer

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Any:
    logger = logging.getLogger(diagnostics.LOGGER)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    caplog.set_level(logging.INFO, logger=diagnostics.LOGGER)
    return lambda: [
        json.loads(r.message) for r in caplog.records if r.name == diagnostics.LOGGER
    ]


@pytest.mark.parametrize(
    "phase",
    [
        "capability_before",
        "broker_lookup",
        "operation_reconciliation",
        "runtime_profile",
        "development_binding",
        "ownership",
    ],
)
@pytest.mark.parametrize("fails", [False, True])
def test_slow_phase_elapsed_cpu_and_queue_are_separate_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, records: Any, phase: str, fails: bool
) -> None:
    elapsed = 10.0
    cpu = 1_000_000_000
    monkeypatch.setattr(
        diagnostics,
        "time",
        SimpleNamespace(
            monotonic=lambda: elapsed,
            monotonic_ns=lambda: int(elapsed * 1e9),
            thread_time_ns=lambda: cpu,
            time=time.time,
        ),
    )
    try:
        with (
            diagnostics.binding_timing("web_watchdog", INSTANCE_ID, queued_at=9.5),
            diagnostics.binding_phase(phase),
        ):
            elapsed += 2
            cpu += 50_000_000
            if fails:
                raise ValueError("secret-path-token-sql")
    except ValueError:
        assert fails
    [entry] = records()
    assert entry["queue_ms"] == 500
    assert entry["elapsed_ms"] == 2500 and entry["worker_ms"] == 2000
    assert entry["worker_cpu_ms"] == 50
    assert entry["phase_ms"] == {phase: 2000}
    assert entry["phase_cpu_ms"] == {phase: 50}
    assert entry["phase_calls"] == {phase: 1}
    assert entry["reason"] == ("internal_error" if fails else "task_completed")
    assert "secret" not in json.dumps(entry)
    # Fast calls and contexts outside a timed worker emit nothing.
    with (
        diagnostics.binding_timing("web_watchdog", INSTANCE_ID),
        diagnostics.binding_phase("broker_lookup"),
    ):
        pass
    assert len(records()) == 1


def test_late_worker_retains_complete_timing_and_capacity_after_timeout(
    monkeypatch: pytest.MonkeyPatch, records: Any
) -> None:
    monkeypatch.setattr(diagnostics, "SLOW_BINDING_SECONDS", 0)
    entered, release, finished = Event(), Event(), Event()
    cancelled = Event()
    viewer = PrototypeViewer(Path("/unused"), lambda _: BINDING)
    grants: list[bool] = []

    def worker() -> bool:
        try:
            with diagnostics.binding_timing("web_watchdog", INSTANCE_ID):
                with diagnostics.binding_phase("broker_lookup"):
                    entered.set()
                    assert release.wait(3)
                with diagnostics.binding_phase("ownership"):
                    pass
                return True
        finally:
            finished.set()

    async def run() -> None:
        async def caller() -> None:
            grants.append(
                await viewer.checked(worker, timeout=0.05, cancelled=cancelled)
            )

        task = asyncio.create_task(caller())
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(TimeoutError):
            await task
        cancelled.set()
        assert records() == [] and grants == []
        # The timed-out reader still occupies its slot until its real completion.
        assert viewer.checks.acquire(blocking=False)
        assert not viewer.checks.acquire(blocking=False)
        viewer.checks.release()
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.get_running_loop().shutdown_default_executor()
        assert grants == []
        assert viewer.checks.acquire(blocking=False)
        assert viewer.checks.acquire(blocking=False)
        viewer.checks.release()
        viewer.checks.release()

    try:
        asyncio.run(run())
    finally:
        release.set()
    [entry] = records()
    assert entry["event"] == "binding_checked"
    assert entry["phase_calls"] == {"broker_lookup": 1, "ownership": 1}


def test_timing_fields_reject_arbitrary_values_and_logging_failure(
    monkeypatch: pytest.MonkeyPatch, records: Any
) -> None:
    diagnostics.diagnostic(
        "binding_checked",
        binding_origin="secret",
        phase_ms={
            "secret": 1,
            "broker_lookup": 12,
            "ownership": "secret",
            "auth": -1,
            "binding": True,
        },
    )
    [entry] = records()
    assert entry["phase_ms"] == {"broker_lookup": 12}
    assert "secret" not in json.dumps(entry)
    monkeypatch.setattr(diagnostics, "SLOW_BINDING_SECONDS", 0)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("secret")

    monkeypatch.setattr(logging.getLogger(diagnostics.LOGGER), "info", broken)
    with diagnostics.binding_timing("web_watchdog", INSTANCE_ID):
        pass

from __future__ import annotations

import json
import logging
import stat
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab import viewer_diagnostics as diagnostics


def test_records_are_closed_schema_and_files_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = logging.getLogger(diagnostics.LOGGER)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        "kodi_lab.viewer_diagnostics.tempfile.mkdtemp", lambda **_: str(directory)
    )
    diagnostics.configure_diagnostics()
    try:
        diagnostics.configure_diagnostics()
        assert len(logger.handlers) == 1
        instance, attempt = uuid4(), uuid4()
        for _ in range(800):
            diagnostics.diagnostic(
                "helper_ended",
                instance=instance,
                attempt=attempt,
                reason="exec_eof",
                exit_code=1,
                armed=False,
                terminated=True,
                startup_ms=10,
                elapsed_ms=2000,
                bytes_down=12,
                validation_ms=20,
                iteration_gap_ms=30,
                web_renew_gap_ms=40,
                ticket="ticket-secret",
                stderr=b"stderr-secret",
                cookies="cookie-secret",
                image="image-secret",
                headers={"authorization": "auth-secret"},
            )
        # Even recognized field names cannot smuggle arbitrary strings/numbers.
        diagnostics.diagnostic(
            "web_ended",
            reason="reason-secret",
            phase="phase-secret",
            watchdog_phase="phase-secret",
            watchdog_check_ms=True,
            watchdog_gap_ms=-1,
            watchdog_write_gap_ms="gap-secret",
            gc_pause_ms=10**13,
            lock_ms="lock-secret",
            update_ms=True,
            commit_ms=-1,
            ws_code=4007,
            bytes_up=-1,
            exit_code=True,
            attempt="attempt-secret",  # type: ignore[arg-type]
        )
        diagnostics.diagnostic("event-secret")
        handler = logger.handlers[0]
        handler.flush()
        files = sorted(directory.iterdir())
        assert len(files) == 2
        assert all(path.stat().st_size <= diagnostics.MAX_BYTES for path in files)
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        text = "\n".join(path.read_text() for path in files)
        assert "secret" not in text
        records = [json.loads(line) for line in text.splitlines() if line]
        assert len(records) < 800
        normal = next(row for row in records if row["event"] == "helper_ended")
        assert normal["instance"] == str(instance) and normal["attempt"] == str(attempt)
        assert normal["exit_code"] == 1 and normal["elapsed_ms"] == 2000
        assert normal["validation_ms"] == 20
        assert normal["iteration_gap_ms"] == 30
        assert normal["web_renew_gap_ms"] == 40
        assert all(len(line) < 1024 for line in text.splitlines())
        malformed = next(row for row in records if row["event"] == "web_ended")
        assert set(malformed) == {"event", "at_ms"}
    finally:
        for handler in logger.handlers:
            handler.close()


def test_unavailable_diagnostic_storage_does_not_break_viewing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger(diagnostics.LOGGER)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)

    def denied(**kwargs: object) -> str:
        raise OSError("storage-secret")

    monkeypatch.setattr("kodi_lab.viewer_diagnostics.tempfile.mkdtemp", denied)
    diagnostics.configure_diagnostics()
    assert isinstance(logger.handlers[0], logging.NullHandler)
    diagnostics.diagnostic("helper_ended", reason="exec_eof")


@pytest.mark.parametrize("failure", ["write", "rotation"])
def test_handler_failure_never_exposes_exception_details_or_changes_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    logger = logging.getLogger(diagnostics.LOGGER)
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.INFO)
    # Exercise the handler's own error path even with Python's debugging enabled.
    monkeypatch.setattr(logging, "raiseExceptions", True)
    monkeypatch.setattr(diagnostics, "SLOW_BINDING_SECONDS", 0)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(
        "kodi_lab.viewer_diagnostics.tempfile.mkdtemp", lambda **_: str(directory)
    )
    diagnostics.configure_diagnostics()
    handler = logger.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    failures = 0

    def broken(*args: object, **kwargs: object) -> None:
        nonlocal failures
        failures += 1
        raise OSError("handler-secret-private-path")

    def check(rejected: bool) -> bool:
        with (
            diagnostics.binding_timing("web_watchdog", uuid4()),
            diagnostics.binding_phase("ownership"),
        ):
            if rejected:
                raise ValueError("worker-secret")
            return True

    try:
        diagnostics.diagnostic("helper_started")
        handler.flush()
        before = (directory / "events.jsonl").read_text()
        with monkeypatch.context() as patch:
            if failure == "rotation":
                patch.setattr(handler, "maxBytes", 1)
                patch.setattr(handler, "rotate", broken)
            else:
                assert handler.stream is not None
                patch.setattr(handler.stream, "write", broken)
            assert check(False) is True
            with pytest.raises(ValueError, match="worker-secret"):
                check(True)
        assert failures == 2
        assert capsys.readouterr() == ("", "")
        assert (directory / "events.jsonl").read_text() == before
        assert check(False) is True
        handler.flush()
        entries = [
            json.loads(line)
            for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        assert [entry["event"] for entry in entries] == [
            "helper_started",
            "binding_checked",
        ]
        assert entries[-1]["phase_calls"] == {"ownership": 1}
        assert "secret" not in json.dumps(entries)
    finally:
        handler.close()


def test_web_log_configuration_preserves_private_diagnostic_handler(
    tmp_path: Path,
) -> None:
    # Exercise uvicorn's actual dictConfig ordering in an isolated process.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import logging.config, pathlib, sys, tempfile",
                    "tempfile.tempdir = sys.argv[1]",
                    "from kodi_lab.viewer_diagnostics import "
                    "configure_diagnostics, diagnostic",
                    "from kodi_lab.web.run import application_logging_config",
                    "configure_diagnostics()",
                    "logging.config.dictConfig(application_logging_config())",
                    "diagnostic('web_ended', reason='browser_disconnect')",
                    "print(next(pathlib.Path(sys.argv[1]).glob('*/events.jsonl')).read_text())",
                )
            ),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stderr == ""
    record = json.loads(completed.stdout)
    assert record["event"] == "web_ended" and record["reason"] == "browser_disconnect"

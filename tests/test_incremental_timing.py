"""Incremental reports survive termination and keep sensitive values out."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.summarize_timing import summarize_journal
from scripts.test_timing import safe_id
from tests.test_test_efficiency import run_probe


def journal(*, finish: bool = True, total: int = 4) -> bytes:
    events: list[dict[str, Any]] = [
        {
            "event": "session_start",
            "elapsed_seconds": 0,
            "source": {"commit": "a" * 40},
        },
        {"event": "collection", "elapsed_seconds": 1, "collected": total},
    ]
    for ordinal in range(1, 5):
        events += [
            {
                "event": "case_start",
                "elapsed_seconds": ordinal * 2 - 1,
                "nodeid": f"tests/probe.py::test_{ordinal}",
                "ordinal": ordinal,
            },
            {
                "event": "case_complete",
                "elapsed_seconds": ordinal * 2,
                "nodeid": f"tests/probe.py::test_{ordinal}",
                "ordinal": ordinal,
                "phases": {
                    p: {"outcome": "passed", "seconds": s}
                    for p, s in (("setup", 0.1), ("call", 0.5), ("teardown", 0.1))
                },
            },
        ]
    if finish:
        events.append(
            {"event": "session_finish", "elapsed_seconds": 9, "exit_status": 0}
        )
    return b"".join(
        (json.dumps({"schema_version": 1, "run_id": "probe", **e}) + "\n").encode()
        for e in events
    )


def test_quartiles_use_collected_count_and_actual_elapsed_time() -> None:
    result = summarize_journal(journal())
    assert result["classification"] == "complete"
    assert result["pytest_exit_status"] == 0
    assert result["recorded_cases"] == 4
    assert result["total_phase_seconds"] == pytest.approx(2.8)
    assert result["elapsed_seconds"] == 9
    assert result["unaccounted_seconds"] == pytest.approx(6.2)
    assert [s["cumulative_elapsed_seconds"] for s in result["progress_segments"]] == [
        2,
        4,
        6,
        8,
        8,
        8,
    ]
    assert result["files"][0]["case_wall_seconds"] == 4


def test_incomplete_and_torn_append_never_become_complete() -> None:
    result = summarize_journal(journal(finish=False, total=8) + b'{"partial":')
    assert result["classification"] == "incomplete"
    assert result["pytest_exit_status"] is None
    assert result["missing_cases"] == 4
    assert result["torn_final_record"] is True
    assert result["progress_segments"][2]["cumulative_elapsed_seconds"] is None
    assert result["last_completed_case"]["ordinal"] == 4


@pytest.mark.parametrize(
    "defect", ["identity", "order", "duration", "ordinal", "malformed"]
)
def test_invalid_journal_is_rejected(defect: str) -> None:
    lines = [json.loads(line) for line in journal().splitlines()]
    if defect == "identity":
        lines[3]["run_id"] = "different"
    elif defect == "order":
        lines[4]["elapsed_seconds"] = 0
    elif defect == "duration":
        lines[3]["phases"]["call"]["seconds"] = float("nan")
    elif defect == "ordinal":
        lines[2]["ordinal"] = 2
    else:
        lines[2]["event"] = "unknown"
    with pytest.raises(ValueError):
        summarize_journal(
            b"".join((json.dumps(line) + "\n").encode() for line in lines)
        )


def test_real_report_preserves_phases_and_redacts_parameters(tmp_path: Path) -> None:
    result = run_probe(
        tmp_path,
        """
import pytest
@pytest.mark.parametrize("password", ["sentinel-secret"])
def test_value(password):
    assert password
""",
    )
    body = (tmp_path / "result.timing.jsonl").read_bytes()
    assert b"sentinel-secret" not in body
    summary = summarize_journal(body)
    assert summary["classification"] == "complete"
    assert summary["outcomes"] == {"passed": 1}
    assert summary["phase_seconds"] == {
        p: next(iter(cast(dict[str, Any], result["tests"]).values()))["phases"][p][
            "seconds"
        ]
        for p in ("setup", "call", "teardown")
    }
    assert (tmp_path / "result.timing.jsonl").stat().st_mode & 0o777 == 0o600
    assert safe_id("../../private/value::test") == "redacted"
    assert safe_id("/private/value::test") == "redacted"


def test_terminated_pytest_retains_completed_prefix_and_active_case(
    tmp_path: Path,
) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import time\ndef test_one(): pass\ndef test_two(): time.sleep(60)\n"
    )
    report = tmp_path / "result.json"
    sidecar = report.with_suffix(".timing.jsonl")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.efficiency_plugin",
            "--harness-report",
            str(report),
            str(probe),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if sidecar.exists() and b'"ordinal":2' in sidecar.read_bytes():
                break
            if process.poll() is not None:
                pytest.fail(
                    str(process.stderr.read() if process.stderr else "early exit")
                )
            time.sleep(0.02)
        else:
            pytest.fail("pytest did not reach the second case")
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert not report.exists()
    summary = summarize_journal(sidecar.read_bytes())
    assert summary["classification"] == "incomplete"
    assert summary["recorded_cases"] == 1
    assert summary["active_case"]["ordinal"] == 2
    assert summary["pytest_exit_status"] is None


def test_repeated_collection_cannot_shrink_original_denominator() -> None:
    lines = journal().splitlines(keepends=True)
    duplicate = json.loads(lines[1])
    duplicate["collected"] = 8
    lines.insert(1, (json.dumps(duplicate) + "\n").encode())
    with pytest.raises(ValueError, match="repeated collection"):
        summarize_journal(b"".join(lines))


def test_redacted_case_still_has_file_wall_attribution() -> None:
    events = [json.loads(line) for line in journal().splitlines()]
    for event in events:
        if "nodeid" in event:
            event["nodeid"] = "redacted"
    result = summarize_journal(
        b"".join((json.dumps(e) + "\n").encode() for e in events)
    )
    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "redacted"
    assert result["files"][0]["cases"] == 4
    assert result["files"][0]["case_wall_seconds"] == 4


def test_untracked_source_identity_is_explicitly_incomplete(tmp_path: Path) -> None:
    from scripts.test_timing import source_identity

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True)

    git("init")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    assert source_identity(tmp_path)["identity_complete"] is True
    (tmp_path / "implementation.py").write_text("changed implementation")
    identity = source_identity(tmp_path)
    assert identity["untracked_files_present"] is True
    assert identity["identity_complete"] is False


def test_cli_recovers_timeout_from_companion_stage_evidence(tmp_path: Path) -> None:
    path = tmp_path / "pytest.timing.jsonl"
    path.write_bytes(journal(finish=False, total=8))
    (tmp_path / "stages.tsv").write_text("pytest\t2400\t124\n")
    result = subprocess.run(
        [sys.executable, "-m", "scripts.summarize_test_report", str(path), "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["classification"] == "incomplete"
    assert summary["pytest_exit_status"] is None
    assert summary["companion_pytest_stage_outcome"] == "watchdog_timeout"


def test_text_summary_identifies_active_case(tmp_path: Path) -> None:
    path = tmp_path / "pytest.timing.jsonl"
    lines = journal(finish=False, total=8).splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-1]))
    result = subprocess.run(
        [sys.executable, "-m", "scripts.summarize_test_report", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    active_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Active case")
    )
    assert "test_4" in active_line
    assert "'ordinal': 4" in active_line
    assert "Journal: incomplete" in result.stdout


def test_missing_passed_phase_prevents_complete_classification() -> None:
    events = [json.loads(line) for line in journal().splitlines()]
    del events[3]["phases"]["call"]
    result = summarize_journal(
        b"".join((json.dumps(e) + "\n").encode() for e in events)
    )
    assert result["classification"] == "incomplete"
    assert result["outcomes"]["incomplete"] == 1

"""Retained timing reports include failure and incomplete-case evidence."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.summarize_test_report import summarize

ROOT = Path(__file__).resolve().parents[1]


def report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "exit_status": 0,
        "tests": {
            "tests/fast.py::test_one": {
                "phases": {
                    "setup": {"outcome": "passed", "seconds": 0.5},
                    "call": {"outcome": "passed", "seconds": 1.0},
                    "teardown": {"outcome": "passed", "seconds": 0.25},
                }
            },
            "tests/slow.py::test_two": {
                "phases": {
                    "setup": {"outcome": "passed", "seconds": 4.0},
                    "call": {"outcome": "passed", "seconds": 2.0},
                    "teardown": {"outcome": "passed", "seconds": 0.25},
                }
            },
        },
    }


def test_grouped_phase_costs_and_totals_preserve_input() -> None:
    document = report()
    document["tests"]["tests/slow.py::test_three"] = copy.deepcopy(
        document["tests"]["tests/slow.py::test_two"]
    )
    expected = copy.deepcopy(document)
    result = summarize(document)
    assert result["recorded_cases"] == 3
    assert result["outcomes"] == {"passed": 3}
    assert result["phase_seconds"] == {"setup": 8.5, "call": 5.0, "teardown": 0.75}
    assert result["total_phase_seconds"] == 14.25
    assert [item["path"] for item in result["files"]] == [
        "tests/slow.py",
        "tests/fast.py",
    ]
    assert result["files"][0]["cases"] == 2
    assert document == expected


def test_failed_skipped_and_incomplete_cases_remain_visible() -> None:
    document = report()
    document["exit_status"] = 2
    cases = document["tests"]
    cases["tests/fast.py::test_one"]["phases"]["teardown"]["outcome"] = "failed"
    del cases["tests/slow.py::test_two"]["phases"]["teardown"]
    cases["tests/skip.py::test_skip"] = {
        "phases": {"setup": {"outcome": "skipped", "seconds": 0.1}}
    }
    result = summarize(document)
    assert result["pytest_exit_status"] == 2
    assert result["outcomes"] == {"failed": 1, "incomplete": 1, "skipped": 1}
    assert "release-stage status is separate" in result["scope"]
    assert result["files"][0]["outcomes"] == {"incomplete": 1}


@pytest.mark.parametrize(
    "duration", [None, -1, float("nan"), float("inf"), "1", True, 10**1000]
)
def test_invalid_phase_durations_are_rejected(duration: Any) -> None:
    document = report()
    document["tests"]["tests/fast.py::test_one"]["phases"]["call"]["seconds"] = duration
    with pytest.raises(ValueError, match="duration"):
        summarize(document)


def test_overflowing_phase_totals_are_rejected() -> None:
    document = report()
    for case in document["tests"].values():
        for phase in case["phases"].values():
            phase["seconds"] = 1e308
    with pytest.raises(ValueError, match="aggregate"):
        summarize(document)


@pytest.mark.parametrize("defect", ["schema", "status", "tests", "phase", "outcome"])
def test_malformed_report_does_not_become_a_success_summary(defect: str) -> None:
    document = report()
    if defect == "schema":
        document["schema_version"] = True
    elif defect == "status":
        document["exit_status"] = True
    elif defect == "tests":
        document["tests"] = []
    elif defect == "phase":
        document["tests"]["tests/fast.py::test_one"]["phases"]["unrecognized"] = {}
    else:
        document["tests"]["tests/fast.py::test_one"]["phases"]["call"]["outcome"] = "ok"
    with pytest.raises(ValueError):
        summarize(document)


def test_cli_limits_file_ranking_without_dropping_totals_or_changing_report(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pytest.json"
    body = json.dumps(report())
    path.write_text(body)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.summarize_test_report",
            str(path),
            "--json",
            "--limit",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert len(result["files"]) == 1
    assert result["files"][0]["path"] == "tests/slow.py"
    assert result["recorded_cases"] == 2
    assert result["total_phase_seconds"] == 8.0
    assert path.read_text() == body


@pytest.mark.parametrize("bad_limit", ["0", "1001", "unknown"])
def test_cli_rejects_invalid_limits(tmp_path: Path, bad_limit: str) -> None:
    path = tmp_path / "pytest.json"
    path.write_text(json.dumps(report()))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.summarize_test_report",
            str(path),
            "--limit",
            bad_limit,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 2
    assert not completed.stdout

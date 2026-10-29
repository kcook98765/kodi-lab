"""Rank recorded pytest phase costs without executing or selecting any tests."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_PHASES = frozenset({"setup", "call", "teardown"})
_OUTCOMES = frozenset({"passed", "failed", "skipped"})
_MAX_REPORT_BYTES = 64 * 1024 * 1024


def summarize(document: object) -> dict[str, Any]:
    """Keep missing phases and unsuccessful pytest status visible in the summary."""
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
    ):
        raise ValueError("expected Kodi Lab harness report schema_version 1")
    exit_status = document.get("exit_status")
    if type(exit_status) is not int or not 0 <= exit_status <= 255:
        raise ValueError("report must contain an integer pytest exit_status")
    cases = document.get("tests")
    if not isinstance(cases, dict):
        raise ValueError("report must contain a tests object")
    phases: defaultdict[str, float] = defaultdict(float)
    outcomes: Counter[str] = Counter()
    files: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "cases": 0,
            "outcomes": Counter(),
            "phase_seconds": defaultdict(float),
        }
    )
    for nodeid, case in cases.items():
        if not isinstance(nodeid, str) or not nodeid or not isinstance(case, dict):
            raise ValueError("invalid test case entry")
        recorded = case.get("phases")
        if not isinstance(recorded, dict) or not recorded:
            raise ValueError("test case must contain recorded phases")
        observed: list[str] = []
        file = files[nodeid.split("::", 1)[0]]
        file["cases"] += 1
        for phase, detail in recorded.items():
            if phase not in _PHASES or not isinstance(detail, dict):
                raise ValueError("invalid test phase")
            outcome, seconds = detail.get("outcome"), detail.get("seconds")
            if not isinstance(outcome, str) or outcome not in _OUTCOMES:
                raise ValueError("invalid test phase outcome")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("phase duration must be finite and nonnegative")
            try:
                seconds = float(seconds)
            except OverflowError as error:
                raise ValueError("phase duration is too large") from error
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("phase duration must be finite and nonnegative")
            observed.append(outcome)
            phases[phase] += seconds
            file["phase_seconds"][phase] += seconds
        outcome = (
            "failed"
            if "failed" in observed
            else "skipped"
            if "skipped" in observed
            else "passed"
            if recorded.keys() == _PHASES
            else "incomplete"
        )
        outcomes[outcome] += 1
        file["outcomes"][outcome] += 1
    if not math.isfinite(sum(phases.values())):
        raise ValueError("aggregate phase duration is too large")
    ranked = [
        {
            "path": path,
            "cases": data["cases"],
            "outcomes": dict(data["outcomes"]),
            "phase_seconds": dict(data["phase_seconds"]),
            "total_phase_seconds": sum(data["phase_seconds"].values()),
        }
        for path, data in files.items()
    ]
    ranked.sort(key=lambda data: (-data["total_phase_seconds"], data["path"]))
    return {
        "schema_version": 1,
        "scope": "recorded pytest phases; release-stage status is separate",
        "pytest_exit_status": exit_status,
        "recorded_cases": len(cases),
        "outcomes": dict(outcomes),
        "phase_seconds": dict(phases),
        "total_phase_seconds": sum(phases.values()),
        "files": ranked,
    }


def _limit(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report", type=Path, help="retained pytest.json or .timing.jsonl"
    )
    parser.add_argument("--limit", type=_limit, default=10)
    parser.add_argument("--json", action="store_true", help="emit a structured summary")
    args = parser.parse_args()
    try:
        with args.report.open("rb") as stream:
            body = stream.read(_MAX_REPORT_BYTES + 1)
        if len(body) > _MAX_REPORT_BYTES:
            raise ValueError("harness report exceeds 64 MiB")
        if args.report.suffix == ".jsonl":
            from scripts.summarize_timing import summarize_journal

            result = summarize_journal(body)
            stages = args.report.parent / "stages.tsv"
            if args.report.name == "pytest.timing.jsonl" and stages.is_file():
                with stages.open("rb") as stream:
                    stage_body = stream.read(65537)
                if len(stage_body) > 65536:
                    raise ValueError("stage report exceeds 64 KiB")
                rows = [line.split("\t") for line in stage_body.decode().splitlines()]
                pytest_rows = [row for row in rows if row[0] == "pytest"]
                if len(pytest_rows) > 1:
                    raise ValueError("duplicate pytest stage status")
                if pytest_rows:
                    row = pytest_rows[0]
                    if len(row) != 3 or not all(x.isdecimal() for x in row[1:]):
                        raise ValueError("invalid pytest stage status")
                    result["companion_pytest_stage_status"] = int(row[2])
                    result["companion_pytest_stage_outcome"] = (
                        "watchdog_timeout"
                        if row[2] == "124"
                        else "forced_kill_cause_unknown"
                        if row[2] == "137"
                        else "passed"
                        if row[2] == "0"
                        else "failed"
                    )
        else:
            result = summarize(json.loads(body))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        result["files"] = result["files"][: args.limit]
        print(json.dumps(result, indent=2, allow_nan=False))
        return
    print(result["scope"])
    print(
        f"pytest exit status: {result['pytest_exit_status']}; "
        f"recorded cases: {result['recorded_cases']}; "
        f"outcomes: {json.dumps(result['outcomes'], sort_keys=True)}"
    )
    print("Phase sums are measured test phases, not elapsed lane or release wall time.")
    if "classification" in result:
        print(
            f"Journal: {result['classification']}; "
            f"collected: {result['collected_cases']}; "
            f"missing: {result['missing_cases']}; "
            f"last completion: {result['last_completed_case']}"
        )
        print(f"Active case at last record: {result['active_case']}")
        print(
            f"Last recorded session elapsed (lower bound if incomplete): "
            f"{result['elapsed_seconds']:.2f}s; "
            f"not accounted for by completed-case phases: "
            f"{result['unaccounted_seconds']:.2f}s "
            "(includes unrecorded active phases in incomplete runs)"
        )
        print(f"Source identity: {json.dumps(result['source'], sort_keys=True)}")
        print(
            f"ORM compilation cleanup: {json.dumps(result['orm_compilation_cleanup'])}"
        )
        if "companion_pytest_stage_outcome" in result:
            print(f"Companion pytest stage: {result['companion_pytest_stage_outcome']}")
        for segment in result["progress_segments"]:
            print(
                f"Progress through {segment['progress_end']:.0%}: "
                f"cumulative elapsed {segment['cumulative_elapsed_seconds']}; "
                f"segment elapsed {segment['elapsed_seconds']}; "
                f"complete={segment['complete']}"
            )
        print("Slow fixture setups >=50ms (may include nested dynamic dependencies):")
        for fixture in result["fixtures"][: args.limit]:
            print(
                f"{fixture['seconds']:.2f}s / {fixture['count']} setups: "
                f"{fixture['fixture']}"
            )
    print("   total    setup     call teardown  cases  file")
    for entry in result["files"][: args.limit]:
        phases = entry["phase_seconds"]
        print(
            f"{entry['total_phase_seconds']:8.2f} "
            f"{phases.get('setup', 0):8.2f} {phases.get('call', 0):8.2f} "
            f"{phases.get('teardown', 0):8.2f} {entry['cases']:6d}  {entry['path']}"
        )


if __name__ == "__main__":
    main()

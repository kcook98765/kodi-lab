"""Validate and summarize complete or interrupted sequential timing journals."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any

from scripts.summarize_test_report import summarize


def seconds(value: Any) -> float:
    if type(value) not in {int, float}:
        raise ValueError("timing duration must be finite and nonnegative")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("timing duration is too large") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError("timing duration must be finite and nonnegative")
    return result


def summarize_journal(body: bytes) -> dict[str, Any]:
    lines = body.splitlines(keepends=True)
    truncated = bool(lines and not lines[-1].endswith(b"\n"))
    if truncated:
        lines.pop()  # Only a torn final append is recoverable.
    events = [json.loads(line) for line in lines]
    if (
        not events
        or not isinstance(events[0], dict)
        or events[0].get("event") != "session_start"
    ):
        raise ValueError("journal must start with session_start")
    run = events[0].get("run_id")
    if not isinstance(run, str) or not run:
        raise ValueError("journal run identity is missing")
    collected = None
    status = None
    finished = False
    previous = 0.0
    active: dict[str, Any] | None = None
    completed: list[dict[str, Any]] = []
    fixtures: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "seconds": 0.0, "max_seconds": 0.0}
    )
    cases = {}
    for index, event in enumerate(events):
        if (
            not isinstance(event, dict)
            or type(event.get("schema_version")) is not int
            or event["schema_version"] != 1
            or event.get("run_id") != run
            or finished
        ):
            raise ValueError("invalid journal schema, run identity or event order")
        elapsed = seconds(event.get("elapsed_seconds"))
        if elapsed < previous:
            raise ValueError("journal elapsed time moved backwards")
        previous = elapsed
        kind = event.get("event")
        if kind == "session_start":
            if index != 0 or not isinstance(event.get("source"), dict):
                raise ValueError("invalid session source identity")
        elif kind == "collection":
            if collected is not None:
                raise ValueError("repeated collection record")
            collected = event.get("collected")
            if type(collected) is not int or collected < 0 or completed or active:
                raise ValueError("invalid collected test count")
        elif kind == "case_start":
            if (
                collected is None
                or active is not None
                or type(event.get("ordinal")) is not int
                or event["ordinal"] != len(completed) + 1
                or event["ordinal"] > collected
                or not isinstance(event.get("nodeid"), str)
            ):
                raise ValueError("invalid sequential case start")
            active = event
        elif kind == "case_complete":
            if active is None or any(
                event.get(key) != active[key] for key in ("ordinal", "nodeid")
            ):
                raise ValueError("case completion does not match its start")
            event["wall_seconds"] = elapsed - active["elapsed_seconds"]
            completed.append(event)
            identity = event["nodeid"]
            if "::" not in identity:
                identity += "::case"
            cases[f"{identity}[{event['ordinal']}]"] = {"phases": event.get("phases")}
            active = None
        elif kind == "fixture_setup":
            name, scope = event.get("name"), event.get("scope")
            if (
                not isinstance(name, str)
                or not isinstance(scope, str)
                or scope
                not in {
                    "function",
                    "class",
                    "module",
                    "package",
                    "session",
                }
            ):
                raise ValueError("invalid fixture timing")
            duration = seconds(event.get("seconds"))
            entry = fixtures[f"{scope}:{name}"]
            entry["count"] += 1
            entry["seconds"] += duration
            entry["max_seconds"] = max(entry["max_seconds"], duration)
            seconds(entry["seconds"])
        elif kind == "session_finish":
            status = event.get("exit_status")
            if type(status) is not int or not 0 <= status <= 255:
                raise ValueError("invalid pytest exit status")
            finished = True
        else:
            raise ValueError("unknown journal event")
    result = summarize(
        {"schema_version": 1, "exit_status": status or 0, "tests": cases}
    )
    result["pytest_exit_status"] = status
    result.update(
        run_id=run,
        source=events[0]["source"],
        scope="sequential session-relative timing; release-stage status is separate",
        classification=(
            "complete"
            if finished
            and collected == len(completed)
            and active is None
            and not truncated
            and not result["outcomes"].get("incomplete")
            else "incomplete"
        ),
        collected_cases=collected,
        missing_cases=None if collected is None else collected - len(completed),
        torn_final_record=truncated,
        last_event_seconds=previous,
        last_completed_case=(
            {
                key: completed[-1][key]
                for key in ("ordinal", "nodeid", "elapsed_seconds")
            }
            if completed
            else None
        ),
        active_case=active,
        elapsed_seconds=previous,
        unaccounted_seconds=previous - result["total_phase_seconds"],
        fixtures=[
            {"fixture": name, **data}
            for name, data in sorted(fixtures.items(), key=lambda x: -x[1]["seconds"])
        ],
    )
    # Boundaries use the original collected count, never the completed prefix.
    segments = []
    low = 0
    start: float | None = 0.0
    if collected:
        for fraction in (0.25, 0.5, 0.75, 0.9, 0.95, 1.0):
            high = math.ceil(collected * fraction)
            available = completed[low:high]
            reached = high <= len(completed)
            phase = {
                key: sum(e["phases"].get(key, {}).get("seconds", 0) for e in available)
                for key in ("setup", "call", "teardown")
            }
            end = completed[high - 1]["elapsed_seconds"] if reached else None
            segments.append(
                {
                    "progress_end": fraction,
                    "first_ordinal": low + 1,
                    "last_ordinal": high,
                    "completed_cases": len(available),
                    "complete": reached,
                    "cumulative_elapsed_seconds": end,
                    "elapsed_seconds": end - start
                    if end is not None and start is not None
                    else None,
                    "phase_seconds": phase,
                    "unaccounted_seconds": end - start - sum(phase.values())
                    if end is not None and start is not None
                    else None,
                    "last_resources": available[-1].get("resources")
                    if available
                    else None,
                }
            )
            low, start = high, end
    result["progress_segments"] = segments
    cache_seconds = 0.0
    cache_entries = 0
    cache_retained = 0
    for event in completed:
        cleanup = event.get("orm_cache")
        if cleanup is None:
            continue
        if not isinstance(cleanup, dict):
            raise ValueError("invalid ORM compilation cleanup record")
        if cleanup.get("outcome") == "retained_workers_or_unknown":
            cache_retained += 1
        elif cleanup.get("outcome") == "reclaimed":
            entries = cleanup.get("entries")
            if type(entries) is not int or entries < 0:
                raise ValueError("invalid ORM compilation entry count")
            cache_seconds += seconds(cleanup.get("seconds"))
            seconds(cache_seconds)
            cache_entries += entries
        else:
            raise ValueError("invalid ORM compilation cleanup outcome")
    result["orm_compilation_cleanup"] = {
        "seconds": cache_seconds,
        "entries": cache_entries,
        "retained_workers_or_unknown_cases": cache_retained,
        "scope": "post-teardown cleanup; included in unaccounted elapsed time",
    }
    file_wall: dict[str, float] = defaultdict(float)
    for event in completed:
        file_wall[event["nodeid"].split("::", 1)[0]] += event["wall_seconds"]
    for file in result["files"]:
        file["case_wall_seconds"] = file_wall[file["path"]]
    return result

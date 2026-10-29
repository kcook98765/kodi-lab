"""Reclaim successful case scratch only after teardown and worker quiescence."""

from __future__ import annotations

import json
import os
import resource
import threading
import time
from collections import Counter
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from scripts.test_timing import TimingJournal, safe_id
from tests.efficiency import (
    CurrentSchemaTemplate,
    OwnedScratch,
    reclaim_orm_compilations,
)

_SCRATCH: pytest.StashKey[OwnedScratch] = pytest.StashKey()
_REPORTS: pytest.StashKey[dict[str, dict[str, Any]]] = pytest.StashKey()
_METRICS: pytest.StashKey[dict[str, int]] = pytest.StashKey()
_TIMING: pytest.StashKey[TimingJournal] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Kodi Lab test resources")
    group.addoption("--keep-passed-tmp", action="store_true", default=False)
    group.addoption("--harness-report", default=None, metavar="PATH")


def pytest_sessionstart(session: pytest.Session) -> None:
    report = session.config.getoption("harness_report")
    if report:
        scratch = session.config.getoption("basetemp")
        session.config.stash[_TIMING] = TimingJournal(
            Path(report).with_suffix(".timing.jsonl"),
            session.config.rootpath,
            Path(scratch).parent if scratch else Path("/tmp"),
        )


def pytest_collection_finish(session: pytest.Session) -> None:
    journal = session.config.stash.get(_TIMING, None)
    if journal:
        journal.emit("collection", collected=len(session.items))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, bool | None, bool | None]:
    journal = item.config.stash.get(_TIMING, None)
    if journal:
        journal.start_case(item.nodeid)
    result = yield
    case = item.config.stash[_REPORTS].get(item.nodeid)
    if case is not None:
        metrics = item.config.stash[_METRICS]
        if _workers_alive():
            case["orm_cache"] = {"outcome": "retained_workers_or_unknown"}
            key = "orm_cache_retained_workers_or_unknown"
            metrics[key] = metrics.get(key, 0) + 1
        else:
            started = time.monotonic()
            removed = reclaim_orm_compilations()
            case["orm_cache"] = {
                "outcome": "reclaimed",
                "entries": removed,
                "seconds": time.monotonic() - started,
            }
            key = "orm_cache_entries_removed"
            metrics[key] = metrics.get(key, 0) + removed
    if journal:
        journal.complete_case(item.nodeid, item.config.stash[_REPORTS][item.nodeid])
    return result


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_REPORTS] = {}
    config.stash[_METRICS] = {"logical_bytes_removed": 0}
    config.addinivalue_line(
        "markers", "keep_scratch: retain this case's temporary files"
    )
    if config.getini("tmp_path_retention_policy") != "all":
        raise pytest.UsageError("Kodi Lab requires tmp_path_retention_policy=all")


@pytest.fixture(scope="session")
def current_schema(tmp_path_factory: pytest.TempPathFactory) -> CurrentSchemaTemplate:
    return CurrentSchemaTemplate(tmp_path_factory.mktemp("current-schema"))


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
) -> Generator[None, Any, Any]:
    started = time.monotonic()
    value = yield
    elapsed = time.monotonic() - started
    journal = request.config.stash.get(_TIMING, None)
    if journal and elapsed >= 0.05:
        journal.emit(
            "fixture_setup",
            name=safe_id(fixturedef.argname),
            scope=fixturedef.scope,
            seconds=elapsed,
            ordinal=journal.ordinal,
        )
    if (
        fixturedef.argname == "tmp_path"
        and fixturedef.func.__module__ == "_pytest.tmpdir"
        and fixturedef.scope == "function"
        and isinstance(value, Path)
    ):
        factory = request.getfixturevalue("tmp_path_factory")
        # Custom/replaced roots do not confer cleanup authority.
        with suppress(OSError, ValueError):
            request.node.stash[_SCRATCH] = OwnedScratch.capture(
                value, factory.getbasetemp()
            )
    return value


def _workers_alive() -> bool:
    if any(
        t.is_alive() and t is not threading.current_thread()
        for t in threading.enumerate()
    ):
        return True
    try:
        return bool(Path(f"/proc/self/task/{os.getpid()}/children").read_text().strip())
    except OSError:
        return True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    entries = item.config.stash[_REPORTS]
    case = entries.setdefault(item.nodeid, {"phases": {}})
    case["phases"][report.when] = {
        "outcome": report.outcome,
        "seconds": report.duration,
    }
    scratch = item.stash.get(_SCRATCH, None)
    metrics = item.config.stash[_METRICS]
    if scratch is not None:
        with suppress(OSError):
            available = os.statvfs(scratch.path.parent)
            free = available.f_bavail * available.f_frsize
            metrics["minimum_free_bytes"] = min(
                metrics.get("minimum_free_bytes", free), free
            )
    if report.when != "teardown" or scratch is None:
        return report
    case["scratch"] = str(scratch.path)
    if any(
        case["phases"].get(phase, {}).get("outcome") != "passed"
        for phase in ("setup", "call", "teardown")
    ):
        case["cleanup"] = "retained_failure_or_skip"
    elif item.config.getoption("keep_passed_tmp") or item.get_closest_marker(
        "keep_scratch"
    ):
        case["cleanup"] = "retained_explicit"
    elif _workers_alive():
        case["cleanup"] = "retained_workers"
    else:
        try:
            metrics["logical_bytes_removed"] += scratch.remove()
            case["cleanup"] = "removed"
        except (OSError, ValueError) as error:
            case["cleanup"] = "retained_unsafe_or_unavailable"
            case["cleanup_error"] = type(error).__name__
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    journal = session.config.stash.get(_TIMING, None)
    if journal:
        journal.finish(exitstatus)
    path = session.config.getoption("harness_report")
    if path:
        document = {
            "schema_version": 1,
            "exit_status": int(exitstatus),
            "pytest_version": pytest.__version__,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "metrics": session.config.stash[_METRICS],
            "tests": session.config.stash[_REPORTS],
        }
        Path(path).write_text(json.dumps(document, indent=2) + "\n")


def pytest_terminal_summary(terminalreporter: Any) -> None:
    counts = Counter(
        entry.get("cleanup", "no_scratch")
        for entry in terminalreporter.config.stash[_REPORTS].values()
    )
    terminalreporter.write_line(
        "scratch cleanup: "
        + ", ".join(f"{key}={count}" for key, count in sorted(counts.items()))
    )
    metrics = terminalreporter.config.stash[_METRICS]
    terminalreporter.write_line(
        "ORM compilation cache: "
        f"{metrics.get('orm_cache_entries_removed', 0)} entries reclaimed; "
        f"{metrics.get('orm_cache_retained_workers_or_unknown', 0)} "
        "cases retained with workers or unknown state"
    )

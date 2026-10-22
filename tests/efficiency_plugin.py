"""Reclaim successful case scratch only after teardown and worker quiescence."""

from __future__ import annotations

import json
import os
import resource
import threading
from collections import Counter
from collections.abc import Generator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from tests.efficiency import CurrentSchemaTemplate, OwnedScratch

_SCRATCH: pytest.StashKey[OwnedScratch] = pytest.StashKey()
_REPORTS: pytest.StashKey[dict[str, dict[str, Any]]] = pytest.StashKey()
_METRICS: pytest.StashKey[dict[str, int]] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("Kodi Lab test resources")
    group.addoption("--keep-passed-tmp", action="store_true", default=False)
    group.addoption("--harness-report", default=None, metavar="PATH")


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
    value = yield
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

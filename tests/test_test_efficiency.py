"""Fixture isolation and cleanup proof without running the application suite."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from tests.efficiency import CurrentSchemaTemplate, OwnedScratch

REPOSITORY = Path(__file__).resolve().parents[1]


def test_closed_schema_clones_do_not_share_mutations(
    tmp_path: Path, current_schema: CurrentSchemaTemplate
) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    current_schema.clone(first)
    current_schema.clone(second)
    with closing(sqlite3.connect(first)) as connection:
        connection.execute("CREATE TABLE private_probe (id INTEGER)")
        connection.commit()
    for path in (second, current_schema.path):
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            assert connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='private_probe'"
            ).fetchone() == (0,)
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert first != second


def test_template_checks_close_all_database_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def tracked(database: str, *, uri: bool) -> sqlite3.Connection:
        connection = connect(database, uri=uri)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked)
    template = CurrentSchemaTemplate(tmp_path)
    template.clone(tmp_path / "clone.db")
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("defect", ["bytes", "sidecar", "symlink", "destination"])
def test_schema_template_fails_closed(tmp_path: Path, defect: str) -> None:
    directory = tmp_path / "template"
    directory.mkdir()
    template = CurrentSchemaTemplate(directory)
    destination = tmp_path / "clone.db"
    if defect == "bytes":
        template.path.chmod(0o600)
        template.path.write_bytes(b"changed")
    elif defect == "sidecar":
        Path(f"{template.path}-wal").write_bytes(b"unclosed")
    elif defect == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"unchanged")
        template.path.unlink()
        template.path.symlink_to(outside)
    else:
        destination.write_bytes(b"existing")
    with pytest.raises((RuntimeError, OSError)):
        template.clone(destination)
    if defect == "destination":
        assert destination.read_bytes() == b"existing"
    else:
        assert not destination.exists()
    if defect == "symlink":
        assert outside.read_bytes() == b"unchanged"


def test_cleanup_handles_readonly_directories_without_following_links(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    case = parent / "case"
    case.mkdir(mode=0o700)
    owned = OwnedScratch.capture(case, parent)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"preserved")
    sentinel.chmod(0o400)
    child = case / "readonly"
    child.mkdir()
    (child / "symlink").symlink_to(outside, target_is_directory=True)
    os.link(sentinel, child / "hardlink")
    child.chmod(0o500)
    case.chmod(0o500)
    owned.remove()
    assert not case.exists()
    assert sentinel.read_bytes() == b"preserved"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o400


@pytest.mark.parametrize("replacement", ["symlink", "directory", "parent"])
def test_cleanup_rejects_replaced_root(tmp_path: Path, replacement: str) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    case = parent / "case"
    case.mkdir(mode=0o700)
    owned = OwnedScratch.capture(case, parent)
    case.rename(parent / "original")
    if replacement == "parent":
        parent.rename(tmp_path / "original-parent")
        parent.mkdir(mode=0o700)
    if replacement == "symlink":
        case.symlink_to(tmp_path, target_is_directory=True)
    else:
        case.mkdir(mode=0o700)
        (case / "sentinel").write_text("preserved")
    with pytest.raises((ValueError, OSError)):
        owned.remove()
    if replacement != "symlink":
        assert (case / "sentinel").read_text() == "preserved"


def run_probe(tmp_path: Path, source: str, *options: str) -> dict[str, object]:
    probe = tmp_path / "test_probe.py"
    probe.write_text(source)
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    report = tmp_path / "result.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.efficiency_plugin",
            "-c",
            str(config),
            "--basetemp",
            str(tmp_path / "run"),
            "--harness-report",
            str(report),
            *options,
            str(probe),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(REPOSITORY),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stdout + result.stderr
    return json.loads(report.read_text())  # type: ignore[no-any-return]


def test_plugin_waits_for_teardown_and_preserves_every_failure_phase(
    tmp_path: Path,
) -> None:
    result = run_probe(
        tmp_path,
        """
import pytest

def test_pass(tmp_path):
    child = tmp_path / "readonly"
    child.mkdir()
    (child / "data").write_text("data")
    child.chmod(0o500)

@pytest.fixture
def fixture(tmp_path):
    yield
    assert (tmp_path / "evidence").exists()

def test_teardown_reads_scratch(tmp_path, fixture):
    (tmp_path / "evidence").write_text("preserved until teardown")

def test_call_failure(tmp_path):
    (tmp_path / "evidence").write_text("call")
    assert False

@pytest.fixture
def setup_failure(tmp_path):
    (tmp_path / "evidence").write_text("setup")
    raise RuntimeError("intentional")

def test_setup_failure(setup_failure): pass

@pytest.fixture
def teardown_failure(tmp_path):
    (tmp_path / "evidence").write_text("teardown")
    yield
    raise RuntimeError("intentional")

def test_teardown_failure(teardown_failure): pass

@pytest.mark.keep_scratch
def test_keep(tmp_path):
    (tmp_path / "evidence").write_text("explicit")

def test_session_data(tmp_path_factory, tmp_path):
    root = tmp_path_factory.mktemp("shared-session")
    (root / "evidence").write_text("session")
""",
    )
    assert result["exit_status"] == 1
    assert not list((tmp_path / "run").glob("test_pass[0-9]*"))
    assert not list((tmp_path / "run").glob("test_teardown_reads_scratch*"))
    for name in (
        "test_call_failure",
        "test_setup_failure",
        "test_teardown_failure",
        "test_keep",
        "shared-session",
    ):
        assert any(
            (path / "evidence").exists() for path in (tmp_path / "run").glob(name + "*")
        )


@pytest.mark.parametrize("worker", ["thread", "process"])
def test_plugin_retains_scratch_for_live_workers(tmp_path: Path, worker: str) -> None:
    start = (
        "import threading\n"
        "threading.Thread(target=lambda: threading.Event().wait(5), "
        "daemon=True).start()"
        if worker == "thread"
        else "import subprocess, sys, atexit\n"
        'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])\n'
        "atexit.register(lambda: (p.terminate(), p.wait(timeout=5)))"
    )
    result = run_probe(
        tmp_path,
        'def test_live(tmp_path):\n    (tmp_path / "evidence").write_text("active")\n'
        + "\n".join("    " + line for line in start.splitlines()),
    )
    cases = result["tests"]
    assert isinstance(cases, dict)
    assert next(iter(cases.values()))["cleanup"] == "retained_workers"
    assert list((tmp_path / "run").glob("test_live*/evidence"))


def test_debug_option_keeps_successful_case(tmp_path: Path) -> None:
    result = run_probe(
        tmp_path,
        'def test_ok(tmp_path): (tmp_path / "evidence").touch()',
        "--keep-passed-tmp",
    )
    assert result["exit_status"] == 0
    assert list((tmp_path / "run").glob("test_ok*/evidence"))


def test_custom_tmp_path_does_not_authorize_removing_session_data(
    tmp_path: Path,
) -> None:
    result = run_probe(
        tmp_path,
        """
import pytest

@pytest.fixture(name="tmp_path")
def custom(tmp_path_factory):
    return tmp_path_factory.mktemp("custom-session")

def test_custom(tmp_path):
    (tmp_path / "evidence").write_text("session data")
""",
    )
    assert result["exit_status"] == 0
    assert list((tmp_path / "run").glob("custom-session*/evidence"))

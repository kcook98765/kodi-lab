"""Imported session fixtures share only closed templates within one session."""

from __future__ import annotations

import inspect
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests import test_test_efficiency


def test_imported_fixtures_build_one_store_and_keep_private_clones(
    tmp_path: Path,
) -> None:
    first = tmp_path / "test_a.py"
    second = tmp_path / "test_b.py"
    first.write_text("""
import sqlite3
from tests.storage.test_migrations import migration_templates as migration_templates

def test_first(migration_templates, tmp_path, request):
    request.config.shared_migration_store = migration_templates
    request.config.first_clone = tmp_path / "first.db"
    migration_templates.clone("head", request.config.first_clone)
    with sqlite3.connect(request.config.first_clone) as db:
        db.execute("CREATE TABLE private_probe (id INTEGER)")
""")
    second.write_text("""
import sqlite3
from tests.storage.test_migrations import migration_templates as migration_templates

def test_second(migration_templates, tmp_path, request):
    assert migration_templates is request.config.shared_migration_store
    clone = tmp_path / "second.db"
    assert clone != request.config.first_clone
    migration_templates.clone("head", clone)
    with sqlite3.connect(clone) as db:
        query = "SELECT count(*) FROM sqlite_master WHERE name='private_probe'"
        assert db.execute(query).fetchone() == (0,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
""")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(tmp_path / "run"),
            "-c",
            str(config),
            str(first),
            str(second),
        ],
        env={**os.environ, "PYTHONPATH": str(test_test_efficiency.REPOSITORY)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_failed_store_construction_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real interrupted Alembic build must not publish a partial cache entry.
    from types import SimpleNamespace
    from typing import cast

    from tests.storage import test_migrations as module

    config = SimpleNamespace(stash=pytest.Stash())
    request = cast(pytest.FixtureRequest, SimpleNamespace(config=config))
    factory = cast(
        pytest.TempPathFactory,
        SimpleNamespace(mktemp=lambda _name: tmp_path / "templates"),
    )

    def fail(*_args: object) -> None:
        raise RuntimeError("injected migration construction failure")

    monkeypatch.setattr(command, "upgrade", fail)
    with pytest.raises(RuntimeError, match="injected migration"):
        inspect.unwrap(module.migration_templates)(factory, request)
    assert module._SESSION_TEMPLATES not in config.stash


def test_migration_clone_closes_verification_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.storage.test_migrations import MigrationTemplateStore

    source = tmp_path / "template.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('revision')")
        connection.commit()
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def tracked(path: Path) -> sqlite3.Connection:
        connection = connect(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked)
    store = MigrationTemplateStore(
        tmp_path, {"revision": source}, head_revision="revision"
    )
    store.clone("head", tmp_path / "clone.db")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")

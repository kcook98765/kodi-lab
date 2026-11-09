"""Exact old-head parity, fresh initialization, and refusal without writes."""

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.packaged_migrations import database_is_empty, migrate_database
from tests.migration_support import migration_config
from tests.schema_manifest import schema_manifest

BASELINE = "0001_initial_release"


def test_exact_prerelease_head_contract_and_safe_reinitialization(
    tmp_path: Path,
) -> None:
    database = tmp_path / "new.db"
    config = migration_config(database)
    graph = ScriptDirectory.from_config(config)
    assert [(r.revision, r.down_revision) for r in graph.walk_revisions()] == [
        ("0007_production_navigation", "0006_external_screenshots"),
        ("0006_external_screenshots", "0005_managed_web"),
        ("0005_managed_web", "0004_clean_reset"),
        ("0004_clean_reset", "0003_managed_versions"),
        ("0003_managed_versions", "0002_managed_setup"),
        ("0002_managed_setup", BASELINE),
        (BASELINE, None),
    ]
    command.upgrade(config, BASELINE)
    expected = json.loads(
        (Path(__file__).parents[1] / "fixtures/initial-schema.json").read_text()
    )
    assert schema_manifest(database) == expected
    before = database.read_bytes()
    command.upgrade(config, BASELINE)
    assert database.read_bytes() == before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [
            (BASELINE,)
        ]
    engine = create_database_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(config, "base")
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "revision",
    [
        None,
        "",
        "0001_targets_instances",
        "0036_mcp_core_foundation",
        "0076_managed_resource_policy",
        "0092_create_kodi_operations",
        "unknown",
        "multiple",
        "malformed",
        "version_only",
    ],
)
@pytest.mark.parametrize("entry", ["alembic", "installed"])
def test_unsupported_database_is_refused_byte_for_byte(
    tmp_path: Path, revision: str | None, entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as db:
        if revision != "version_only":
            db.execute(
                "CREATE TABLE retained (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("INSERT INTO retained VALUES (1,'keep application data')")
        if revision is not None:
            if revision == "malformed":
                db.execute("CREATE TABLE alembic_version (wrong_column TEXT)")
            else:
                db.execute(
                    "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)"
                )
                if revision:
                    db.execute(
                        "INSERT INTO alembic_version VALUES (?)",
                        (BASELINE if revision == "version_only" else revision,),
                    )
                if revision == "multiple":
                    db.execute("INSERT INTO alembic_version VALUES (?)", (BASELINE,))
    before = hashlib.sha256(database.read_bytes()).digest()
    config = migration_config(database)
    if entry == "installed":
        from collections.abc import Iterator
        from contextlib import contextmanager

        from alembic.config import Config

        @contextmanager
        def source_config(_url: str) -> Iterator[Config]:
            yield config

        monkeypatch.setattr(
            "kodi_lab.storage.packaged_migrations.packaged_config", source_config
        )
    with pytest.raises(RuntimeError, match="fresh installation is required"):
        if entry == "alembic":
            command.upgrade(config, "head")
        else:
            migrate_database(database, f"sqlite:///{database}")
    assert hashlib.sha256(database.read_bytes()).digest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.db"]


@pytest.mark.parametrize("name", ["sqlite", "sqlitex_retained_data", "SQLITEZ_data"])
@pytest.mark.parametrize("entry", ["alembic", "installed_preflight"])
def test_sqlite_like_user_tables_are_not_mistaken_for_empty_databases(
    tmp_path: Path, name: str, entry: str
) -> None:
    database = tmp_path / "retained.db"
    with sqlite3.connect(database) as db:
        db.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, value TEXT)')
        db.execute(f'INSERT INTO "{name}" VALUES (1, ?)', ("retained data",))
    before = database.read_bytes()
    config = migration_config(database)
    with pytest.raises(RuntimeError, match="fresh installation is required"):
        if entry == "alembic":
            command.upgrade(config, "head")
        else:
            database_is_empty(database, config)
    assert database.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["retained.db"]


def test_normal_forward_migration_after_baseline(tmp_path: Path) -> None:
    tree = tmp_path / "migrations"
    shutil.copytree(Path(__file__).parents[2] / "migrations", tree)
    (tree / "versions/0002_future.py").write_text(
        "from alembic import op\n"
        "revision='0002_future'\ndown_revision='0007_production_navigation'\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE future_data (id INTEGER PRIMARY KEY)')\n"
        "def downgrade(): op.execute('DROP TABLE future_data')\n"
    )
    database = tmp_path / "future.db"
    config = migration_config(database)
    config.set_main_option("script_location", str(tree))
    command.upgrade(config, BASELINE)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0002_future",)
        ]
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    command.downgrade(config, BASELINE)


@pytest.mark.parametrize("state", ["old", "unversioned", "empty", "multiple"])
def test_installed_startup_requires_fresh_install_for_unsupported_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from alembic.config import Config

    from kodi_lab import remote_lifecycle
    from kodi_lab.storage import packaged_migrations

    root = tmp_path / "state"
    database = root / "database/kodi-lab.db"
    url = f"sqlite:///{database}"

    @contextmanager
    def source_config(_url: str) -> Iterator[Config]:
        yield migration_config(database)

    monkeypatch.setattr(packaged_migrations, "packaged_config", source_config)
    monkeypatch.setattr(remote_lifecycle, "packaged_config", source_config)
    remote_lifecycle.initialize(root, url)
    with sqlite3.connect(database) as db:
        if state == "old":
            db.execute(
                "UPDATE alembic_version SET version_num='0092_create_kodi_operations'"
            )
        elif state == "unversioned":
            db.execute("DROP TABLE alembic_version")
        elif state == "empty":
            db.execute("DELETE FROM alembic_version")
        else:
            db.execute(
                "INSERT INTO alembic_version VALUES ('0092_create_kodi_operations')"
            )
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="fresh installation is required"):
        remote_lifecycle.validate(root, url, require_head=True, require_marker=True)
    assert database.read_bytes() == before

"""Remote container lifecycle proof against packaged historical migrations."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from kodi_lab import remote_lifecycle
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    generate_master_key,
    initialize_state,
)
from kodi_lab.remote_lifecycle import (
    _key_digest,
    _revision,
    _verify_backup,
    backup,
    initialize,
    lifecycle_lock,
    restore,
    upgrade,
    validate,
)
from kodi_lab.storage import packaged_migrations


@pytest.fixture(autouse=True)
def repository_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real tree while the installed-wheel path has its own tests."""

    @contextmanager
    def config_for(database_url: str) -> Iterator[Config]:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[2] / "migrations")
        )
        config.set_main_option("sqlalchemy.url", database_url)
        yield config

    monkeypatch.setattr(packaged_migrations, "packaged_config", config_for)
    monkeypatch.setattr(remote_lifecycle, "packaged_config", config_for)


def _url(root: Path) -> str:
    return f"sqlite:///{root / 'database' / 'kodi-lab.db'}"


def _historical(root: Path) -> None:
    initialize_state(root)
    generate_master_key(root)
    previous_umask = os.umask(0o077)
    try:
        with packaged_migrations.packaged_config(_url(root)) as config:
            command.upgrade(config, "0052_production_policy_revision_binding")
    finally:
        os.umask(previous_umask)


def test_fresh_state_is_idempotent_and_partial_state_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    revision = initialize(root, _url(root))
    key = _key_digest(root)
    assert initialize(root, _url(root)) == revision
    assert _key_digest(root) == key
    assert validate(root, _url(root), require_head=True) == revision
    assert stat.S_IMODE((root / "master-key").stat().st_mode) == 0o600
    (root / ".state.json").unlink()
    with pytest.raises(HostBootstrapError, match="marker is missing"):
        validate(root, _url(root), require_marker=True)
    with pytest.raises(HostBootstrapError, match="marker is missing"):
        initialize(root, _url(root))
    previous, adopted, identifier = upgrade(root, _url(root))
    assert previous == adopted == revision and identifier is not None
    assert (
        validate(root, _url(root), require_head=True, require_marker=True) == revision
    )
    (root / "master-key").unlink()
    with pytest.raises(FileNotFoundError):
        initialize(root, _url(root))
    with pytest.raises(FileNotFoundError):
        validate(root, _url(root))
    partial = tmp_path / "partial"
    partial.mkdir(mode=0o700)
    (partial / "unexpected").write_text("x")
    with pytest.raises(FileNotFoundError):
        initialize(partial, _url(partial))


def test_backup_is_private_complete_and_uses_sqlite_backup_api(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize(root, _url(root))
    db_path = root / "database" / "kodi-lab.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE lifecycle_probe (value TEXT NOT NULL)")
        db.execute("INSERT INTO lifecycle_probe VALUES ('committed')")
        db.commit()
        with lifecycle_lock(root):
            identifier = backup(root, _url(root))
        folder = root / "backups" / identifier
        metadata = _verify_backup(root, identifier)
        assert metadata["complete"] is True
        assert metadata["key_sha256"] == _key_digest(root)
        assert "application" in metadata and "created_at" in metadata
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((folder / "master-key").stat().st_mode) == 0o600
        assert not (folder / "database" / "kodi-lab.db-wal").exists()
        with sqlite3.connect(folder / "database" / "kodi-lab.db") as copy:
            assert copy.execute("SELECT value FROM lifecycle_probe").fetchone() == (
                "committed",
            )
        (folder / "backups").mkdir(mode=0o700)
        with pytest.raises(HostBootstrapError, match="directories differ"):
            _verify_backup(root, identifier)
        (folder / "backups").rmdir()

    def second_lock() -> None:
        with lifecycle_lock(root):
            pass

    with lifecycle_lock(root), pytest.raises(HostBootstrapError, match="in use"):
        second_lock()


def test_historical_transition_and_matching_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)
    before = _revision(root / "database" / "kodi-lab.db")
    key = _key_digest(root)
    previous, current, identifier = upgrade(root, _url(root))
    assert identifier is not None
    assert previous == before
    assert current == "0053_production_screenshot_capability_amendment"
    assert upgrade(root, _url(root)) == (current, current, None)
    assert (
        _revision(root / "backups" / identifier / "database" / "kodi-lab.db") == before
    )
    with pytest.raises(HostBootstrapError, match="confirmation"):
        restore(root, _url(root), identifier, "wrong")
    wrong = backup(root, _url(root))
    with pytest.raises(HostBootstrapError, match="most recent upgrade"):
        restore(root, _url(root), wrong, wrong)
    original_copyfile = shutil.copyfile

    def interrupted_copy(
        source: str | Path, destination: str | Path, *, follow_symlinks: bool = True
    ) -> str | Path:
        if Path(destination) == root / "master-key":
            raise OSError("injected restore interruption")
        return original_copyfile(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", interrupted_copy)
    with pytest.raises(OSError, match="injected restore interruption"):
        restore(root, _url(root), identifier, identifier)
    with pytest.raises(HostBootstrapError, match="restore is incomplete"):
        validate(root, _url(root), require_head=True)
    monkeypatch.setattr(shutil, "copyfile", original_copyfile)
    restored, quarantine_id = restore(root, _url(root), identifier, identifier)
    assert restored == before
    assert _key_digest(root) == key
    assert (root / "quarantine" / quarantine_id).is_dir()
    assert any(
        _revision(path / "database" / "kodi-lab.db") == current
        for path in (root / "quarantine").iterdir()
        if (path / "database" / "kodi-lab.db").exists()
    )
    with pytest.raises(HostBootstrapError, match="does not match installed head"):
        validate(root, _url(root), require_head=True)


def test_failed_migration_blocks_startup_and_restore_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)

    def fail(*_args: object) -> tuple[str, str]:
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr("kodi_lab.remote_lifecycle.migrate_database", fail)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        upgrade(root, _url(root))
    receipt = json.loads((root / ".upgrade-pending.json").read_text())
    with pytest.raises(HostBootstrapError, match="incomplete"):
        validate(root, _url(root), require_head=True)
    restored, _ = restore(root, _url(root), receipt["backup_id"], receipt["backup_id"])
    assert restored == "0052_production_policy_revision_binding"


def test_failed_backup_prevents_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)

    def fail(*_args: object) -> str:
        raise RuntimeError("injected backup failure")

    monkeypatch.setattr("kodi_lab.remote_lifecycle.backup", fail)
    with pytest.raises(RuntimeError, match="injected backup failure"):
        upgrade(root, _url(root))
    assert (
        _revision(root / "database" / "kodi-lab.db")
        == "0052_production_policy_revision_binding"
    )
    assert not (root / ".upgrade-pending.json").exists()

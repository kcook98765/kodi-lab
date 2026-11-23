"""The additive migration preserves authority and refuses to discard history."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from tests.storage.test_ui_observation_migration import _config, _revision, _triggers

PREDECESSOR = "0017_system_snapshot_capability"
REVISION = "0018_source_addition_reviews"


@pytest.mark.parametrize("retained", [False, True])
def test_direct_predecessor_preservation_and_history_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retained: bool
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    before = _triggers(database)
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('alembic_version', 'sqlite_sequence')"
            )
        ]
        original = {
            table: connection.execute('SELECT * FROM "' + table + '"').fetchall()
            for table in tables
        }
    command.upgrade(config, REVISION)
    assert _revision(database) == REVISION
    assert _triggers(database) == before
    with sqlite3.connect(database) as connection:
        assert original == {
            table: connection.execute('SELECT * FROM "' + table + '"').fetchall()
            for table in tables
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        if retained:
            connection.execute(
                "INSERT INTO source_addition_reviews(id,target_id,source_id,"
                "container_id,proof_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    "review",
                    "target",
                    "source",
                    "c" * 64,
                    "{}",
                    "2026-11-23T00:00:00.000000Z",
                    "2026-11-23T00:15:00.000000Z",
                ),
            )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                connection.execute(
                    "UPDATE source_addition_reviews SET dispatched_at='now'"
                )
    if retained:
        with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
            command.downgrade(config, PREDECESSOR)
        assert _revision(database) == REVISION
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM source_addition_reviews"
            ).fetchone() == (1,)
    else:
        command.downgrade(config, PREDECESSOR)
        assert _revision(database) == PREDECESSOR
        command.upgrade(config, REVISION)
        assert _revision(database) == REVISION
    assert _triggers(database) == before

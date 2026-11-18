"""Closed capability admission preserves retained records and SQL guards."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from tests.storage.test_ui_observation_migration import (
    _config,
    _review,
    _revision,
    _triggers,
)

PREDECESSOR = "0016_reviewed_bridge_updates"
REVISION = "0017_system_snapshot_capability"


@pytest.mark.parametrize("admit_snapshot", [False, True])
def test_direct_predecessor_preservation_and_safe_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admit_snapshot: bool
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    with sqlite3.connect(database) as connection:
        _review(connection, '["log_scan","screenshot"]', 1)
        rows = connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(connection, '["screenshot","system_snapshot"]', 9)
    original = _triggers(database)
    command.upgrade(config, REVISION)
    assert _triggers(database) == original
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
            == rows
        )
        if admit_snapshot:
            _review(connection, '["screenshot","system_snapshot"]', 2)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(connection, '["filesystem","screenshot"]', 3)
    command.upgrade(config, "head")
    assert _revision(database) == REVISION
    if admit_snapshot:
        with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
            command.downgrade(config, PREDECESSOR)
        assert _revision(database) == REVISION
    else:
        command.downgrade(config, PREDECESSOR)
        assert _revision(database) == PREDECESSOR
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute("SELECT * FROM reviewed_bridge_builds").fetchall()
                == rows
            )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _review(connection, '["screenshot","system_snapshot"]', 4)
    assert _triggers(database) == original

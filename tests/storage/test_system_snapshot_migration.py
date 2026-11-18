"""Closed capability admission preserves retained records and SQL guards."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.application.test_production_bridges import (
    _pair_production_bridge,
    _production_target,
)
from tests.kodi.fake_external_bridge import FakeExternalBridge
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


@pytest.mark.parametrize(
    "capability_column", ["observed_capabilities_json", "verified_capabilities_json"]
)
def test_populated_production_authority_and_history_survive_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability_column: str
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, PREDECESSOR)
    engine = create_database_engine(f"sqlite:///{database}")
    factory = create_session_factory(engine)
    try:
        target, _ = _production_target(factory, tmp_path)
        with FakeExternalBridge(target_id=target, token=b"x" * 32) as fake:
            service = _pair_production_bridge(factory, tmp_path, target, fake)
            service.set_reviewed_update_policy(
                target, enabled=True, expected_revision=0
            )
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO production_bridge_adoptions "
                "(id,target_id,bridge_id,from_revision,to_revision,from_build_id,"
                "to_build_id,authority_revision,authority_build_id,"
                "http_acknowledgement_id,binding_digest,policy_revision,"
                "actor,occurred_at) "
                "SELECT ?,p.target_id,p.pinned_bridge_id,1,2,p.reviewed_build_id,"
                "p.reviewed_build_id,1,p.reviewed_build_id,NULL,u.binding_digest,"
                "u.revision,'automatic_reviewed_update',u.updated_at "
                "FROM external_bridge_pairings p "
                "JOIN production_bridge_update_policies u "
                "ON u.target_id=p.target_id",
                ("00000000-0000-4000-8000-000000000099",),
            )
            protected = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT IN ('alembic_version','sqlite_sequence') "
                    "ORDER BY name"
                )
            ]

        def rows() -> dict[str, list[tuple[object, ...]]]:
            with sqlite3.connect(database) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchall() == [
                    ("ok",)
                ]
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
                return {
                    table: connection.execute(
                        'SELECT * FROM "' + table + '"'
                    ).fetchall()
                    for table in protected
                }

        before, triggers = rows(), _triggers(database)
        for revision in (REVISION, PREDECESSOR, REVISION):
            if revision == PREDECESSOR:
                command.downgrade(config, revision)
            else:
                command.upgrade(config, revision)
            assert _revision(database) == revision
            assert rows() == before and _triggers(database) == triggers
            with sqlite3.connect(database) as connection:
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute("DELETE FROM production_bridge_adoptions")
                with pytest.raises(sqlite3.IntegrityError, match="revision"):
                    connection.execute(
                        "UPDATE external_bridge_pairings "
                        "SET production_bridge_trust_revision=0"
                    )
        with sqlite3.connect(database) as connection:
            # Both pairing capability columns independently block lossy downgrade.
            connection.execute(
                "UPDATE external_bridge_pairings SET "
                + capability_column
                + '=\'["screenshot","system_snapshot","workspace_deployment"]\''
            )
        with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
            command.downgrade(config, PREDECESSOR)
        assert _revision(database) == REVISION
        assert _triggers(database) == triggers
    finally:
        engine.dispose()

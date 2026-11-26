"""Closed capability admission preserves retained records and SQL guards."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

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


@pytest.mark.parametrize(
    "capability_column", ["observed_capabilities_json", "verified_capabilities_json"]
)
def test_populated_production_authority_and_history_survive_reinitialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability_column: str
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    head = ScriptDirectory.from_config(config).get_current_head()
    command.upgrade(config, "head")
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
        command.upgrade(config, "head")
        assert _revision(database) == head
        assert rows() == before and _triggers(database) == triggers
        with sqlite3.connect(database) as connection:
            _review(connection, '["screenshot","system_snapshot"]', 2)
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _review(connection, '["filesystem","screenshot"]', 3)
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute("DELETE FROM production_bridge_adoptions")
            with pytest.raises(sqlite3.IntegrityError, match="revision"):
                connection.execute(
                    "UPDATE external_bridge_pairings "
                    "SET production_bridge_trust_revision=0"
                )
        with sqlite3.connect(database) as connection:
            # Both pairing capability columns retain valid snapshot evidence.
            connection.execute(
                "UPDATE external_bridge_pairings SET "
                + capability_column
                + '=\'["screenshot","system_snapshot","workspace_deployment"]\''
            )
        with pytest.raises(
            RuntimeError, match="initial managed-v2 schema cannot be downgraded"
        ):
            command.downgrade(config, "base")
        assert _revision(database) == head
        assert _triggers(database) == triggers
    finally:
        engine.dispose()

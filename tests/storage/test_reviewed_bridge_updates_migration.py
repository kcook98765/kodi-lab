"""Initial schema defaults reviewed updates off and preserves policy history."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ProductionBridgeUpdatePolicyRow
from tests.application.test_production_bridges import (
    _pair_production_bridge,
    _production_target,
)
from tests.kodi.fake_external_bridge import FakeExternalBridge
from tests.storage.test_ui_observation_migration import _config, _revision, _triggers

REVISION = "0001_foundation"


def test_reinitialization_preserves_original_rows_and_defaults_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, "head")
    engine = create_database_engine(f"sqlite:///{database}")
    factory = create_session_factory(engine)
    target, _ = _production_target(factory, tmp_path)
    with FakeExternalBridge(target_id=target, token=b"x" * 32) as fake:
        service = _pair_production_bridge(factory, tmp_path, target, fake)
        protected = [
            "targets",
            "external_kodi_targets",
            "external_production_policies",
            "external_bridge_pairings",
            "reviewed_bridge_builds",
            "production_bridge_http_acknowledgements",
            "external_bridge_events",
        ]

        def rows() -> dict[str, list[tuple[object, ...]]]:
            with sqlite3.connect(database) as c:
                return {
                    t: c.execute('SELECT * FROM "' + t + '"').fetchall()
                    for t in protected
                }

        before = rows()
        triggers = _triggers(database)
        command.upgrade(config, REVISION)
        assert rows() == before and triggers.items() <= _triggers(database).items()
        assert not service.reviewed_update_policy(target).enabled
        with factory() as session:
            assert not list(session.scalars(select(ProductionBridgeUpdatePolicyRow)))
        service.set_reviewed_update_policy(target, enabled=True, expected_revision=0)
        with sqlite3.connect(database) as c:
            with pytest.raises(
                sqlite3.IntegrityError, match="bridge_update_policy_revision"
            ):
                c.execute(
                    "UPDATE production_bridge_update_policies SET revision=revision"
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="bridge_update_policy_history"
            ):
                c.execute("DELETE FROM production_bridge_update_policies")
            with pytest.raises(
                sqlite3.IntegrityError, match="bridge_update_policy_history"
            ):
                c.execute(
                    "INSERT OR REPLACE INTO production_bridge_update_policies "
                    "SELECT * FROM production_bridge_update_policies"
                )
        with pytest.raises(
            RuntimeError, match="initial managed-v2 schema cannot be downgraded"
        ):
            command.downgrade(config, "base")
        assert _revision(database) == REVISION and rows() == before
    engine.dispose()

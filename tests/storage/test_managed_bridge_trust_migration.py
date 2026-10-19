"""Direct predecessor migration proof; no authority/permission backfill."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from tests.storage.test_ui_observation_migration import _config, _triggers
from tests.web.conftest import DEFINITION_ID, INSTANCE_ID, REVISION_ID
from tests.web.conftest import database as database


def test_direct_upgrade_has_no_bindings_and_preserves_authority_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, "0068_ui_observation_capability")
    before = _triggers(database)
    command.upgrade(config, "0069_managed_bridge_trust")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM managed_bridge_trust"
        ).fetchone() == (0,)
    assert _triggers(database) == before
    command.downgrade(config, "0068_ui_observation_capability")
    assert _triggers(database) == before


def test_populated_binding_refuses_lossy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: sessionmaker[Session]
) -> None:
    config, database_path = _config(tmp_path, monkeypatch)
    command.upgrade(config, "0069_managed_bridge_trust")
    engine = create_engine(f"sqlite:///{database_path}")
    sessions: sessionmaker[Session] = create_session_factory(engine)
    # Copy only the managed runtime fixture. Keep the migration-created authority
    # singleton and all of its guard triggers intact.
    with database() as source, sessions.begin() as session:
        for model, key in (
            (RuntimeDefinitionRow, DEFINITION_ID),
            (RuntimeRevisionRow, REVISION_ID),
            (TargetRow, INSTANCE_ID),
            (ManagedInstanceRow, INSTANCE_ID),
        ):
            row = source.get(model, str(key))
            assert row is not None
            session.add(
                model(
                    **{
                        column.name: getattr(row, column.name)
                        for column in model.__table__.columns
                    }
                )
            )
            session.flush()
    # Insert the historical 0069 shape. The current ORM adds reviewed-installation
    # provenance in 0077, which must not be injected into this predecessor proof.
    legacy_trust = Table("managed_bridge_trust", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            legacy_trust.insert().values(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id="a" * 64,
                bridge_id="00000000-0000-4000-8000-000000000999",
                bridge_port=8765,
                secret_reference="00000000-0000-4000-8000-000000000998",
                identity_json="{}",
                state="active",
                generation=1,
                prepared_at="2026-10-07T00:00:00.000000Z",
                established_at="2026-10-07T00:00:00.000000Z",
            )
        )
    with pytest.raises(RuntimeError, match="retirement"):
        command.downgrade(config, "0068_ui_observation_capability")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT state FROM managed_bridge_trust"
        ).fetchone() == ("active",)
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0069_managed_bridge_trust",)
    engine.dispose()

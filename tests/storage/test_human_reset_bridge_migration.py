"""Populated native-only confirmations keep their meaning across explicit consent."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD, NEW = "0013_agent_packages", "0014_human_reset_bridge"


def test_populated_upgrade_preserves_confirmation_and_empty_consent_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    identity = str(uuid4())
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO managed_web_intents VALUES "
                "(:id,'omega','reset',:sha,1,:now)"
            ),
            {"id": identity, "sha": "a" * 64, "now": "2026-11-13T01:00:00.000000Z"},
        )
        before = db.execute(text("SELECT * FROM managed_web_intents")).all()
        policies = db.execute(text("SELECT * FROM managed_resource_policy")).all()
    command.upgrade(config, NEW)
    with engine.connect() as db:
        row = db.execute(text("SELECT * FROM managed_web_intents")).one()
        assert tuple(row[:-1]) == tuple(before[0]) and row[-1] == 0
        assert (
            db.execute(text("SELECT * FROM managed_resource_policy")).all() == policies
        )
        assert db.scalar(text("PRAGMA integrity_check")) == "ok"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    with pytest.raises(IntegrityError, match="immutable"), engine.begin() as db:
        db.execute(
            text(
                "UPDATE managed_web_intents SET prepare_bridge=1 WHERE request_id=:id"
            ),
            {"id": identity},
        )
    command.downgrade(config, OLD)
    with engine.connect() as db:
        assert db.execute(text("SELECT * FROM managed_web_intents")).all() == before
    engine.dispose()


def test_opt_in_consent_requires_reset_and_prevents_lossy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    statement = text(
        "INSERT INTO managed_web_intents (request_id,generation,action,"
        "snapshot_sha256,reset_confirmed,created_at,prepare_bridge) "
        "VALUES (:id,'omega',:action,:sha,1,:now,1)"
    )
    params = {
        "id": str(uuid4()),
        "sha": "a" * 64,
        "now": "2026-11-13T01:00:00.000000Z",
        "action": "start",
    }
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(statement, params)
    with engine.begin() as db:
        db.execute(statement, {**params, "action": "reset"})
    with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
        command.downgrade(config, OLD)
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert db.scalar(text("SELECT prepare_bridge FROM managed_web_intents")) == 1
    engine.dispose()

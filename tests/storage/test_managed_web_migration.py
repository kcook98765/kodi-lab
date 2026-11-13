"""Frozen Web intent migration preserves authority and immutable human confirmation."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import ManagedWebIntentRow
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD, NEW = "0004_clean_reset", "0005_managed_web"


def test_upgrade_preserves_policies_and_supports_empty_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    tables = (
        "mcp_presets",
        "mcp_preset_permissions",
        "mcp_client_target_grants",
        "managed_resource_policy",
        "managed_clean_resets",
    )
    with engine.connect() as connection:
        before = {
            t: connection.execute(text(f"SELECT * FROM {t}")).all() for t in tables
        }
    command.upgrade(config, NEW)
    assert {
        c["name"] for c in inspect(engine).get_columns("managed_web_intents")
    } == set(ManagedWebIntentRow.__table__.columns.keys()) - {"prepare_bridge"}
    with engine.connect() as connection:
        assert before == {
            t: connection.execute(text(f"SELECT * FROM {t}")).all() for t in tables
        }
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    assert "managed_web_intents" not in inspect(engine).get_table_names()
    engine.dispose()


def test_confirmation_binding_is_immutable_and_cannot_be_lost_by_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    values = dict(
        request=str(uuid4()),
        version="omega",
        action="reset",
        snapshot="a" * 64,
        confirm=1,
        now="2026-11-06T19:35:20.000000Z",
    )
    statement = text(
        "INSERT INTO managed_web_intents VALUES "
        "(:request,:version,:action,:snapshot,:confirm,:now)"
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement, {**values, "confirm": 0})
    with engine.begin() as connection:
        connection.execute(statement, values)
    for column, replacement in (
        ("generation", "nexus"),
        ("action", "start"),
        ("snapshot_sha256", "b" * 64),
        ("reset_confirmed", 0),
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(f"UPDATE managed_web_intents SET {column}=:value"),
                dict(value=replacement),
            )
    with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
    engine.dispose()

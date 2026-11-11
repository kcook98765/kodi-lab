"""Upgrade preserves accepted intents; downgrade cannot erase new setup consent."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD = "0001_initial_release"
NEW = "0002_managed_setup"


def test_existing_approved_intent_and_admin_limit_survive_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    operation, request, target = (str(uuid4()) for _ in range(3))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO create_kodi_operations "
                "(id,request_id,target_id,generation,recipe_id,recipe_revision,recipe_digest,"
                "scope,stage,checkpoint,version,approved_at,updated_at) VALUES "
                "(:id,:request,:target,'omega','fixture',1,:digest,'create_native_v1',"
                "'installation','accepted',0,:now,:now)"
            ),
            dict(
                id=operation,
                request=request,
                target=target,
                digest="sha256:" + "a" * 64,
                now="2026-11-05T00:00:00.000000Z",
            ),
        )
        connection.execute(text("UPDATE managed_resource_policy SET max_running=2"))
        before = connection.execute(text("SELECT * FROM create_kodi_operations")).all()
    command.upgrade(config, NEW)
    with engine.connect() as connection:
        row = (
            connection.execute(text("SELECT * FROM create_kodi_operations"))
            .mappings()
            .one()
        )
        assert row["id"] == operation and row["request_id"] == request
        assert row["start_after_setup"] == 1 and row["preview_approved"] == 0
        assert (
            connection.scalar(text("SELECT max_running FROM managed_resource_policy"))
            == 2
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT * FROM create_kodi_operations")).all()
            == before
        )
        assert "start_after_setup" not in {
            c["name"] for c in inspect(connection).get_columns("create_kodi_operations")
        }
    engine.dispose()


@pytest.mark.parametrize("generation", ["omega", "piers"])
def test_downgrade_refuses_used_options_without_losing_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    # Exercise the predecessor's frozen schema without the current ORM, which
    # includes fields introduced by later public migrations.
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO create_kodi_operations "
                "(id,request_id,target_id,generation,recipe_id,recipe_revision,"
                "recipe_digest,scope,stage,checkpoint,version,approved_at,updated_at,"
                "start_after_setup,preview_approved) VALUES "
                "(:id,:request,:target,:generation,'fixture',1,:digest,"
                "'create_native_v1','installation','accepted',0,:now,:now,0,:preview)"
            ),
            dict(
                id=str(uuid4()),
                request=str(uuid4()),
                target=str(uuid4()),
                generation=generation,
                digest="sha256:" + "a" * 64,
                now="2026-11-05T00:00:00.000000Z",
                preview=generation == "piers",
            ),
        )
        before = connection.execute(text("SELECT * FROM create_kodi_operations")).all()
    with pytest.raises(RuntimeError, match="options have been used"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT * FROM create_kodi_operations")).all()
            == before
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    engine.dispose()

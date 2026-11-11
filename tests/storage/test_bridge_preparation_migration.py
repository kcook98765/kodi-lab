"""Additive predecessor migration preserves intent, authority and recovery history."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD = "0007_production_navigation"
NEW = "0008_managed_bridge_preparation"


def test_predecessor_rows_are_preserved_and_unused_upgrade_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE managed_resource_policy SET max_running=3"))
        connection.execute(
            text(
                "INSERT INTO create_kodi_operations "
                "(id,request_id,target_id,generation,recipe_id,recipe_revision,"
                "recipe_digest,scope,stage,checkpoint,version,approved_at,updated_at) "
                "VALUES "
                "(:id,:request,:target,'omega','fixture',1,:digest,'create_native_v1','installation','accepted',0,:now,:now)"
            ),
            dict(
                id=str(uuid4()),
                request=str(uuid4()),
                target=str(uuid4()),
                digest="sha256:" + "a" * 64,
                now="2026-11-11T00:00:00.000000Z",
            ),
        )
        tables = [
            name
            for name in inspect(connection).get_table_names()
            if name != "alembic_version"
        ]
        before = {
            name: [
                dict(row)
                for row in connection.execute(
                    text(f'SELECT * FROM "{name}"')
                ).mappings()
            ]
            for name in tables
        }
    command.upgrade(config, NEW)
    with engine.connect() as connection:
        for name, original in before.items():
            rows = [
                dict(row)
                for row in connection.execute(
                    text(f'SELECT * FROM "{name}"')
                ).mappings()
            ]
            if name == "create_kodi_operations":
                assert all(row.pop("bridge_required") == 0 for row in rows)
            assert rows == original, name
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM create_kodi_operations")
            ).mappings()
        ] == before["create_kodi_operations"]
    engine.dispose()


def test_immutable_bridge_intent_and_used_downgrade_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    identity = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO managed_bridge_preparations "
                "(id,request_id,target_id,runtime_revision_id,container_id,image_id,"
                "runtime_state_revision,keep_running,phase,checkpoint,version,"
                "attempt,created_at,updated_at) "
                "VALUES "
                "(:id,:request,:target,:revision,:container,:image,0,0,'start','pending',0,0,:now,:now)"
            ),
            dict(
                id=identity,
                request=str(uuid4()),
                target=str(uuid4()),
                revision=str(uuid4()),
                container="a" * 64,
                image="sha256:" + "b" * 64,
                now="2026-11-11T00:00:00.000000Z",
            ),
        )
    with engine.begin() as connection, pytest.raises(IntegrityError, match="immutable"):
        connection.execute(
            text(
                "UPDATE managed_bridge_preparations SET container_id=:other WHERE "
                "id=:id"
            ),
            dict(id=identity, other="c" * 64),
        )
    with pytest.raises(RuntimeError, match="receipts exist"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM managed_bridge_preparations"))
            == 1
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    engine.dispose()

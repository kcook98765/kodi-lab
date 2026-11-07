"""The three stacked revisions preserve populated predecessor state and audit."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates
from tests.storage.test_managed_clean_reset_migration import seed

OLD, HEAD = "0002_managed_setup", "0005_managed_web"
TABLES = (
    "mcp_clients",
    "mcp_presets",
    "mcp_preset_permissions",
    "mcp_client_target_grants",
    "managed_resource_policy",
)


@pytest.mark.parametrize("receipts", [False, True])
def test_full_managed_stack_upgrade_and_downgrade_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipts: bool
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    now, request, parent, client, target = (
        "2026-11-06T20:11:00.000000Z",
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
    )
    with engine.begin() as connection:
        connection.execute(text("UPDATE managed_resource_policy SET max_running=4"))
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in TABLES
        }
    command.upgrade(config, "0003_managed_versions")
    if receipts:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO managed_version_operations "
                    "(id,client_id,request_id,generation,target_id,action,phase,code,"
                    "created_at,updated_at) "
                    "VALUES (:id,:client,:request,'omega',:target,'start','blocked',"
                    "'blocked',:now,:now)"
                ),
                dict(id=parent, client=client, request=request, target=target, now=now),
            )
    command.upgrade(config, "0004_clean_reset")
    if receipts:
        with engine.begin() as connection:
            reset = seed(connection)
    command.upgrade(config, HEAD)
    if receipts:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO managed_web_intents VALUES "
                    "(:request,'omega','start',:snapshot,0,:now)"
                ),
                dict(request=request, snapshot="a" * 64, now=now),
            )
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
        )
        assert before == {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in TABLES
        }
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    if receipts:
        with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
            command.downgrade(config, OLD)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == HEAD
            )
            assert (
                connection.scalar(text("SELECT id FROM managed_version_operations"))
                == parent
            )
            assert (
                connection.scalar(text("SELECT id FROM managed_clean_resets"))
                == reset["id"]
            )
            assert (
                connection.scalar(text("SELECT request_id FROM managed_web_intents"))
                == request
            )
    else:
        command.downgrade(config, OLD)
        assert not {
            "managed_version_operations",
            "managed_clean_resets",
            "managed_web_intents",
        }.intersection(inspect(engine).get_table_names())
        command.upgrade(config, HEAD)
    engine.dispose()

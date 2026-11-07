"""Direct predecessor migration preserves authority and immutable request history."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import ManagedVersionOperationRow
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD = "0002_managed_setup"
NEW = "0003_managed_versions"
NOW = "2026-11-06T18:00:00.000000Z"


def test_direct_upgrade_preserves_policy_presets_and_empty_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE managed_resource_policy SET max_running=4"))
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in (
                "mcp_presets",
                "mcp_preset_permissions",
                "managed_resource_policy",
            )
        }
    command.upgrade(config, NEW)
    inspector = inspect(engine)
    assert {
        column["name"] for column in inspector.get_columns("managed_version_operations")
    } == set(ManagedVersionOperationRow.__table__.columns.keys())
    with engine.connect() as connection:
        for table, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {table}")).all() == rows
        assert (
            connection.scalar(text("SELECT count(*) FROM managed_version_operations"))
            == 0
        )
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM mcp_preset_permissions "
                    "WHERE permission='managed.versions'"
                )
            )
            == 0
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()
    command.downgrade(config, OLD)
    assert "managed_version_operations" not in inspect(engine).get_table_names()
    command.upgrade(config, NEW)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
    engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "client_id='00000000-0000-4000-8000-000000000001'",
        "request_id='00000000-0000-4000-8000-000000000002'",
        "target_id='00000000-0000-4000-8000-000000000003'",
        "generation='nexus'",
        "action='stop'",
        "runtime_revision_id='00000000-0000-4000-8000-000000000004', "
        "image_id='sha256:' || :digest",
        "created_at='2026-11-07T18:00:00.000000Z'",
        "container_id=:replacement",
        "lifecycle_operation_id='00000000-0000-4000-8000-000000000005'",
    ],
)
def test_receipt_binding_cannot_be_retargeted_or_erased_by_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    owner, request, target, operation, child = [str(uuid4()) for _ in range(5)]
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO managed_version_operations
            (id,client_id,request_id,generation,target_id,action,phase,code,
             container_id,lifecycle_operation_id,created_at,updated_at)
            VALUES (:id,:owner,:request,'omega',:target,'start','complete',
             'already_running',:container,:child,:now,:now)"""),
            dict(
                id=operation,
                owner=owner,
                request=request,
                target=target,
                child=child,
                now=NOW,
                container="a" * 64,
            ),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(f"UPDATE managed_version_operations SET {mutation} WHERE id=:id"),
            dict(id=operation, digest="b" * 64, replacement="c" * 64),
        )
    with pytest.raises(RuntimeError, match="receipts exist"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        saved = (
            connection.execute(text("SELECT * FROM managed_version_operations"))
            .mappings()
            .one()
        )
        assert saved["client_id"] == owner and saved["request_id"] == request
        assert saved["action"] == "start" and saved["generation"] == "omega"
        assert (
            saved["container_id"] == "a" * 64
            and saved["lifecycle_operation_id"] == child
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
    engine.dispose()


def test_owner_request_is_unique_across_actions_and_partial_runtime_binding_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    engine = create_database_engine(url)
    owner, request, target = [str(uuid4()) for _ in range(3)]
    statement = text("""INSERT INTO managed_version_operations
        (id,client_id,request_id,generation,target_id,action,phase,code,created_at,updated_at)
        VALUES (:id,:owner,:request,'omega',:target,:action,
                'blocked','test',:now,:now)""")
    parameters = dict(
        id=str(uuid4()),
        owner=owner,
        request=request,
        target=target,
        action="start",
        now=NOW,
    )
    with engine.begin() as connection:
        connection.execute(statement, parameters)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement, parameters | {"id": str(uuid4()), "action": "stop"}
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO managed_version_operations
            (id,client_id,request_id,generation,target_id,action,phase,code,
             runtime_revision_id,created_at,updated_at) VALUES
            (:id,:owner,:request,'omega',:target,'start','accepted','test',:revision,:now,:now)"""),
            parameters
            | {"id": str(uuid4()), "request": str(uuid4()), "revision": str(uuid4())},
        )
    engine.dispose()

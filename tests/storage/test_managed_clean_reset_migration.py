"""Direct 0003 transition preserves authority, history and Reset binding."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import ManagedCleanResetRow
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD = "0003_managed_versions"
NEW = "0004_clean_reset"
NOW = "2026-11-06T18:50:00.000000Z"


def seed(connection: Connection) -> dict[str, Any]:
    values: dict[str, Any] = {
        name: str(uuid4())
        for name in (
            "id",
            "client_id",
            "request_id",
            "target_id",
            "runtime_revision_id",
            "stop_id",
            "remove_id",
            "provision_id",
            "start_id",
        )
    }
    values.update(
        generation="omega",
        restart=0,
        image_id="sha256:" + "b" * 64,
        old_container_id="a" * 64,
        expected_state_revision=0,
        phase="accepted",
        code="accepted",
        created_at=NOW,
        updated_at=NOW,
    )
    columns = ",".join(values)
    connection.execute(
        text(
            f"INSERT INTO managed_clean_resets ({columns}) VALUES ("
            + ",".join(":" + k for k in values)
            + ")"
        ),
        values,
    )
    return values


def test_direct_upgrade_preserves_prior_authority_history_and_empty_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    tables = (
        "mcp_presets",
        "mcp_preset_permissions",
        "mcp_client_target_grants",
        "managed_resource_policy",
        "managed_version_operations",
    )
    with engine.begin() as connection:
        connection.execute(text("UPDATE managed_resource_policy SET max_running=4"))
        before = {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in tables
        }
    command.upgrade(config, NEW)
    assert {
        c["name"] for c in inspect(engine).get_columns("managed_clean_resets")
    } == set(ManagedCleanResetRow.__table__.columns.keys())
    with engine.connect() as connection:
        assert {
            table: connection.execute(text(f"SELECT * FROM {table}")).all()
            for table in tables
        } == before
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM mcp_preset_permissions WHERE "
                    "permission='managed.reset'"
                )
            )
            == 0
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    assert "managed_clean_resets" not in inspect(engine).get_table_names()
    command.upgrade(config, NEW)
    engine.dispose()


@pytest.mark.parametrize(
    "assignment",
    [
        "client_id",
        "request_id",
        "target_id",
        "runtime_revision_id",
        "stop_id",
        "remove_id",
        "provision_id",
        "start_id",
        "old_container_id",
        "restart",
    ],
)
def test_binding_is_immutable_and_downgrade_cannot_erase_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assignment: str
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        row = seed(connection)
    replacement = (
        1
        if assignment == "restart"
        else "c" * 64
        if assignment == "old_container_id"
        else str(uuid4())
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(f"UPDATE managed_clean_resets SET {assignment}=:value WHERE id=:id"),
            dict(value=replacement, id=row["id"]),
        )
    with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
        command.downgrade(config, OLD)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert connection.scalar(text("SELECT count(*) FROM managed_clean_resets")) == 1
    engine.dispose()


def test_active_target_and_request_are_unique_and_unrelated_mutation_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        row = seed(connection)
    with (
        pytest.raises(IntegrityError, match="clean reset pending"),
        engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO managed_instance_mutation_leases "
                "(managed_instance_id,operation_kind,operation_id) VALUES "
                "(:target,'fixture_assignment',:id)"
            ),
            dict(target=row["target_id"], id=str(uuid4())),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_clean_resets SET new_container_id=old_container_id")
        )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_clean_resets SET new_container_id=:new"),
            dict(new="d" * 64),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE managed_clean_resets SET new_container_id=:new"),
            dict(new="e" * 64),
        )
    engine.dispose()


def test_reset_composes_the_actual_migrated_schema_and_keeps_version_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_clean_reset import (
        ManagedCleanResetService,
        ManagedResetInput,
    )
    from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
    from kodi_lab.host_bootstrap import generate_master_key
    from kodi_lab.storage.engine import create_session_factory
    from tests.managed_versions_support import Environment

    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    command.upgrade(config, "head")
    root = tmp_path / "private-state"
    root.mkdir(mode=0o700)
    generate_master_key(root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    engine = create_database_engine(url)
    env = Environment(create_session_factory(engine), root)
    try:
        env.preset = env.admin.create_custom_preset(
            display_name="Migrated Reset",
            template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
            permissions=frozenset({"managed.versions", "managed.reset"}),
        )
        env.principal = env.actor("Migrated actor")
        target = env.prepare()
        started = env.invoke()
        env.wait(target)
        original = env.broker.records[0]
        operation = ManagedResetInput(version="omega", request_id=uuid4())
        result = ManagedCleanResetService(env.service).invoke(
            env.principal.client_id,
            operation,
            lambda session, selected: env.authorization.recheck_managed_versions(
                session, env.principal, "managed_reset", selected
            ),
        )
        assert (
            result.phase == "complete"
            and result.retirement == result.replacement == "confirmed"
        ), result
        assert env.broker.records[0].container_id != original.container_id
        assert env.invoke(request_id=started.request_id).replayed
        assert env.broker.starts == 1 and env.broker.provisions == 2
    finally:
        env.close()
        engine.dispose()

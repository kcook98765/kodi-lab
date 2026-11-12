"""0089 receipts remain unchanged; verification history cannot be discarded."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentError,
    _fingerprint,
)
from kodi_lab.storage.engine import create_database_engine
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_noop import deploy, environment
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD = "0001_initial_release"
NEW = "0001_initial_release"


def test_populated_upgrade_preserves_old_receipts_and_reverses_without_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, "head")
    env = fixture._environment(
        tmp_path / "legacy", database_path=Path(url.removeprefix("sqlite:///"))
    )
    env.sessions.kw["bind"].dispose()
    command.downgrade(config, OLD)
    engine = create_database_engine(url)
    request, operation = uuid4(), uuid4()
    legacy = dict(
        id=str(operation),
        client_id=str(env.client_id),
        request_id=str(request),
        workspace_id=str(env.workspace_id),
        development_source_id=str(fixture.SOURCE_ID),
        target_id=str(fixture.TARGET_ID),
        runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
        initiator_kind="mcp",
        target_class="internal_lab",
        deployment_mode="direct",
        generation=0,
        addon_id="plugin.video.slice4",
        addon_version="1.0.0",
        state="succeeded",
        phase="finalize",
        mutation_started=1,
        target_stopped=1,
        sync_started=1,
        sync_completed=1,
        target_started=1,
        inventory_classification="matched",
        addon_enabled=1,
        result_classification="unchanged",
        source_manifest_digest="sha256:" + "c" * 64,
        verified_destination_digest="sha256:" + "d" * 64,
        original_container_id=fixture.CONTAINER_ID,
        original_runtime_state_revision=0,
        requested_at=fixture.NOW_TEXT,
        started_at=fixture.NOW_TEXT,
        completed_at=fixture.NOW_TEXT,
    )
    with engine.begin() as connection:
        insert_columns = ",".join(legacy)
        values = ",".join(":" + k for k in legacy)
        connection.execute(
            text(
                f"INSERT INTO mcp_workspace_deployment_operations ({insert_columns}) "
                f"VALUES ({values})"
            ),
            legacy,
        )
        connection.execute(
            text(
                "INSERT INTO mcp_development_requests "
                "(id,client_id,request_id,operation_kind,parameter_fingerprint,"
                "development_source_id,workspace_id,operation_id,state,"
                "created_at,updated_at) "
                "VALUES (:id,:client,:request,'workspace_deploy',:fingerprint,:source,"
                ":workspace,:operation,'allocated',:now,:now)"
            ),
            dict(
                id=str(uuid4()),
                client=str(env.client_id),
                request=str(request),
                fingerprint=_fingerprint(
                    "workspace_deploy", env.workspace_id, 0, fixture.TARGET_ID, "direct"
                ),
                source=str(fixture.SOURCE_ID),
                workspace=str(env.workspace_id),
                operation=str(operation),
                now=fixture.NOW_TEXT,
            ),
        )
        tables = inspect(connection).get_table_names()
        columns = {
            table: [v["name"] for v in inspect(connection).get_columns(table)]
            for table in tables
            if table != "alembic_version"
        }
        before = {
            table: connection.execute(text(f'SELECT * FROM "{table}"')).all()
            for table in columns
        }
    command.upgrade(config, "head")
    replay = env.development.deploy(
        client_id=env.client_id,
        request_id=request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixture.TARGET_ID,
    )
    assert replay.deployment_id == operation
    assert not replay.deployment_work_skipped and replay.managed_verification is None
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 0
    with engine.connect() as connection:
        for table, names in columns.items():
            select_columns = ",".join('"' + name + '"' for name in names)
            assert (
                connection.execute(
                    text(f'SELECT {select_columns} FROM "{table}"')
                ).all()
                == before[table]
            )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM mcp_workspace_deployment_operations "
                    "WHERE managed_verification_json IS NOT NULL"
                )
            )
            == 0
        )
    command.downgrade(config, NEW)
    engine.dispose()


def test_noop_proof_works_on_migrated_schema_and_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    command.upgrade(config, "head")
    env = environment(
        tmp_path / "live", database_path=Path(url.removeprefix("sqlite:///"))
    )
    assert not deploy(env).deployment_work_skipped
    assert deploy(env).deployment_work_skipped
    engine = create_database_engine(url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM mcp_workspace_deployment_operations "
                    "WHERE managed_verification_json IS NOT NULL"
                )
            )
            == 2
        )
    engine.dispose()


def test_missing_current_proof_storage_cannot_admit_native_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    _config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    engine = create_database_engine(url)
    # Corrupt only this isolated fixture: a current stamp cannot substitute
    # for the actual required proof storage. Historical observers remain
    # supported, but current mutation admission must fail before native work.
    from sqlalchemy import CheckConstraint, MetaData, Table

    table = Table(
        "mcp_workspace_deployment_operations", MetaData(), autoload_with=engine
    )
    for constraint in list(table.constraints):
        if isinstance(
            constraint, CheckConstraint
        ) and "managed_verification_json" in str(constraint.sqltext):
            table.constraints.remove(constraint)
    table._columns.remove(table.c.managed_verification_json)
    with engine.begin() as connection:
        table.drop(connection)
        table.create(connection)
    env = environment(
        tmp_path / "fixture", database_path=Path(url.removeprefix("sqlite:///"))
    )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_ambiguous"
    ) as caught:
        deploy(env)
    assert isinstance(caught.value.__cause__, SQLAlchemyError)
    assert "managed_verification_json" in str(caught.value.__cause__)
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        0,
        0,
        0,
    )
    engine.dispose()

"""Recovery columns preserve historical operations and reject evidence loss."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)


def test_upgrade_and_empty_downgrade_preserve_predecessor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0077_workspace_launch"
    )
    engine = create_database_engine(url)
    before = {
        c["name"]
        for c in inspect(engine).get_columns("mcp_workspace_deployment_operations")
    }
    command.upgrade(config, "0078_workspace_deploy_recovery")
    after = {
        c["name"]
        for c in inspect(engine).get_columns("mcp_workspace_deployment_operations")
    }
    assert after - before == {
        "original_container_id",
        "original_runtime_state_revision",
        "failure_phase",
        "failure_cause",
        "recovery_state",
        "recovery_digest",
        "recovery_at",
    }
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()
    command.downgrade(config, "0077_workspace_launch")
    assert {
        c["name"]
        for c in inspect(engine).get_columns("mcp_workspace_deployment_operations")
    } == before


def test_populated_predecessor_keeps_unknown_original_phase_and_refuses_evidence_loss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sqlalchemy import MetaData, Table, update

    from tests.application.test_workspace_deployment_recovery import broken

    fixture, row = broken(tmp_path / "fixture")
    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision="0077_workspace_launch"
    )
    engine = create_database_engine(url)
    previous = Table(
        "mcp_workspace_deployment_operations", MetaData(), autoload_with=engine
    )
    old = {column.name: getattr(row, column.name) for column in previous.columns}
    with engine.begin() as connection:
        connection.execute(previous.insert().values(**old))
    command.upgrade(config, "0078_workspace_deploy_recovery")
    current = Table(
        "mcp_workspace_deployment_operations", MetaData(), autoload_with=engine
    )
    with engine.begin() as connection:
        saved = connection.execute(current.select()).mappings().one()
        assert all(saved[key] == value for key, value in old.items())
        assert saved["failure_phase"] is None and saved["original_container_id"] is None
        connection.execute(
            update(current).values(
                failure_phase="sync",
                failure_cause="sync_unconfirmed",
                recovery_state="still_requires_admin",
            )
        )
    with pytest.raises(RuntimeError, match="recovery evidence must be preserved"):
        command.downgrade(config, "0077_workspace_launch")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0078_workspace_deploy_recovery"
        )
        assert (
            connection.scalar(
                text("SELECT failure_phase FROM mcp_workspace_deployment_operations")
            )
            == "sync"
        )
    fixture.sessions.kw["bind"].dispose()


def test_recovery_coordinator_runs_on_the_upgraded_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from uuid import UUID, uuid4

    from sqlalchemy import MetaData, Table
    from sqlalchemy.orm import defer

    from kodi_lab.application.workspace_deployment_recovery import (
        WorkspaceDeploymentRecoveryService,
    )
    from kodi_lab.broker.docker_inventory import ContainerRuntimeState
    from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow
    from tests.application import test_mcp_workspace_development as fixture
    from tests.application.test_workspace_deployment_recovery import (
        EvidenceBroker,
    )

    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0077_workspace_launch"
    )
    command.upgrade(config, "0078_workspace_deploy_recovery")
    engine = create_database_engine(url)
    root = tmp_path / "fixture"
    env = fixture._environment(root, database_path=Path(str(engine.url.database)))
    # Current deployment admission requires its matched head schema. This older
    # transition seeds only the actual 0078 interrupted-receipt vocabulary, then
    # exercises the real recovery coordinator without disabling current guards.
    assert "development_session_policy" not in inspect(engine).get_table_names()
    historical = Table(
        "mcp_workspace_deployment_operations", MetaData(), autoload_with=engine
    )
    operation_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            historical.insert().values(
                id=operation_id,
                client_id=str(env.client_id),
                initiator_kind="mcp",
                request_id=str(uuid4()),
                workspace_id=str(env.workspace_id),
                development_source_id=str(fixture.SOURCE_ID),
                target_id=str(fixture.TARGET_ID),
                runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                target_class="internal_lab",
                deployment_mode="direct",
                generation=0,
                addon_id="plugin.video.slice4",
                addon_version="1.0.0",
                state="reconciliation_required",
                phase="finalize",
                mutation_started=True,
                target_stopped=True,
                sync_started=False,
                sync_completed=False,
                target_started=False,
                inventory_classification="not_checked",
                failure_code="deployment_ambiguous",
                failure_phase="stop",
                failure_cause="stop_unconfirmed",
                original_container_id=fixture.CONTAINER_ID,
                original_runtime_state_revision=0,
                requested_at=fixture.NOW_TEXT,
                started_at=fixture.NOW_TEXT,
                completed_at=fixture.NOW_TEXT,
            )
        )
    env.broker.state = ContainerRuntimeState.STOPPED
    with env.sessions() as session:
        row = session.get(
            McpWorkspaceDeploymentOperationRow,
            operation_id,
            options=[
                defer(McpWorkspaceDeploymentOperationRow.managed_verification_json)
            ],
        )
        assert row is not None
        session.expunge(row)
    broker = EvidenceBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=root / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"
    assert not result.deployment_succeeded and broker.reads == 1
    assert "development_session_policy" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0078_workspace_deploy_recovery"
        )
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()

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
    from uuid import UUID

    from kodi_lab.application.workspace_deployment_recovery import (
        WorkspaceDeploymentRecoveryService,
    )
    from tests.application import test_mcp_workspace_development as fixture
    from tests.application.test_workspace_deployment_recovery import (
        EvidenceBroker,
        broken,
    )

    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0077_workspace_launch"
    )
    command.upgrade(config, "0078_workspace_deploy_recovery")
    engine = create_database_engine(url)
    root = tmp_path / "fixture"
    env, row = broken(root, database_path=Path(str(engine.url.database)))
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, EvidenceBroker(env, row), data_root=root / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"
    assert not result.deployment_succeeded
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()

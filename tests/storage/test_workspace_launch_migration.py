"""A populated predecessor retains evidence and admits only the new typed launch."""

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, select, text

from kodi_lab.application.workspace_launch import WorkspaceLaunchService
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_launch import Bridge, launch
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)


def test_populated_upgrade_runs_exact_launch_and_lossy_downgrade_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0076_managed_resource_policy"
    )
    # Current coordinators need the current columns. Prepare their fixture at
    # head, remove only the later nullable binding evidence, then return to the
    # exact historical schema before exercising the 76 -> 77 transition.
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    path = Path(str(engine.url.database))
    env = fixture._environment(tmp_path / "fixture", database_path=path)
    fixture._deploy_workspace(env)
    manifest = capture_workspace_deployment_tree(
        tmp_path / "fixture/data", env.workspace_id
    )
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert workspace is not None and deployment is not None
        deployment.source_manifest_digest = workspace.current_tree_digest
        deployment.verified_destination_digest = expected_destination_digest(
            manifest, runtime_uid=1000, runtime_gid=1000
        )
        deployment.original_container_id = None
        deployment.original_runtime_state_revision = None
    command.downgrade(config, "0076_managed_resource_policy")
    with engine.connect() as connection:
        prior = (
            connection.execute(
                text("SELECT * FROM mcp_workspace_deployment_operations ORDER BY id")
            )
            .tuples()
            .all()
        )
        triggers = (
            connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name"
                )
            )
            .tuples()
            .all()
        )
    command.upgrade(config, "0077_workspace_launch")
    assert "mcp_workspace_launches" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT * FROM mcp_workspace_deployment_operations ORDER BY id")
            )
            .tuples()
            .all()
            == prior
        )
        assert (
            connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name"
                )
            )
            .tuples()
            .all()
            == triggers
        )
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()
    command.upgrade(config, "head")
    bridge = Bridge()
    service = WorkspaceLaunchService(
        env.sessions,
        env.development,
        bridge,
        data_root=tmp_path / "fixture/data",
        clock=lambda: fixture.NOW + timedelta(minutes=1),
    )
    assert launch(env, service).state == "accepted"
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceLaunchRow)) is not None
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(config, "0076_managed_resource_policy")


def test_empty_launch_revision_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0076_managed_resource_policy"
    )
    command.upgrade(config, "0077_workspace_launch")
    command.downgrade(config, "0076_managed_resource_policy")
    assert (
        "mcp_workspace_launches"
        not in inspect(create_database_engine(url)).get_table_names()
    )

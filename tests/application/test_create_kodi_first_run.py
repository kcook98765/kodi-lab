"""Real clean-host initialization and Create stages with fake native transports."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import admin_cli
from kodi_lab.domain import ManagedInstanceLifecycleOperationState
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateStage
from kodi_lab.remote_lifecycle import initialize, lifecycle_lock
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage import packaged_migrations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import read_resource_policy, resource_policy
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedResourcePolicyRow,
    ManagedStartAuthorizationRow,
)
from kodi_lab.storage.packaged_migrations import migrate_database
from tests.create_kodi_start_support import Environment
from tests.storage.test_remote_lifecycle import (
    repository_migrations as repository_migrations,
)


@pytest.fixture
def factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    root = tmp_path / "state"
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    url = f"sqlite:///{root / 'database' / 'kodi-lab.db'}"
    initialize(root, url)
    engine = create_database_engine(url)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def test_first_run_intent_registration_provision_start_without_development(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        policy = session.get(ManagedResourcePolicyRow, 1)
        assert policy and (policy.max_running, policy.idle_minutes) == (1, 60)
        assert session.get(DevelopmentSessionPolicyRow, 1) is None
    # Exercise the actual Create/install/register/provision/lifecycle/capacity
    # services. Only reviewed acquisition, Docker and native transports are fake.
    env = Environment(factory, fresh_install=True, retained=False)
    try:
        provisioned = env.op
        assert provisioned.stage is CreateStage.PROVISIONING
        assert provisioned.checkpoint is CreateCheckpoint.CONFIRMED
        env.advance()
        result = env.start.start(env.op.id, version=env.op.version)
        assert result.stage is CreateStage.STARTUP
        assert result.checkpoint is CreateCheckpoint.CONFIRMED
        assert (
            result.container_id,
            result.runtime_revision_id,
            result.image_id,
        ) == (
            provisioned.container_id,
            provisioned.runtime_revision_id,
            provisioned.image_id,
        )
        assert env.broker.starts == 1
        assert env.start.start(result.id, version=result.version) == result
        assert env.broker.starts == 1
        with factory() as session:
            child = session.get(
                ManagedInstanceLifecycleOperationRow, str(result.start_operation_id)
            )
            proof = session.get(
                ManagedStartAuthorizationRow, str(result.start_operation_id)
            )
            assert (
                child
                and child.state is ManagedInstanceLifecycleOperationState.SUCCEEDED
            )
            assert proof and proof.consumed_at
            assert read_resource_policy(session).max_running == 1
            assert session.get(DevelopmentSessionPolicyRow, 1) is None
            assert session.scalars(select(DevelopmentSessionRow)).all() == []
            assert session.scalars(select(AgentWorkspaceRow)).all() == []
    finally:
        env.close()


@pytest.mark.parametrize("limit", [1, 4, 64])
@pytest.mark.parametrize("retained", [False, True])
def test_existing_policy_survives_workflow_upgrade_and_reinitialization(
    factory: sessionmaker[Session], tmp_path: Path, limit: int, retained: bool
) -> None:
    root = tmp_path / "state"
    path = root / "database" / "kodi-lab.db"
    url = f"sqlite:///{path}"
    with factory.begin() as session:
        policy = resource_policy(session)
        policy.max_running, policy.idle_minutes = limit, None
        session.add(
            DevelopmentSessionPolicyRow(
                id=1,
                retain_profiles=retained,
                retained_omega_target_id=str(
                    recipe_target_id(load_installation_recipe("omega"))
                )
                if retained
                else None,
            )
        )
    with packaged_migrations.packaged_config(url) as config:
        command.downgrade(config, "0091_managed_start_authorization")
    with lifecycle_lock(root):
        migrate_database(path, url)
    initialize(root, url)
    with factory() as session:
        assert (
            resource_policy(session).max_running,
            read_resource_policy(session).idle_minutes,
        ) == (limit, None)
        selection = session.get(DevelopmentSessionPolicyRow, 1)
        assert selection and selection.retain_profiles is retained
        assert selection.retained_omega_target_id == (
            str(recipe_target_id(load_installation_recipe("omega")))
            if retained
            else None
        )


def test_unrecorded_read_and_row_initialization_share_defaults(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        row = session.get(ManagedResourcePolicyRow, 1)
        assert row
        session.delete(row)
    with factory.begin() as session:
        assert read_resource_policy(session).max_running == 1
        assert session.get(ManagedResourcePolicyRow, 1) is None
        assert resource_policy(session).max_running == 1
        resource_policy(session).max_running = 4
        session.flush()
        assert resource_policy(session).max_running == 4


@pytest.mark.parametrize("empty_file", [False, True])
def test_local_host_migrate_initializes_once_and_preserves_recorded_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_file: bool
) -> None:
    root = tmp_path / "state"
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    monkeypatch.delenv("KODI_LAB_REMOTE_CONTAINER", raising=False)
    path = root / "database" / "kodi-lab.db"
    if empty_file:
        path.parent.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        path.touch(mode=0o600)
    assert admin_cli.main(["migrate"]) == 0
    engine = create_database_engine(f"sqlite:///{path}")
    sessions = create_session_factory(engine)
    try:
        with sessions.begin() as session:
            assert resource_policy(session).max_running == 1
            resource_policy(session).max_running = 4
        assert admin_cli.main(["migrate"]) == 0
        with sessions() as session:
            assert read_resource_policy(session).max_running == 4
    finally:
        engine.dispose()

"""Four retained profiles share one global running slot, including legacy settings."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.managed_agent_lifecycle import (
    ManagedAgentLifecycleError,
    ManagedAgentLifecycleService,
)
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.domain.create_kodi import CreateAcceptance, CreateCheckpoint
from kodi_lab.kodi.readiness import ReadinessFailureReason
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.managed_activity import resource_policy
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedStartAuthorizationRow,
)
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_managed_start_selection import setup
from tests.create_kodi_runner_support import Environment
from tests.create_kodi_support import accept


def test_install_all_four_with_one_running_and_finish_stopped(
    factory: sessionmaker[Session],
) -> None:
    env = Environment(factory, start_after_setup=False)
    env.broker.unique_containers = True
    app = CreateKodiService(env.repo)
    try:
        for generation in ("omega", "matrix", "nexus", "piers"):
            if generation != "omega":
                env.op = accept(
                    app,
                    generation=generation,
                    start_after_setup=False,
                    preview_approved=generation == "piers",
                )
                env.broker.parent = env.op.id
            for _ in range(20):
                if env.op.checkpoint is CreateCheckpoint.COMPLETE:
                    break
                env.step()
            assert env.op.checkpoint is CreateCheckpoint.COMPLETE, env.op
            assert env.op.stage == "provisioning"
            assert env.op.start_operation_id is None
            assert env.broker.starts == 0
            if generation == "omega":
                # Model an independently started exact instance filling the one slot.
                env.lifecycle.start_managed(env.op.target_id)
                env.broker.starts = 0
        assert len(env.broker.records) == 4
        assert sum(r.runtime_state == "running" for r in env.broker.records) == 1
        assert resource_policy_value(factory) == 1
        for target in [UUID(r.managed_instance_id) for r in env.broker.records]:
            for running in list(env.broker.records):
                if running.runtime_state == "running":
                    env.lifecycle.stop(UUID(running.managed_instance_id))
            env.lifecycle.start_managed(target)
            assert sum(r.runtime_state == "running" for r in env.broker.records) == 1
        assert len(env.broker.records) == 4
    finally:
        env.close()


def resource_policy_value(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return resource_policy(session).max_running


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 4])
def test_legacy_limits_cannot_expand_running_capacity_or_change_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, retained: bool
) -> None:
    env = setup(tmp_path, monkeypatch)
    s = env.s
    s.choose("omega")
    with s.env.sessions.begin() as session:
        resource_policy(session).max_running = limit
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy
        policy.retain_profiles = retained
        if not retained:
            policy.retained_omega_target_id = None
    with s.env.sessions() as session:
        selection = session.execute(select(DevelopmentSessionRow.__table__)).all()
    profiles = {key: path.read_bytes() for key, path in s.profiles.items()}
    resources = ManagedResourceService(s.env.sessions, env.broker, env.lifecycle)
    try:
        targets = list(s.ids.values())
        env.lifecycle.start_managed(targets[0])
        assert resources.capacity().limit == 1
        assert resources.capacity().used == 1
        assert resource_policy_value(s.env.sessions) == limit
        with pytest.raises((ManagedCapacityError, DevelopmentSessionConflict)):
            env.lifecycle.start_managed(targets[1])
        with s.env.sessions() as session:
            assert (
                session.execute(select(DevelopmentSessionRow.__table__)).all()
                == selection
            )
        assert {key: path.read_bytes() for key, path in s.profiles.items()} == profiles
        # Lowering a limit preserves already running instances and blocks new work.
        with s.env.sessions.begin() as session:
            resource_policy(session).max_running = 1
        assert resources.capacity().used == 1
        assert not resources.capacity().available
    finally:
        env.readiness.close()


def test_concurrent_web_starts_cannot_expand_single_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = setup(tmp_path, monkeypatch)
    try:
        with env.s.env.sessions.begin() as session:
            resource_policy(session).max_running = 4
        targets = list(env.s.ids.values())
        with ThreadPoolExecutor(max_workers=2) as workers:
            starts = [
                workers.submit(env.lifecycle.start_managed, t) for t in targets[:2]
            ]
            outcomes = []
            for start in starts:
                try:
                    start.result(timeout=10)
                    outcomes.append(True)
                except (ManagedCapacityError, DevelopmentSessionConflict):
                    outcomes.append(False)
        assert sorted(outcomes) == [False, True]
        assert env.broker.starts == 1
        assert sum(r.runtime_state == "running" for r in env.s.records) == 1
        with pytest.raises((ManagedCapacityError, DevelopmentSessionConflict)):
            env.lifecycle.start_managed(targets[2])
        with env.s.env.sessions() as session:
            authorization = session.scalar(
                select(ManagedStartAuthorizationRow).where(
                    ManagedStartAuthorizationRow.target_id == str(targets[2])
                )
            )
            assert authorization is None or authorization.consumed_at is None
    finally:
        env.readiness.close()


@pytest.mark.parametrize("agent_first", [False, True])
def test_web_and_mcp_lifecycle_share_admission_without_changing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_first: bool
) -> None:
    env = setup(tmp_path, monkeypatch)
    s = env.s
    s.choose("omega")
    client = McpAdministrationService(s.env.sessions).create_client(
        display_name="Concurrent lifecycle fixture",
        preset_id=ADDON_DEVELOPMENT_PRESET_ID,
    )
    status = ManagedRuntimeStatusService(s.env.sessions, env.broker, env.readiness)
    resources = ManagedResourceService(s.env.sessions, env.broker, env.lifecycle)
    agent = ManagedAgentLifecycleService(
        s.env.sessions,
        resources,
        env.lifecycle,
        status,
        lock_root=tmp_path / "agent-locks",
        rate_limiter=BoundedIntervalRateLimiter(0),
    )

    def agent_start() -> object:
        # Transport/policy authorization is covered by the MCP route tests.
        return agent.ensure_ready(
            client_id=client.id,
            request_id=uuid4(),
            target_id=s.ids["omega"],
            authorize=lambda _session: None,
            hold_minutes=0,
        )

    def web_start() -> object:
        return env.lifecycle.start_managed(s.ids["nexus"])

    models = (AgentWorkspaceRow, DevelopmentSourceRow, DevelopmentSessionRow)
    with s.env.sessions() as session:
        before = [session.execute(select(model.__table__)).all() for model in models]
    profiles = {key: path.read_bytes() for key, path in s.profiles.items()}
    env.broker.entered, env.broker.finish = Event(), Event()
    first, second = (
        (agent_start, web_start) if agent_first else (web_start, agent_start)
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            started = worker.submit(first)
            try:
                assert env.broker.entered.wait(5)
                with pytest.raises(
                    (
                        ManagedCapacityError,
                        DevelopmentSessionConflict,
                        ManagedAgentLifecycleError,
                    )
                ):
                    second()
            finally:
                env.broker.finish.set()
            started.result(timeout=5)
        assert env.broker.starts == 1
        assert sum(r.runtime_state == "running" for r in s.records) == 1
        assert {key: path.read_bytes() for key, path in s.profiles.items()} == profiles
        with s.env.sessions() as session:
            assert [
                session.execute(select(model.__table__)).all() for model in models
            ] == before
    finally:
        env.broker.finish.set()
        env.readiness.close()


def test_setup_replay_cannot_change_start_or_preview_approval(
    factory: sessionmaker[Session],
) -> None:
    from kodi_lab.domain.create_kodi import CreateConflict
    from kodi_lab.runtime.recipes import load_installation_recipe

    app = CreateKodiService(CreateKodiRepository(factory))
    request = uuid4()

    def submit(
        *, start_after_setup: bool = False, preview_approved: bool = True
    ) -> CreateAcceptance:
        return app.accept(
            request_id=request,
            generation="piers",
            recipe_digest=load_installation_recipe("piers").digest,
            owner_approved=True,
            start_after_setup=start_after_setup,
            preview_approved=preview_approved,
        )

    original = submit().operation
    assert submit().operation == original
    with pytest.raises(CreateConflict):
        submit(start_after_setup=True)
    with pytest.raises(ValueError, match="prerelease approval"):
        submit(preview_approved=False)


@pytest.mark.parametrize("ordinal", ["1", "2"])
def test_piers_explicit_start_finishes_with_its_own_native_identity(
    factory: sessionmaker[Session],
    ordinal: str,
) -> None:
    env = Environment(factory, generation="piers", preview_approved=True)
    env.transport.native_version = {
        "major": 22,
        "minor": 0,
        "tag": "releasecandidate",
        "tagversion": ordinal,
    }
    try:
        for _ in range(24):
            if env.op.checkpoint is CreateCheckpoint.COMPLETE:
                break
            env.step()
        if ordinal == "2":
            assert env.op.checkpoint is CreateCheckpoint.COMPLETE, env.op
        else:
            inspection = env.native.inspect(env.op.id, version=env.op.version)
            assert inspection.operation.checkpoint is CreateCheckpoint.DISPATCHED
            assert inspection.result is not None
            assert inspection.result.failure_reason is (
                ReadinessFailureReason.UNEXPECTED_KODI_VERSION
            )
            assert inspection.result.identity is not None
            assert inspection.result.identity.tag_version == 1
        assert env.op.stage == "readiness" and env.op.start_after_setup
        assert env.broker.starts == 1
        assert "Application.GetProperties" in env.transport.methods
    finally:
        env.close()


def test_prepare_another_reviewed_profile_preserves_legacy_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.docker_inventory import ProvisionedManagedContainerRecord

    env = setup(tmp_path, monkeypatch)
    s = env.s
    try:
        s.choose("omega")
        with s.env.sessions.begin() as session:
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy
            policy.retain_profiles = False
            policy.retained_omega_target_id = None
        before = s.service.status()
        target = s.ids["matrix"]
        stopped = next(r for r in s.records if r.managed_instance_id == str(target))
        s.records.remove(stopped)

        def dispatch() -> ProvisionedManagedContainerRecord:
            s.records.append(stopped)
            return ProvisionedManagedContainerRecord(
                stopped.managed_instance_id,
                str(stopped.runtime_revision_id),
                stopped.container_id,
                stopped.image_id,
                stopped.runtime_state,
                True,
            )

        s.admission.run_provision(
            instance_id=str(target),
            revision_id=str(s.revisions[target]),
            dispatch=dispatch,
        )
        assert s.service.status() == before
        assert len(s.records) == 4
        assert all(r.runtime_state == "stopped" for r in s.records)
    finally:
        env.readiness.close()


def test_retained_stopped_provision_is_confirmed_while_another_version_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.docker_inventory import ProvisionedManagedContainerRecord
    from kodi_lab.storage.models import ManagedProvisionReservationRow

    env = setup(tmp_path, monkeypatch)
    s = env.s
    try:
        env.lifecycle.start_managed(s.ids["omega"])
        target = s.ids["matrix"]
        stopped = next(r for r in s.records if r.managed_instance_id == str(target))
        s.records.remove(stopped)

        def dispatch() -> ProvisionedManagedContainerRecord:
            s.records.append(stopped)
            return ProvisionedManagedContainerRecord(
                stopped.managed_instance_id,
                str(stopped.runtime_revision_id),
                stopped.container_id,
                stopped.image_id,
                stopped.runtime_state,
                True,
            )

        assert s.admission.run_provision(
            instance_id=str(target),
            revision_id=str(s.revisions[target]),
            dispatch=dispatch,
        ).created
        with s.env.sessions() as session:
            assert session.get(ManagedProvisionReservationRow, str(target)) is None
        assert sum(record.runtime_state == "running" for record in s.records) == 1
        assert s.service.status() is None
    finally:
        env.readiness.close()

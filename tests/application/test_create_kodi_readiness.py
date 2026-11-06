"""Durable native readiness, fresh JSON-RPC and conservative explicit rechecks."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import BoundedSemaphore, Event
from typing import Any
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_readiness as module
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.domain import InstanceRuntimeState, ManagedInstanceSemanticState
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as State
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.kodi.readiness import ReadinessCancelledError
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import resource_policy
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedResourcePolicyRow,
    ManagedStartAuthorizationRow,
    RuntimeExecutionProfileRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Interrupted
from tests.create_kodi_readiness_support import Broker, Transport, service
from tests.create_kodi_start_support import Environment as StartEnvironment
from tests.runtime.test_recipe_installation import _ready


class Environment:
    def __init__(
        self, sessions: sessionmaker[Session], *, retained: bool = False
    ) -> None:
        self.started = StartEnvironment(sessions, retained=retained)
        self.started.advance()
        self.op = self.started.start.start(
            self.started.op.id, version=self.started.op.version
        )
        self.started.close()
        assert self.op.checkpoint is CreateCheckpoint.CONFIRMED
        self.sessions, self.repo = sessions, self.started.repo
        self.broker = Broker(sessions, self.started.broker.records[0])
        self.transport = Transport()
        self.service = service(sessions, self.repo, self.broker, self.transport)

    def advance(self) -> None:
        self.op = self.service.advance(self.op.id, version=self.op.version)

    def inspect(self) -> None:
        inspected = self.service.inspect(self.op.id, version=self.op.version)
        self.op = inspected.operation


@pytest.fixture
def env(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Environment]:
    value = Environment(factory)
    from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
    from kodi_lab.application.runtime_installations import RuntimeInstallationService

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("native readiness must not install, reconcile or prepare Bridge")

    monkeypatch.setattr(ManagedBridgeTrustService, "prepare_background", forbidden)
    monkeypatch.setattr(RuntimeInstallationService, "start", forbidden)
    monkeypatch.setattr(RuntimeInstallationService, "_reconcile", forbidden)
    yield value
    assert value.started.broker.starts == 1 and value.started.broker.stops == 0
    assert value.broker.starts == value.broker.stops == value.broker.calls == 0


def test_exact_native_completion_preserves_start_provision_and_projection(
    env: Environment,
) -> None:
    start = env.op
    env.advance()
    assert (env.op.stage, env.op.checkpoint) == (
        CreateStage.READINESS,
        CreateCheckpoint.ACCEPTED,
    )
    assert env.transport.methods == [] and env.broker.observations == 0
    env.inspect()
    assert env.op.checkpoint is CreateCheckpoint.COMPLETE
    assert env.op.completed_at and env.op.outcome == "succeeded"
    assert env.op.child_id == uuid5(env.op.id, "create-kodi:readiness:v1")
    assert (
        env.op.start_operation_id,
        env.op.provision_operation_id,
        env.op.container_id,
        env.op.image_id,
        env.op.runtime_revision_id,
        env.op.runtime_state_revision,
    ) == (
        start.start_operation_id,
        start.provision_operation_id,
        start.container_id,
        start.image_id,
        start.runtime_revision_id,
        start.runtime_state_revision,
    )
    assert env.transport.methods == ["JSONRPC.Ping", "Application.GetProperties"]
    with env.sessions() as session:
        assert session.scalars(select(AgentWorkspaceRow)).all() == []
        assert session.scalars(select(DevelopmentSessionRow)).all() == []


def test_delayed_native_process_becomes_usable_within_existing_bound(
    env: Environment,
) -> None:
    env.broker.starting_observations = 2
    env.advance()
    env.inspect()
    assert env.op.checkpoint is CreateCheckpoint.COMPLETE
    assert env.broker.observations == 3 and env.transport.clock.value > 0


@pytest.mark.parametrize("unsafe_inventory", [False, True])
def test_retained_mode_keeps_exact_inventory_fences_without_workspace(
    factory: sessionmaker[Session], unsafe_inventory: bool
) -> None:
    env = Environment(factory, retained=True)
    with factory.begin() as session:
        policy = resource_policy(session)
        policy.max_running = 4
    if unsafe_inventory:
        env.broker.records.append(
            replace(
                env.broker.records[0],
                managed_instance_id=str(uuid4()),
                container_id="f" * 64,
                runtime_state=ContainerRuntimeState.STOPPED,
            )
        )
        with pytest.raises(DevelopmentSessionConflict):
            env.advance()
        assert env.transport.methods == []
    else:
        env.advance()
        env.inspect()
        assert env.op.checkpoint is CreateCheckpoint.COMPLETE
        with factory() as session:
            assert session.scalars(select(DevelopmentSessionRow)).all() == []
            assert session.scalars(select(AgentWorkspaceRow)).all() == []
    assert env.broker.starts == env.broker.stops == 0


def test_higher_nonretained_capacity_policy_preserves_readiness(
    env: Environment,
) -> None:
    with env.sessions.begin() as session:
        policy = resource_policy(session)
        policy.max_running = 4
    env.advance()
    assert env.transport.methods == []
    with env.sessions() as session:
        recorded = session.get(ManagedResourcePolicyRow, 1)
        assert recorded and recorded.max_running == 4
    env.inspect()
    assert env.op.checkpoint is CreateCheckpoint.COMPLETE


@pytest.mark.parametrize("mode", ["unreachable", "invalid", "name", "version"])
def test_unsuccessful_native_observation_allows_only_explicit_readonly_recheck(
    env: Environment, mode: str
) -> None:
    env.advance()
    env.transport.mode = mode
    env.inspect()
    waiting = env.op
    assert waiting.checkpoint is CreateCheckpoint.DISPATCHED
    assert waiting.outcome is waiting.completed_at is None
    requests = len(env.transport.methods)
    for _ in range(3):
        assert env.repo.get(waiting.id) == waiting
        assert env.repo.for_target(waiting.target_id) == waiting
    assert len(env.transport.methods) == requests
    env.transport.mode = "ready"
    env.inspect()
    assert env.op.checkpoint is CreateCheckpoint.COMPLETE
    assert env.op.child_id == waiting.child_id


def test_running_container_without_native_process_never_completes(
    env: Environment,
) -> None:
    env.broker.process_state = KodiProcessState.NOT_RUNNING
    env.advance()
    env.inspect()
    waiting = env.op
    assert waiting.checkpoint is CreateCheckpoint.DISPATCHED
    assert env.transport.methods == []
    env.broker.process_state = KodiProcessState.RUNNING
    env.inspect()
    assert env.op.checkpoint is CreateCheckpoint.COMPLETE


@pytest.mark.parametrize(
    "when", ["planned", "before_probe", "after_ping", "after_identity"]
)
def test_interrupted_probe_keeps_same_identity_and_reestablishes_after_restart(
    env: Environment, when: str
) -> None:
    env.advance()
    if when == "planned":
        env.op = env.repo.plan(env.op.id, version=env.op.version)
    elif when == "before_probe":
        env.op = env.repo.plan(env.op.id, version=env.op.version)
        env.op = env.repo.claim_readiness_probe(
            env.op.id, version=env.op.version, verify=env.service._admit
        )
    else:

        def interrupt() -> None:
            if len(env.transport.methods) == (1 if when == "after_ping" else 2):
                raise Interrupted()

        env.transport.on_request = interrupt
        with pytest.raises(Interrupted):
            env.inspect()
        found = env.repo.get(env.op.id)
        assert found
        env.op = found
    old = env.op
    # Lose every process-local registry/adapter/connection. Only durable records
    # and newly supplied read-only broker/HTTP observations are available.
    with env.sessions() as session:
        url = str(session.get_bind().engine.url)
    engine = create_database_engine(url)
    try:
        sessions = create_session_factory(engine)
        repo = CreateKodiRepository(sessions)
        transport = Transport()
        restarted = service(sessions, repo, env.broker, transport)
        inspected = restarted.inspect(old.id, version=old.version)
        assert inspected.operation.checkpoint is CreateCheckpoint.COMPLETE
        assert inspected.operation.child_id == old.child_id
        assert transport.methods == ["JSONRPC.Ping", "Application.GetProperties"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "change",
    [
        "container",
        "image",
        "revision",
        "missing",
        "stopped",
        "projection",
        "pin",
        "profile",
    ],
)
@pytest.mark.parametrize("phase", ["advance", "confirmation"])
def test_identity_drift_is_never_durable_readiness(
    env: Environment, change: str, phase: str
) -> None:
    def drift() -> None:
        revision = None
        if change == "pin":
            installed = _ready(env.sessions, generation="nexus")
            with env.sessions() as session:
                other = session.get(RuntimeRecipeInstallationRow, str(installed))
                assert other and other.runtime_revision_id
                revision = other.runtime_revision_id
        if change in {"projection", "pin", "profile"}:
            with env.sessions.begin() as session:
                if change == "profile":
                    profile_row = session.get(
                        RuntimeExecutionProfileRow, str(env.op.runtime_revision_id)
                    )
                    assert profile_row
                    profile_row.http_timeout_seconds = 2
                else:
                    row = session.get(ManagedInstanceRow, str(env.op.target_id))
                    assert row
                    if change == "projection":
                        row.runtime_state_revision += 1
                    else:
                        assert revision
                        row.runtime_revision_id = revision
        elif change == "missing":
            env.broker.records.clear()
        else:
            changes: dict[str, dict[str, Any]] = {
                "container": {"container_id": "f" * 64},
                "image": {"image_id": "sha256:" + "e" * 64},
                "revision": {"runtime_revision_id": str(uuid4())},
                "stopped": {"runtime_state": ContainerRuntimeState.STOPPED},
            }
            env.broker.records[0] = replace(env.broker.records[0], **changes[change])

    if phase == "advance":
        drift()
        with pytest.raises((CreateTransitionError, ValueError)):
            env.advance()
    else:
        env.advance()
        env.transport.on_request = lambda: (
            drift() if len(env.transport.methods) == 2 else None
        )
        with pytest.raises((CreateTransitionError, ValueError)):
            env.inspect()
    op = env.repo.get(env.op.id)
    assert op and op.checkpoint is not CreateCheckpoint.COMPLETE


@pytest.mark.parametrize(
    "change",
    [
        "start_missing",
        "start_failed",
        "unconsumed",
        "auth_container",
        "auth_projection",
        "auth_time",
        "provision",
        "lease",
        "active_lifecycle",
        "reconciliation",
    ],
)
def test_original_start_and_idle_recovery_evidence_required(
    env: Environment, change: str
) -> None:
    with env.sessions.begin() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(env.op.start_operation_id)
        )
        auth = session.get(ManagedStartAuthorizationRow, str(env.op.start_operation_id))
        assert child and auth
        if change == "start_missing":
            session.delete(auth)
            session.flush()
            session.delete(child)
        elif change == "start_failed":
            child.state, child.failure_reason = State.FAILED, "fixture failure"
        elif change == "unconsumed":
            auth.consumed_at = None
        elif change == "auth_container":
            auth.container_id = "f" * 64
        elif change == "auth_projection":
            auth.runtime_state_revision += 1
        elif change == "auth_time":
            auth.consumed_at = "2999-01-01T00:00:00.000000Z"
        elif change == "provision":
            provision = session.get(
                ManagedInstanceLifecycleOperationRow, str(env.op.provision_operation_id)
            )
            assert provision
            provision.original_managed_instance_id = str(uuid4())
        elif change == "lease":
            session.add(
                ManagedInstanceMutationLeaseRow(
                    managed_instance_id=str(env.op.target_id),
                    operation_id=str(uuid4()),
                    operation_kind="lifecycle",
                )
            )
        else:
            session.add(
                ManagedInstanceLifecycleOperationRow(
                    id=str(uuid4()),
                    original_managed_instance_id=str(env.op.target_id),
                    managed_instance_id=str(env.op.target_id),
                    instance_name="Pending Stop",
                    kind="stop",
                    requested_at=child.requested_at,
                    started_at=child.started_at,
                    completed_at=None
                    if change == "active_lifecycle"
                    else child.completed_at,
                    prior_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                    state=State.RUNNING
                    if change == "active_lifecycle"
                    else State.RECONCILIATION_REQUIRED,
                    resulting_state=None
                    if change == "active_lifecycle"
                    else "ambiguous",
                    failure_reason=None
                    if change == "active_lifecycle"
                    else "fixture ambiguous",
                    reconciliation_required=change == "reconciliation",
                )
            )
    with pytest.raises((CreateTransitionError, ValueError)):
        env.advance()
    assert env.transport.methods == []


def test_native_observation_of_another_container_is_rejected(env: Environment) -> None:
    env.advance()
    env.broker.observation_patch = {"container_id": "f" * 64}
    with pytest.raises(CreateTransitionError, match="observation_identity"):
        env.inspect()
    assert env.transport.methods == []


def test_duplicate_request_and_superseded_probe_cannot_overwrite_latest_result(
    env: Environment,
) -> None:
    env.advance()
    original = env.op
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(5)

    env.transport.on_request = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(env.service.inspect, original.id, version=original.version)
        try:
            assert entered.wait(5)
            with pytest.raises(CreateTransitionError, match="stale"):
                env.service.inspect(original.id, version=original.version)
            pending = env.repo.get(original.id)
            assert pending and pending.checkpoint is CreateCheckpoint.DISPATCHED
            # An explicit newer read-only inspection supersedes the old version.
            latest = service(env.sessions, env.repo, env.broker, Transport()).inspect(
                pending.id, version=pending.version
            )
            assert latest.operation.checkpoint is CreateCheckpoint.COMPLETE
        finally:
            release.set()
        with pytest.raises((CreateTransitionError, ReadinessCancelledError)):
            old.result(timeout=5)
    assert env.repo.get(original.id) == latest.operation


def test_busy_probe_capacity_does_not_mutate_parent_or_supersede_owner(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_PROBES", BoundedSemaphore(1))
    env.advance()
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(5)

    env.transport.on_request = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(env.service.inspect, env.op.id, version=env.op.version)
        try:
            assert entered.wait(5)
            pending = env.repo.get(env.op.id)
            assert pending
            with pytest.raises(CreateTransitionError, match="native_readiness_busy"):
                env.service.inspect(pending.id, version=pending.version)
            assert env.repo.get(env.op.id) == pending
        finally:
            release.set()
        assert (
            active.result(timeout=5).operation.checkpoint is CreateCheckpoint.COMPLETE
        )


@pytest.mark.parametrize("change", ["receipt", "authorization", "projection", "lease"])
def test_final_writer_rechecks_original_evidence_after_native_reply(
    env: Environment, change: str
) -> None:
    env.advance()

    def drift() -> None:
        if len(env.transport.methods) != 2:
            return
        with env.sessions.begin() as session:
            if change == "receipt":
                row = session.get(
                    ManagedInstanceLifecycleOperationRow, str(env.op.start_operation_id)
                )
                assert row
                row.original_managed_instance_id = str(uuid4())
            elif change == "authorization":
                auth = session.get(
                    ManagedStartAuthorizationRow, str(env.op.start_operation_id)
                )
                assert auth
                auth.consumed_at = None
            elif change == "projection":
                instance = session.get(ManagedInstanceRow, str(env.op.target_id))
                assert instance
                instance.runtime_state_revision += 1
            else:
                session.add(
                    ManagedInstanceMutationLeaseRow(
                        managed_instance_id=str(env.op.target_id),
                        operation_id=str(uuid4()),
                        operation_kind="lifecycle",
                    )
                )

    env.transport.on_request = drift
    with pytest.raises(CreateTransitionError):
        env.inspect()
    found = env.repo.get(env.op.id)
    assert found and found.checkpoint is CreateCheckpoint.DISPATCHED


def test_probe_claim_cannot_replay_any_start_stage(env: Environment) -> None:
    original = env.op
    with pytest.raises(CreateTransitionError):
        env.repo.claim_readiness_probe(
            original.id, version=original.version, verify=env.service._admit
        )
    assert env.repo.get(original.id) == original
    assert env.transport.methods == []


def test_historical_completion_and_receipt_retention_after_manual_stop(
    env: Environment,
) -> None:
    env.advance()
    env.inspect()
    completed = env.op
    requests = len(env.transport.methods)
    # Model the result of a later administrator Stop, not a readiness side effect.
    env.broker.records[0] = replace(
        env.broker.records[0], runtime_state=ContainerRuntimeState.STOPPED
    )
    with env.sessions.begin() as session:
        row = session.get(ManagedInstanceRow, str(env.op.target_id))
        assert row
        row.runtime_state, row.runtime_state_revision = (
            InstanceRuntimeState.STOPPED,
            row.runtime_state_revision + 2,
        )
        ManagedInstanceLifecycleOperationRepository(session).prune_terminal(
            env.op.target_id, retain=0
        )
    env.inspect()
    assert env.op == completed
    assert len(env.transport.methods) == requests
    with env.sessions() as session:
        assert session.get(
            ManagedInstanceLifecycleOperationRow, str(completed.start_operation_id)
        )
        assert session.get(
            ManagedInstanceLifecycleOperationRow, str(completed.provision_operation_id)
        )
        assert session.get(
            ManagedStartAuthorizationRow, str(completed.start_operation_id)
        )

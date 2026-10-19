"""Idle policy uses real lifecycle coordination and preserves the existing target."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import (
    ManagedInstanceLifecycleOperationCoordinator,
)
from kodi_lab.application.managed_resources import (
    ManagedResourceProtected,
    ManagedResourceService,
    ResourcePolicyInput,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiApplicationIdentity,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.managed_activity import record_activity, timestamp
from kodi_lab.storage.models import (
    ManagedIdleStateRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceUseRow,
)
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository
from tests.application import test_lifecycle_history as history
from tests.application.test_runtime_execution_profile import values

factory = history.factory
INSTANCE_ID = history.INSTANCE_ID


class Broker:
    def __init__(self) -> None:
        self.running, self.stops, self.observations = True, 0, 0
        self.starts = 0
        self.stop_error = False
        self.start_error = False

    def managed_container_inventory(
        self, **_kwargs: object
    ) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id="a" * 64,
                    image_id="sha256:" + "b" * 64,
                    runtime_state=ContainerRuntimeState.RUNNING
                    if self.running
                    else ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.HEALTHY,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, **_kwargs: object
    ) -> ObserveManagedKodiRuntimeResult:
        self.observations += 1
        assert self.running
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(history.REVISION_ID),
            container_id="a" * 64,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.10",
            jsonrpc_port=8080,
        )

    def stop_managed_container(self, **_kwargs: object) -> StopManagedContainerResult:
        self.stops += 1
        if self.stop_error:
            raise ClientTransportError("fixture lost stop response")
        self.running = False
        return StopManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id="a" * 64,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )

    def start_managed_container(self, **_kwargs: object) -> StartManagedContainerResult:
        self.starts += 1
        self.running = True
        if self.start_error:
            raise ClientTransportError("fixture lost start response")
        return StartManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id="a" * 64,
            runtime_state=ContainerRuntimeState.RUNNING,
            start_performed=True,
        )


class ImmediateReadiness:
    def wait_until_usable(self, *_args: object, **_kwargs: object) -> ReadinessResult:
        return ReadinessResult(
            container_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            jsonrpc_state=JsonRpcState.RESPONSIVE,
            usability_state=UsabilityState.USABLE,
            failure_reason=None,
            last_observed_failure=None,
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            elapsed_seconds=0.0,
            attempts=1,
        )


class Environment:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions, self.clock, self.broker = sessions, history.Clock(), Broker()
        self.active: bool | None = False
        self.playback_queries = 0
        profile = values()
        profile["runtime_revision_id"] = history.REVISION_ID
        with sessions.begin() as session:
            RuntimeExecutionProfileRepository(session).create(
                RuntimeExecutionProfile(**profile)
            )
        self.readiness = ReadinessTaskRegistry(max_workers=1)
        status = ManagedRuntimeStatusService(sessions, self.broker, self.readiness)
        self.status = status
        coordinator = ManagedInstanceLifecycleOperationCoordinator(
            sessions, clock=self.clock
        )
        self.lifecycle = ManagedRuntimeLifecycleService(
            sessions,
            cast(Any, self.broker),
            self.readiness,
            status,
            lifecycle_operations=coordinator,
            coordinator_factory=lambda _broker, _profile: ImmediateReadiness(),
        )
        self.resources = ManagedResourceService(
            sessions,
            self.broker,
            self.lifecycle,
            playback_factory=lambda _observation, _profile: self,
            clock=self.clock,
        )

    def playback_active(self) -> bool:
        self.playback_queries += 1
        if self.active is None:
            raise RuntimeError("fixture player evidence unavailable")
        return self.active


@pytest.fixture
def env(factory: sessionmaker[Session]) -> Iterator[Environment]:
    environment = Environment(factory)
    yield environment
    environment.readiness.close()


def test_one_hour_deadline_stops_same_instance_using_real_lifecycle(
    env: Environment,
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    initial = env.resources.status(INSTANCE_ID)
    for _ in range(3):
        assert env.resources.capacity().used == 1
        assert env.resources.status(INSTANCE_ID) == initial
    env.clock.value += timedelta(minutes=59)
    assert env.resources.sweep_idle() == 0
    assert env.broker.observations == 0
    env.clock.value += timedelta(minutes=1)
    assert env.resources.sweep_idle() == 1
    assert env.broker.stops == 1 and not env.broker.running
    stopped = env.resources.status(INSTANCE_ID)
    assert stopped["last_stop_reason"] == "idle"
    assert stopped["last_activity_at"] == initial["last_activity_at"]
    with env.sessions() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None


def test_existing_instance_is_not_enrolled_and_passive_polling_does_not_wake(
    env: Environment,
) -> None:
    env.clock.value += timedelta(days=2)
    assert not env.resources.status(INSTANCE_ID)["enrolled"]
    assert env.resources.sweep_idle() == 0
    assert env.broker.observations == 0 and env.broker.stops == 0


@pytest.mark.parametrize("active", [True, None])
def test_playback_and_unavailable_evidence_protect_without_stop_attempt(
    env: Environment, active: bool | None
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.clock.value += timedelta(hours=1)
    env.active = active
    assert env.resources.sweep_idle() == 0
    assert env.broker.stops == 0
    with env.sessions() as session:
        row = session.get(ManagedIdleStateRow, str(INSTANCE_ID))
        assert row is not None and row.last_stop_attempt_at is None
    env.active = False
    assert env.resources.sweep_idle() == 1


def test_disabled_idle_policy_and_bounded_keep_awake(env: Environment) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.resources.configure(ResourcePolicyInput(max_running=2, idle_minutes=None))
    env.clock.value += timedelta(hours=3)
    assert env.resources.sweep_idle() == 0
    env.resources.configure(ResourcePolicyInput(max_running=2, idle_minutes=1))
    env.resources.keep_awake(INSTANCE_ID, minutes=120)
    env.clock.value += timedelta(minutes=119)
    assert env.resources.sweep_idle() == 0
    env.clock.value += timedelta(minutes=1)
    assert env.resources.sweep_idle() == 1


def test_other_client_activity_and_hold_protect_release(env: Environment) -> None:
    owner, other = f"mcp:{uuid4()}", f"mcp:{uuid4()}"
    with env.sessions.begin() as session:
        record_activity(session, INSTANCE_ID, actor=other, now=env.clock())
    with pytest.raises(ManagedResourceProtected, match="other_client_active"):
        env.resources.stop_if_eligible(INSTANCE_ID, reason="released", actor=owner)
    env.clock.value += timedelta(minutes=61)
    with env.sessions.begin() as session:
        use = session.get(ManagedInstanceUseRow, (str(INSTANCE_ID), other))
        assert use is not None
        use.hold_until = timestamp(env.clock() + timedelta(minutes=1))
    with pytest.raises(ManagedResourceProtected, match="keep_awake"):
        env.resources.stop_if_eligible(INSTANCE_ID, reason="released", actor=owner)
    env.clock.value += timedelta(minutes=1)
    assert (
        env.resources.stop_if_eligible(INSTANCE_ID, reason="released", actor=owner)
        == "stopped"
    )
    assert env.resources.status(INSTANCE_ID)["last_stop_reason"] == "released"


def test_unconfirmed_stop_keeps_fence_and_does_not_retry_on_next_tick(
    env: Environment,
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.clock.value += timedelta(minutes=60)
    env.broker.stop_error = True
    assert env.resources.sweep_idle() == 0
    assert env.broker.stops == 1
    for _ in range(3):
        env.clock.value += timedelta(minutes=1)
        assert env.resources.sweep_idle() == 0
    assert env.broker.stops == 1
    assert env.resources.status(INSTANCE_ID)["stop_requires_review"]


def test_non_managed_identity_is_rejected_before_broker_use(env: Environment) -> None:
    with pytest.raises(ManagedResourceProtected, match="managed_target_required"):
        env.resources.enroll(uuid4(), enabled=True)
    assert env.broker.observations == 0 and env.broker.stops == 0


def test_repeated_enrollment_does_not_extend_deadline_or_clear_failed_attempt(
    env: Environment,
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    original = env.resources.status(INSTANCE_ID)
    env.clock.value += timedelta(minutes=60)
    env.resources.enroll(INSTANCE_ID, enabled=True)
    assert env.resources.status(INSTANCE_ID) == original
    env.broker.stop_error = True
    assert env.resources.sweep_idle() == 0
    env.resources.enroll(INSTANCE_ID, enabled=True)
    assert env.resources.status(INSTANCE_ID)["stop_requires_review"]
    assert env.resources.sweep_idle() == 0 and env.broker.stops == 1


def test_web_hold_cannot_claim_a_stop_already_in_progress(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.clock.value += timedelta(minutes=60)
    original = env.playback_active

    def probe() -> bool:
        if env.playback_queries == 1:
            with pytest.raises(ManagedResourceProtected, match="stop_already_claimed"):
                env.resources.keep_awake(INSTANCE_ID, minutes=15)
        return original()

    monkeypatch.setattr(env, "playback_active", probe)
    assert env.resources.sweep_idle() == 1
    assert env.broker.stops == 1


def test_shutdown_during_native_observation_prevents_stop(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.clock.value += timedelta(minutes=60)
    cancelled = False
    original = env.playback_active

    def probe() -> bool:
        nonlocal cancelled
        cancelled = True
        return original()

    monkeypatch.setattr(env, "playback_active", probe)
    assert env.resources.sweep_idle(cancelled=lambda: cancelled) == 0
    assert env.broker.stops == 0
    assert not env.resources.status(INSTANCE_ID)["stop_requires_review"]


def test_idle_worker_is_single_cancellable_and_restart_does_not_replay_unknown_stop(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from kodi_lab.application.managed_idle_worker import ManagedIdleWorker

    env.resources.enroll(INSTANCE_ID, enabled=True)
    env.clock.value += timedelta(minutes=60)
    env.broker.stop_error = True
    observed = Event()
    original = env.resources.sweep_idle

    def sweep(*, cancelled: Any = None) -> int:
        result = original(cancelled=cancelled)
        observed.set()
        return result

    monkeypatch.setattr(env.resources, "sweep_idle", sweep)
    worker = ManagedIdleWorker(env.resources, poll_seconds=0.01)
    worker.start()
    worker.start()
    try:
        assert observed.wait(3)
    finally:
        worker.close()
    assert env.broker.stops == 1
    observed.clear()
    replacement = ManagedIdleWorker(env.resources, poll_seconds=0.01)
    replacement.start()
    try:
        assert observed.wait(3)
    finally:
        replacement.close()
    assert env.broker.stops == 1

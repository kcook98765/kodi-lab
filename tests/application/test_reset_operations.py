from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivityState,
)
from kodi_lab.application.reset_operations import (
    LEASE_DURATION,
    DurableResetActivityObserver,
    ResetExecutor,
    ResetLeaseLostError,
    ResetOperationCoordinator,
)
from kodi_lab.domain import ResetOperation, ResetOperationPhase, ResetOperationState
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import ResetOperationRepository

INSTANCE_ID = UUID("10000000-0000-4000-8000-000000000001")
SECOND_ID = UUID("20000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("30000000-0000-4000-8000-000000000001")
DEFINITION_ID = UUID("40000000-0000-4000-8000-000000000001")


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'operations.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "a" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "b" * 64,
                built_at="2026-09-15T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-15T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        for instance_id in (INSTANCE_ID, SECOND_ID):
            session.add(
                TargetRow(
                    id=str(instance_id),
                    name=str(instance_id),
                    target_type="managed",
                    kodi_version="21.3",
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.add(
                ManagedInstanceRow(
                    target_id=str(instance_id),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                    notes=None,
                )
            )
    yield result
    engine.dispose()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 15, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_one_active_operation_per_instance_is_database_enforced(
    factory: sessionmaker[Session],
) -> None:
    coordinator = ResetOperationCoordinator(factory)
    first = coordinator.queue(INSTANCE_ID)
    duplicate = coordinator.queue(INSTANCE_ID)
    other = coordinator.queue(SECOND_ID)
    assert first.created
    assert not duplicate.created
    assert duplicate.operation.operation_id == first.operation.operation_id
    assert other.created

    with pytest.raises(IntegrityError), factory.begin() as session:
        ResetOperationRepository(session).create(
            ResetOperation(uuid4(), INSTANCE_ID, datetime.now(UTC))
        )


def test_competing_sqlite_acquisitions_have_one_lease_owner(
    factory: sessionmaker[Session],
) -> None:
    operation_id = (
        ResetOperationCoordinator(factory).queue(INSTANCE_ID).operation.operation_id
    )
    barrier = threading.Barrier(3)
    results: list[bool] = []

    def acquire() -> None:
        coordinator = ResetOperationCoordinator(factory)
        barrier.wait()
        results.append(coordinator.acquire(operation_id) is not None)

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_competing_sqlite_queue_requests_create_one_operation(
    factory: sessionmaker[Session],
) -> None:
    barrier = threading.Barrier(3)
    operation_ids: list[UUID] = []

    def queue() -> None:
        barrier.wait()
        operation_ids.append(
            ResetOperationCoordinator(factory).queue(INSTANCE_ID).operation.operation_id
        )

    threads = [threading.Thread(target=queue) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(set(operation_ids)) == 1


def test_lease_ownership_heartbeat_expiry_and_terminal_slot(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = ResetOperationCoordinator(factory, clock=clock)
    operation_id = coordinator.queue(INSTANCE_ID).operation.operation_id
    lease = coordinator.acquire(operation_id)
    assert lease is not None
    running = coordinator.latest_for_instance(INSTANCE_ID)
    assert (
        running is not None and running.lease_expires_at == clock.now + LEASE_DURATION
    )
    with factory.begin() as session:
        assert not ResetOperationRepository(session).update_phase(
            operation_id,
            token_digest="0" * 64,
            phase=ResetOperationPhase.PROVISIONING,
            now=clock.now,
            lease_expires_at=clock.now + LEASE_DURATION,
        )
    clock.now += timedelta(seconds=30)
    coordinator.update_phase(lease, ResetOperationPhase.PROVISIONING)
    refreshed = coordinator.latest_for_instance(INSTANCE_ID)
    assert refreshed is not None
    assert refreshed.lease_expires_at == clock.now + LEASE_DURATION
    clock.now += LEASE_DURATION + timedelta(seconds=1)
    with pytest.raises(ResetLeaseLostError):
        coordinator.update_phase(lease, ResetOperationPhase.FINAL_VERIFYING)
    expired = coordinator.latest_for_instance(INSTANCE_ID)
    assert expired is not None
    stale = coordinator.terminalize_expired(expired)
    assert stale.state is ResetOperationState.RECONCILIATION_REQUIRED
    assert stale.phase is ResetOperationPhase.PROVISIONING
    assert stale.reconciliation_required


def test_success_frees_slot_and_pruning_retains_newest_twenty(
    factory: sessionmaker[Session],
) -> None:
    coordinator = ResetOperationCoordinator(factory)
    for _ in range(22):
        lease = coordinator.acquire(
            coordinator.queue(INSTANCE_ID).operation.operation_id
        )
        assert lease is not None
        coordinator.finish_success(lease)
    with factory() as session:
        retained = session.scalar(select(func.count()).select_from(ResetOperationRow))
    assert retained == 20


class FakeResetService:
    def __init__(
        self,
        observer: DurableResetActivityObserver,
        calls: list[UUID],
        failure: str | None = None,
    ) -> None:
        self.observer = observer
        self.calls = calls
        self.failure = failure

    def reset(self, instance_id: UUID) -> object:
        self.calls.append(instance_id)
        activity = self.observer.begin(instance_id)
        self.observer.transition(
            instance_id, activity.generation, ResetActivityState.PROVISIONING
        )
        if self.failure is not None:
            self.observer.fail(
                instance_id,
                activity.generation,
                failure_phase="provision",
                failure_reason="The exact pin could not be provisioned.",
                reconciliation_required=self.failure == "reconciliation",
            )
            return object()
        self.observer.succeed(instance_id, activity.generation)
        return object()


def test_executor_runs_once_and_persists_across_coordinator_restart(
    factory: sessionmaker[Session],
) -> None:
    coordinator = ResetOperationCoordinator(factory)
    operation = coordinator.queue(INSTANCE_ID).operation
    calls: list[UUID] = []
    executor_a = ResetExecutor(
        coordinator,
        ResetActivityRegistry(),
        lambda observer: FakeResetService(observer, calls),
        max_workers=1,
    )
    executor_b = ResetExecutor(
        ResetOperationCoordinator(factory),
        ResetActivityRegistry(),
        lambda observer: FakeResetService(observer, calls),
        max_workers=1,
    )
    executor_a.submit(operation.operation_id)
    executor_b.submit(operation.operation_id)
    for _ in range(100):
        latest = ResetOperationCoordinator(factory).latest_for_instance(INSTANCE_ID)
        if latest is not None and latest.state is ResetOperationState.SUCCEEDED:
            break
        time.sleep(0.01)
    executor_a.close()
    executor_b.close()
    assert calls == [INSTANCE_ID]
    assert latest is not None and latest.state is ResetOperationState.SUCCEEDED


def test_never_started_queue_is_acquired_after_restart(
    factory: sessionmaker[Session],
) -> None:
    ResetOperationCoordinator(factory).queue(INSTANCE_ID)
    calls: list[UUID] = []
    restarted = ResetExecutor(
        ResetOperationCoordinator(factory),
        ResetActivityRegistry(),
        lambda observer: FakeResetService(observer, calls),
        max_workers=1,
    )
    restarted.start()
    for _ in range(100):
        latest = ResetOperationCoordinator(factory).latest_for_instance(INSTANCE_ID)
        if latest is not None and latest.state is ResetOperationState.SUCCEEDED:
            break
        time.sleep(0.01)
    restarted.close()
    assert calls == [INSTANCE_ID]


@pytest.mark.parametrize(
    ("failure", "state"),
    [
        ("ordinary", ResetOperationState.FAILED),
        ("reconciliation", ResetOperationState.RECONCILIATION_REQUIRED),
    ],
)
def test_executor_persists_bounded_failure_outcomes(
    factory: sessionmaker[Session], failure: str, state: ResetOperationState
) -> None:
    coordinator = ResetOperationCoordinator(factory)
    operation = coordinator.queue(INSTANCE_ID).operation
    executor = ResetExecutor(
        coordinator,
        ResetActivityRegistry(),
        lambda observer: FakeResetService(observer, [], failure),
        max_workers=1,
    )
    executor.submit(operation.operation_id)
    for _ in range(100):
        latest = coordinator.latest_for_instance(INSTANCE_ID)
        if latest is not None and latest.state is state:
            break
        time.sleep(0.01)
    executor.close()
    assert latest is not None and latest.state is state
    assert latest.failure_reason == "The exact pin could not be provisioned."
    assert latest.phase is ResetOperationPhase.PROVISIONING

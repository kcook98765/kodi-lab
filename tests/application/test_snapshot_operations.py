from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mutations import (
    ManagedInstanceMutationConflictError,
    ManagedInstanceMutationCoordinator,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.application.reset_operations import (
    ResetOperationConflictError,
    ResetOperationCoordinator,
)
from kodi_lab.application.snapshot_operations import (
    SNAPSHOT_LEASE_DURATION,
    SnapshotCreateConflictError,
    SnapshotCreateLeaseLostError,
    SnapshotCreateOperationCoordinator,
    SnapshotCreateUnsupportedError,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotState,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    SnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import SnapshotRepository

INSTANCE_ID = UUID("91000000-0000-4000-8000-000000000001")
SECOND_ID = UUID("91000000-0000-4000-8000-000000000002")
REVISION_ID = UUID("91000000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("91000000-0000-4000-8000-000000000004")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 15, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot-operations.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21-3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=7,
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


def test_queue_persists_exact_pin_label_and_both_reservations(
    factory: sessionmaker[Session],
) -> None:
    queued = SnapshotCreateOperationCoordinator(factory).queue(
        INSTANCE_ID, label="  before test  "
    )
    assert queued.snapshot.runtime_revision_id == REVISION_ID
    assert queued.snapshot.label == "before test"
    assert queued.snapshot.state is SnapshotState.CREATING
    assert queued.snapshot.prior_runtime_state is None
    with factory() as session:
        mutation = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
        heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
        assert mutation is not None and mutation.operation_kind == "snapshot_create"
        assert heavy is not None and heavy.operation_kind == "snapshot_create"
        assert mutation.lease_token_digest is None
        assert heavy.lease_token_digest is None


@pytest.mark.parametrize(
    "label",
    ["x" * 121, "bad/path", "bad\\path", "bad\x00label", "bad\nlabel"],
)
def test_label_validation_rejects_unsafe_metadata(
    factory: sessionmaker[Session], label: str
) -> None:
    with pytest.raises(SnapshotCreateUnsupportedError):
        SnapshotCreateOperationCoordinator(factory).queue(INSTANCE_ID, label=label)


def test_global_heavy_reservation_blocks_other_instance(
    factory: sessionmaker[Session],
) -> None:
    SnapshotCreateOperationCoordinator(factory).queue(INSTANCE_ID, label=None)
    with pytest.raises(SnapshotCreateConflictError):
        SnapshotCreateOperationCoordinator(factory).queue(SECOND_ID, label=None)


def test_snapshot_reservation_blocks_reset_and_lifecycle(
    factory: sessionmaker[Session],
) -> None:
    SnapshotCreateOperationCoordinator(factory).queue(INSTANCE_ID, label=None)
    with pytest.raises(ResetOperationConflictError):
        ResetOperationCoordinator(factory).queue(INSTANCE_ID)
    with pytest.raises(ManagedInstanceMutationConflictError):
        ManagedInstanceMutationCoordinator(factory).acquire(
            INSTANCE_ID, ManagedInstanceMutationKind.LIFECYCLE
        )
    lease = ManagedInstanceMutationCoordinator(factory).acquire(
        SECOND_ID, ManagedInstanceMutationKind.LIFECYCLE
    )
    ManagedInstanceMutationCoordinator(factory).release(lease)


def test_competing_sqlite_snapshot_queues_have_one_winner(
    factory: sessionmaker[Session],
) -> None:
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def queue() -> None:
        barrier.wait()
        try:
            SnapshotCreateOperationCoordinator(factory).queue(INSTANCE_ID, label=None)
            outcomes.append("created")
        except SnapshotCreateConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=queue) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "created"]


def test_acquire_heartbeat_wrong_token_expiry_and_crash_visibility(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = SnapshotCreateOperationCoordinator(factory, clock=clock)
    operation_id = coordinator.queue(INSTANCE_ID, label=None).operation.operation_id
    lease = coordinator.acquire(operation_id)
    assert lease is not None
    operation = coordinator.get(operation_id)
    assert operation is not None
    assert operation.lease_expires_at == clock.value + SNAPSHOT_LEASE_DURATION
    wrong = type(lease)(
        lease.operation_id,
        lease.snapshot_id,
        lease.managed_instance_id,
        "wrong-token",
    )
    with pytest.raises(SnapshotCreateLeaseLostError):
        coordinator.update_phase(wrong, SnapshotCreateOperationPhase.CAPTURING)
    clock.value += timedelta(seconds=30)
    coordinator.update_phase(lease, SnapshotCreateOperationPhase.CAPTURING)
    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)
    stale = coordinator.get(operation_id)
    assert stale is not None
    assert stale.state is SnapshotCreateOperationState.RECONCILIATION_REQUIRED
    with factory() as session:
        snapshot = session.get(SnapshotRow, str(lease.snapshot_id))
        assert snapshot is not None and snapshot.state == SnapshotState.QUARANTINED
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_expired_running_snapshot_after_stop_marker_remains_blocking(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = SnapshotCreateOperationCoordinator(factory, clock=clock)
    queued = coordinator.queue(INSTANCE_ID, label=None)
    lease = coordinator.acquire(queued.operation.operation_id)
    assert lease is not None
    with factory.begin() as session:
        assert SnapshotRepository(session).set_prior_runtime_state(
            lease.snapshot_id, InstanceRuntimeState.RUNNING
        )
    coordinator.update_phase(lease, SnapshotCreateOperationPhase.STOPPING)
    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)
    stale = coordinator.get(lease.operation_id)
    assert stale is not None
    assert stale.state is SnapshotCreateOperationState.RECONCILIATION_REQUIRED
    assert stale.failure_reason == "broker_runtime_dispatch_unconfirmed"
    gate = ManagedInstanceReconciliationService(factory)
    for _ in range(2):
        status = gate.status(INSTANCE_ID)
        assert status.blocking
        assert ReconciliationAction.START in status.blocked_actions


def test_success_releases_both_leases_and_does_not_prune_snapshot(
    factory: sessionmaker[Session],
) -> None:
    coordinator = SnapshotCreateOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, label=None)
    lease = coordinator.acquire(queued.operation.operation_id)
    assert lease is not None
    with factory.begin() as session:
        snapshots = SnapshotRepository(session)
        snapshots.set_prior_runtime_state(
            queued.snapshot.id, InstanceRuntimeState.STOPPED
        )
        snapshots.mark_available(
            queued.snapshot.id,
            archive_digest="sha256:" + "a" * 64,
            manifest_digest="sha256:" + "b" * 64,
            archive_size=10240,
            member_count=1,
            validated_at=datetime.now(UTC),
        )
    coordinator.finish_success(lease)
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
        assert session.scalar(select(SnapshotRow.id)) == str(queued.snapshot.id)

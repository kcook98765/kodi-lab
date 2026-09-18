from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteOperationCoordinator,
)
from kodi_lab.application.mutations import ManagedInstanceMutationCoordinator
from kodi_lab.application.readiness_tasks import ReadinessTaskState
from kodi_lab.application.reconciliation import (
    CoordinationCorruptionError,
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    ReconciliationOperationKind,
    SnapshotReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.reset_operations import ResetOperationCoordinator
from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteOperationCoordinator,
)
from kodi_lab.application.snapshot_operations import (
    SNAPSHOT_LEASE_DURATION,
    SnapshotCreateOperationCoordinator,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreOperationCoordinator,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    ResetOperationState,
    Snapshot,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    SnapshotCreateOperationRow,
    SnapshotDeleteOperationRow,
    SnapshotRestoreOperationRow,
    SnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ResetOperationRepository,
    SnapshotCreateOperationRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRepository,
    SnapshotRestoreOperationRepository,
)

INSTANCE_ID = UUID("95000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("95000000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("95000000-0000-4000-8000-000000000003")
SNAPSHOT_ID = UUID("95000000-0000-4000-8000-000000000004")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}")
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
                built_at="2026-09-16T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="reconciliation-test",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
        SnapshotRepository(session).create(
            Snapshot(
                id=SNAPSHOT_ID,
                source_managed_instance_id=INSTANCE_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=datetime(2026, 9, 16, tzinfo=UTC),
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="known good",
                manifest_version=1,
                archive_digest="sha256:" + "c" * 64,
                manifest_digest="sha256:" + "d" * 64,
                archive_size=1024,
                member_count=1,
                state=SnapshotState.AVAILABLE,
                validation_at=datetime(2026, 9, 16, tzinfo=UTC),
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    yield result
    engine.dispose()


def test_different_heavy_path_cleans_stale_create_then_uses_semantic_gate(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    create = SnapshotCreateOperationCoordinator(factory, clock=clock)
    queued = create.queue(INSTANCE_ID, label="stale")
    lease = create.acquire(queued.operation.operation_id)
    assert lease is not None
    create.update_phase(lease, SnapshotCreateOperationPhase.CAPTURING)
    with factory.begin() as session:
        SnapshotRepository(session).set_prior_runtime_state(
            queued.snapshot.id, InstanceRuntimeState.STOPPED
        )

    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)
    restore = SnapshotRestoreOperationCoordinator(factory, clock=clock)
    restored = restore.queue(INSTANCE_ID, SNAPSHOT_ID)

    with factory() as session:
        stale = session.get(
            SnapshotCreateOperationRow, str(queued.operation.operation_id)
        )
        quarantined = SnapshotRepository(session).get(queued.snapshot.id)
        mutation = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
        heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
        assert stale is not None
        assert stale.state is SnapshotCreateOperationState.RECONCILIATION_REQUIRED
        assert stale.phase is SnapshotCreateOperationPhase.CAPTURING
        assert (
            quarantined is not None and quarantined.state is SnapshotState.QUARANTINED
        )
        assert mutation is not None and mutation.operation_id == str(
            restored.operation_id
        )
        assert heavy is not None and heavy.operation_id == str(restored.operation_id)


def test_startup_sweep_cleans_stale_dual_lease_without_resuming_work(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    create = SnapshotCreateOperationCoordinator(factory, clock=clock)
    queued = create.queue(INSTANCE_ID, label="startup stale")
    assert create.acquire(queued.operation.operation_id) is not None
    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)

    result = StaleOperationReconciliationDispatcher(
        factory, clock=clock
    ).sweep_startup()
    assert result.terminalized_operations == [queued.operation.operation_id]
    assert result.released_reservations == 2
    with factory() as session:
        operation = session.get(
            SnapshotCreateOperationRow, str(queued.operation.operation_id)
        )
        assert operation is not None
        assert operation.state is SnapshotCreateOperationState.RECONCILIATION_REQUIRED
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_expired_reset_lease_is_released_but_semantic_gate_still_blocks(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    reset = ResetOperationCoordinator(factory, clock=clock)
    queued = reset.queue(INSTANCE_ID)
    lease = reset.acquire(queued.operation.operation_id)
    assert lease is not None
    with factory.begin() as session:
        running = session.get(ResetOperationRow, str(lease.operation_id))
        assert running is not None
        running.failure_reason = "Prior bounded inspection evidence."
    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)

    coordinator = ManagedInstanceMutationCoordinator(factory, clock=clock)
    with pytest.raises(ManagedInstanceReconciliationRequiredError) as captured:
        coordinator.acquire(INSTANCE_ID, ManagedInstanceMutationKind.LIFECYCLE)
    assert captured.value.code == "managed_instance_reconciliation_required"
    with factory() as session:
        stale = session.get(ResetOperationRow, str(lease.operation_id))
        assert (
            stale is not None
            and stale.state is ResetOperationState.RECONCILIATION_REQUIRED
        )
        assert stale.failure_reason == "Prior bounded inspection evidence."
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None


@pytest.mark.parametrize(
    ("kind", "row_class"),
    [
        ("snapshot_restore", SnapshotRestoreOperationRow),
        ("snapshot_delete", SnapshotDeleteOperationRow),
        ("instance_delete", ManagedInstanceDeleteOperationRow),
    ],
)
def test_cross_kind_global_sweep_covers_every_destructive_owner(
    factory: sessionmaker[Session], kind: str, row_class: Any
) -> None:
    clock = Clock()
    if kind == "snapshot_restore":
        coordinator = SnapshotRestoreOperationCoordinator(factory, clock=clock)
        restore_status = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
        operation_id = restore_status.operation_id
        assert coordinator.acquire(operation_id) is not None
    elif kind == "snapshot_delete":
        delete = SnapshotDeleteOperationCoordinator(factory, clock=clock)
        delete_status = delete.queue(INSTANCE_ID, SNAPSHOT_ID)
        operation_id = delete_status.operation_id
        assert delete.acquire(operation_id) is not None
    else:
        instance_delete = ManagedInstanceDeleteOperationCoordinator(
            factory, clock=clock
        )
        instance_delete_status = instance_delete.queue(
            INSTANCE_ID, preserve_snapshots=False
        )
        operation_id = instance_delete_status.operation_id
        assert instance_delete.acquire(operation_id) is not None

    clock.value += SNAPSHOT_LEASE_DURATION + timedelta(seconds=1)
    result = StaleOperationReconciliationDispatcher(factory, clock=clock).sweep_global()
    assert result.terminalized_operations == [operation_id]
    with factory() as session:
        operation = session.get(row_class, str(operation_id))
        assert operation is not None
        assert operation.state.value == "reconciliation_required"
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
        if kind != "snapshot_delete":
            assert (
                session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
            )


def test_orphan_global_reservation_fails_closed_without_release(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    operation_id = uuid4()
    with factory.begin() as session:
        session.add(
            GlobalHeavyOperationLeaseRow(
                singleton_id=1,
                operation_kind=GlobalHeavyOperationKind.SNAPSHOT_CREATE,
                operation_id=str(operation_id),
                lease_token_digest="a" * 64,
                acquired_at=(clock.value - timedelta(minutes=20)).isoformat(),
                heartbeat_at=(clock.value - timedelta(minutes=20)).isoformat(),
                lease_expires_at=(clock.value - timedelta(minutes=10)).isoformat(),
            )
        )
    with pytest.raises(CoordinationCorruptionError, match="missing"):
        StaleOperationReconciliationDispatcher(factory, clock=clock).sweep_global()
    with factory() as session:
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is not None


def test_unsupported_global_kind_fails_closed_without_release(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        session.execute(
            text(
                "INSERT INTO global_heavy_operation_lease "
                "(singleton_id, operation_kind, operation_id, lease_token_digest, "
                "acquired_at, heartbeat_at, lease_expires_at) VALUES "
                "(1, 'unsupported', :operation_id, :digest, :acquired, "
                ":heartbeat, :expires)"
            ),
            {
                "operation_id": str(uuid4()),
                "digest": "a" * 64,
                "acquired": "2026-09-15T23:00:00.000000Z",
                "heartbeat": "2026-09-15T23:00:00.000000Z",
                "expires": "2026-09-15T23:10:00.000000Z",
            },
        )
    with pytest.raises(CoordinationCorruptionError, match="unsupported"):
        StaleOperationReconciliationDispatcher(factory).sweep_global()
    with factory() as session:
        kind = session.execute(
            text(
                "SELECT operation_kind FROM global_heavy_operation_lease "
                "WHERE singleton_id = 1"
            )
        ).scalar_one()
        assert kind == "unsupported"


def test_terminal_owner_and_instance_mismatch_are_not_blindly_released(
    factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    expired = "2026-09-15T23:10:00.000000Z"
    token = "b" * 64
    terminal_id = uuid4()
    with factory.begin() as session:
        session.add(
            SnapshotDeleteOperationRow(
                id=str(terminal_id),
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                prior_snapshot_state="available",
                requested_at="2026-09-15T23:00:00.000000Z",
                started_at="2026-09-15T23:00:00.000000Z",
                heartbeat_at=expired,
                completed_at=expired,
                state="failed",
                phase="deleting_artifact",
                failure_reason="bounded",
                reconciliation_required=False,
            )
        )
        session.add(
            GlobalHeavyOperationLeaseRow(
                singleton_id=1,
                operation_kind="snapshot_delete",
                operation_id=str(terminal_id),
                lease_token_digest=token,
                acquired_at="2026-09-15T23:00:00.000000Z",
                heartbeat_at=expired,
                lease_expires_at=expired,
            )
        )
    with pytest.raises(CoordinationCorruptionError, match="not running"):
        StaleOperationReconciliationDispatcher(
            factory, clock=lambda: now
        ).sweep_global()
    with factory() as session:
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is not None

    with factory.begin() as session:
        session.delete(session.get(GlobalHeavyOperationLeaseRow, 1))
        mismatch_id = uuid4()
        session.add(
            ResetOperationRow(
                id=str(mismatch_id),
                managed_instance_id=str(uuid4()),
                requested_at="2026-09-15T23:00:00.000000Z",
                started_at="2026-09-15T23:00:00.000000Z",
                heartbeat_at=expired,
                state="running",
                phase="stopping",
                reconciliation_required=False,
                lease_token_digest=token,
                lease_expires_at=expired,
            )
        )
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(INSTANCE_ID),
                operation_kind="reset",
                operation_id=str(mismatch_id),
                lease_token_digest=token,
                acquired_at="2026-09-15T23:00:00.000000Z",
                heartbeat_at=expired,
                lease_expires_at=expired,
            )
        )
    with pytest.raises(CoordinationCorruptionError, match="identity"):
        StaleOperationReconciliationDispatcher(
            factory, clock=lambda: now
        ).sweep_instance(INSTANCE_ID)
    with factory() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is not None
        )


def test_multiple_unresolved_records_fail_closed_with_bounded_projection(
    factory: sessionmaker[Session],
) -> None:
    completed = "2026-09-16T00:00:01.000000Z"
    with factory.begin() as session:
        session.add(
            ResetOperationRow(
                id=str(uuid4()),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:00.000000Z",
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at=completed,
                completed_at=completed,
                state="reconciliation_required",
                phase="stopping",
                failure_reason="unsafe/raw/path/that/must/not/be/exposed",
                reconciliation_required=True,
            )
        )
        session.add(
            SnapshotRestoreOperationRow(
                id=str(uuid4()),
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:00.000000Z",
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at=completed,
                completed_at=completed,
                state="reconciliation_required",
                phase="deprovisioning",
                failure_reason="container deadbeef path /secret",
                reconciliation_required=True,
            )
        )
    status = ManagedInstanceReconciliationService(factory).status(INSTANCE_ID)
    assert status.operation_kind is ReconciliationOperationKind.MULTIPLE
    assert status.blocking
    assert len(status.blocked_actions) == 17
    assert "secret" not in status.model_dump_json()


def test_snapshot_delete_reconciliation_is_snapshot_scoped(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        snapshot = session.get(SnapshotDeleteOperationRow, "missing")
        assert snapshot is None
        session.add(
            SnapshotDeleteOperationRow(
                id=str(uuid4()),
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                prior_snapshot_state="available",
                requested_at="2026-09-16T00:00:00.000000Z",
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at="2026-09-16T00:00:01.000000Z",
                completed_at="2026-09-16T00:00:01.000000Z",
                state="reconciliation_required",
                phase="deleting_artifact",
                failure_reason="bounded",
                reconciliation_required=True,
            )
        )
    instance = ManagedInstanceReconciliationService(factory).status(INSTANCE_ID)
    snapshot_status = SnapshotReconciliationService(factory).status(SNAPSHOT_ID)
    assert not instance.required
    assert snapshot_status.blocking
    assert ReconciliationAction.SNAPSHOT_RESTORE in snapshot_status.blocked_actions
    assert ReconciliationAction.PROVISION not in snapshot_status.blocked_actions


def test_expired_source_attachment_lease_is_released_as_short_metadata_work(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = ManagedInstanceMutationCoordinator(factory, clock=clock)
    coordinator.acquire(
        INSTANCE_ID, ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT
    )
    clock.value += timedelta(minutes=10, seconds=1)

    result = StaleOperationReconciliationDispatcher(
        factory, clock=clock
    ).sweep_instance(INSTANCE_ID)

    assert result.released_reservations == 1
    assert result.terminalized_operations == []
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None


def test_running_create_recovery_requires_process_and_readiness_proof(
    factory: sessionmaker[Session],
) -> None:
    operation_id = uuid4()
    with factory.begin() as session:
        snapshot = session.get(SnapshotRow, str(SNAPSHOT_ID))
        assert snapshot is not None
        snapshot.state = SnapshotState.QUARANTINED
        snapshot.prior_runtime_state = InstanceRuntimeState.RUNNING
        snapshot.validation_result = SnapshotValidationResult.FAILED
        session.add(
            SnapshotCreateOperationRow(
                id=str(operation_id),
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:00.000000Z",
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at="2026-09-16T00:00:01.000000Z",
                completed_at="2026-09-16T00:00:01.000000Z",
                state="reconciliation_required",
                phase="verifying_readiness",
                failure_reason="bounded",
                reconciliation_required=True,
            )
        )

    container_id = "a" * 64

    class Broker:
        def managed_container_inventory(self) -> Any:
            return SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        managed_instance_id=str(INSTANCE_ID),
                        container_id=container_id,
                        runtime_state=ContainerRuntimeState.RUNNING,
                    )
                ]
            )

        def observe_managed_kodi_runtime(self, **_kwargs: object) -> Any:
            return SimpleNamespace(
                runtime_state=ContainerRuntimeState.RUNNING,
                process_state=KodiProcessState.RUNNING,
            )

    class Readiness:
        def snapshot(self, _instance_id: UUID) -> Any:
            return SimpleNamespace(state=ReadinessTaskState.USABLE)

    without_readiness = ManagedInstanceReconciliationService(
        factory, broker=Broker()
    ).status(INSTANCE_ID)
    proven = ManagedInstanceReconciliationService(
        factory,
        broker=Broker(),
        readiness=Readiness(),  # type: ignore[arg-type]
    ).status(INSTANCE_ID)
    assert without_readiness.blocking
    assert not proven.blocking
    assert proven.observed_state == "source_running_snapshot_quarantined"


def test_pruning_retains_active_and_reconciliation_rows_for_every_bounded_type(
    factory: sessionmaker[Session],
) -> None:
    requested = "2026-09-16T00:00:00.000000Z"
    completed = "2026-09-16T00:00:01.000000Z"
    with factory.begin() as session:
        for index in range(23):
            suffix = f"{index:012d}"
            session.add(
                ResetOperationRow(
                    id=f"96000000-0000-4000-8000-{suffix}",
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=f"2026-09-16T00:00:{index:02d}.000000Z",
                    started_at=requested,
                    heartbeat_at=completed,
                    completed_at=completed,
                    state="succeeded",
                    phase="succeeded",
                    reconciliation_required=False,
                )
            )
            session.add(
                SnapshotCreateOperationRow(
                    id=f"96100000-0000-4000-8000-{suffix}",
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=f"2026-09-16T00:00:{index:02d}.000000Z",
                    started_at=requested,
                    heartbeat_at=completed,
                    completed_at=completed,
                    state="failed",
                    phase="failed",
                    failure_reason="bounded",
                    reconciliation_required=False,
                )
            )
            session.add(
                SnapshotRestoreOperationRow(
                    id=f"96200000-0000-4000-8000-{suffix}",
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=f"2026-09-16T00:00:{index:02d}.000000Z",
                    started_at=requested,
                    heartbeat_at=completed,
                    completed_at=completed,
                    state="succeeded",
                    phase="succeeded",
                    reconciliation_required=False,
                )
            )
            session.add(
                SnapshotDeleteOperationRow(
                    id=f"96300000-0000-4000-8000-{suffix}",
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    prior_snapshot_state="available",
                    requested_at=f"2026-09-16T00:00:{index:02d}.000000Z",
                    started_at=requested,
                    heartbeat_at=completed,
                    completed_at=completed,
                    state="failed",
                    phase="failed",
                    failure_reason="bounded",
                    reconciliation_required=False,
                )
            )
        reconciliation_ids = []
        for row_class, prefix, extra in (
            (ResetOperationRow, "970", {}),
            (SnapshotCreateOperationRow, "971", {"snapshot_id": str(SNAPSHOT_ID)}),
            (SnapshotRestoreOperationRow, "972", {"snapshot_id": str(SNAPSHOT_ID)}),
            (
                SnapshotDeleteOperationRow,
                "973",
                {
                    "snapshot_id": str(SNAPSHOT_ID),
                    "prior_snapshot_state": "available",
                },
            ),
        ):
            row_id = f"{prefix}00000-0000-4000-8000-000000000001"
            reconciliation_ids.append((row_class, row_id))
            session.add(
                row_class(
                    id=row_id,
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=requested,
                    started_at=requested,
                    heartbeat_at=completed,
                    completed_at=completed,
                    state="reconciliation_required",
                    phase="reconciliation_required",
                    failure_reason="bounded",
                    reconciliation_required=True,
                    **extra,
                )
            )
        active_rows = (
            ResetOperationRow(
                id="97400000-0000-4000-8000-000000000001",
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:23.000000Z",
                state="queued",
                phase="queued",
                reconciliation_required=False,
            ),
            SnapshotCreateOperationRow(
                id="97500000-0000-4000-8000-000000000001",
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:23.000000Z",
                state="queued",
                phase="queued",
                reconciliation_required=False,
            ),
            SnapshotRestoreOperationRow(
                id="97600000-0000-4000-8000-000000000001",
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:23.000000Z",
                state="queued",
                phase="queued",
                reconciliation_required=False,
            ),
            SnapshotDeleteOperationRow(
                id="97700000-0000-4000-8000-000000000001",
                snapshot_id=str(SNAPSHOT_ID),
                managed_instance_id=str(INSTANCE_ID),
                prior_snapshot_state="available",
                requested_at="2026-09-16T00:00:23.000000Z",
                state="queued",
                phase="queued",
                reconciliation_required=False,
            ),
        )
        session.add_all(active_rows)
        delete_history_id = "97800000-0000-4000-8000-000000000001"
        session.add(
            ManagedInstanceDeleteOperationRow(
                id=delete_history_id,
                original_managed_instance_id=str(INSTANCE_ID),
                source_instance_name="reconciliation-test",
                runtime_revision_id=str(REVISION_ID),
                fixture_definition_id=None,
                preserve_snapshots=True,
                snapshot_count=1,
                aggregate_snapshot_bytes=1024,
                completed_snapshot_count=1,
                requested_at=requested,
                started_at=requested,
                heartbeat_at=completed,
                completed_at=completed,
                state="reconciliation_required",
                phase="preserving_snapshots",
                failure_reason="bounded",
                reconciliation_required=True,
            )
        )
        assert ResetOperationRepository(session).prune_terminal(INSTANCE_ID) == 3
        assert (
            SnapshotCreateOperationRepository(session).prune_terminal(INSTANCE_ID) == 3
        )
        assert (
            SnapshotRestoreOperationRepository(session).prune_terminal(INSTANCE_ID) == 3
        )
        assert (
            SnapshotDeleteOperationRepository(session).prune_terminal(INSTANCE_ID) == 3
        )

    with factory() as session:
        for row_class, row_id in reconciliation_ids:
            assert session.get(row_class, row_id) is not None
        for row_class in (
            ResetOperationRow,
            SnapshotCreateOperationRow,
            SnapshotRestoreOperationRow,
            SnapshotDeleteOperationRow,
        ):
            count = session.scalar(
                select(func.count())
                .select_from(row_class)
                .where(row_class.state.in_(["succeeded", "failed"]))
            )
            assert count == 20
        for row in active_rows:
            assert session.get(type(row), row.id) is not None
        assert (
            session.get(ManagedInstanceDeleteOperationRow, delete_history_id)
            is not None
        )

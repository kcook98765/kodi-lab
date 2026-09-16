from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_delete_operations import (
    SNAPSHOT_DELETE_LEASE_DURATION,
    SnapshotDeleteConflictError,
    SnapshotDeleteOperationCoordinator,
)
from kodi_lab.application.snapshot_deletions import (
    DisposableSnapshotDeletionService,
    SnapshotDeletionError,
)
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateConflictError,
    SnapshotCreateOperationCoordinator,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreConflictError,
    SnapshotRestoreOperationCoordinator,
    SnapshotRestoreUnsupportedError,
)
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    Snapshot,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotDeleteReconciliationState,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.snapshot.storage import delete_snapshot_artifact, snapshot_directory
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    SnapshotDeleteOperationRow,
    SnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    SnapshotRepository,
)

INSTANCE_ID = UUID("94300000-0000-4000-8000-000000000001")
SECOND_ID = UUID("94300000-0000-4000-8000-000000000002")
REVISION_ID = UUID("94300000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("94300000-0000-4000-8000-000000000004")
SNAPSHOT_ID = UUID("94300000-0000-4000-8000-000000000005")
SECOND_SNAPSHOT_ID = UUID("94300000-0000-4000-8000-000000000006")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'delete-operations.db'}")
    result = create_session_factory(engine)
    Base.metadata.create_all(engine)
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="delete", kodi_version="21.3"
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
                built_at="2026-09-16T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        for identity in (INSTANCE_ID, SECOND_ID):
            session.add(
                TargetRow(
                    id=str(identity),
                    name=str(identity),
                    target_type="managed",
                    kodi_version="21.3",
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.add(
                ManagedInstanceRow(
                    target_id=str(identity),
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
                captured_at=timestamp,
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="delete",
                manifest_version=1,
                archive_digest="sha256:" + "c" * 64,
                manifest_digest="sha256:" + "d" * 64,
                archive_size=10240,
                member_count=1,
                state=SnapshotState.AVAILABLE,
                validation_at=timestamp,
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
        SnapshotRepository(session).create(
            Snapshot(
                id=SECOND_SNAPSHOT_ID,
                source_managed_instance_id=SECOND_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=timestamp,
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="surviving",
                manifest_version=1,
                archive_digest="sha256:" + "e" * 64,
                manifest_digest="sha256:" + "f" * 64,
                archive_size=10240,
                member_count=1,
                state=SnapshotState.AVAILABLE,
                validation_at=timestamp,
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    yield result
    engine.dispose()


def test_queue_atomically_marks_deleting_and_reserves_only_global_heavy(
    factory: sessionmaker[Session],
) -> None:
    status = SnapshotDeleteOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)
    with factory() as session:
        snapshot = SnapshotRepository(session).get(SNAPSHOT_ID)
        heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
        mutation = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
    assert snapshot is not None and snapshot.state is SnapshotState.DELETING
    assert heavy is not None
    assert heavy.operation_kind is GlobalHeavyOperationKind.SNAPSHOT_DELETE
    assert mutation is None
    assert status.state is SnapshotDeleteOperationState.QUEUED


def test_duplicate_returns_existing_and_restore_rejects_deleting(
    factory: sessionmaker[Session],
) -> None:
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    first = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    assert (
        coordinator.queue(INSTANCE_ID, SNAPSHOT_ID).operation_id == first.operation_id
    )
    with pytest.raises(SnapshotRestoreUnsupportedError, match="available"):
        SnapshotRestoreOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)


def test_delete_blocks_other_global_heavy_but_not_instance_mutation(
    factory: sessionmaker[Session],
) -> None:
    SnapshotDeleteOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)
    with pytest.raises(SnapshotCreateConflictError):
        SnapshotCreateOperationCoordinator(factory).queue(SECOND_ID, label=None)
    with pytest.raises(SnapshotRestoreConflictError):
        SnapshotRestoreOperationCoordinator(factory).queue(
            SECOND_ID, SECOND_SNAPSHOT_ID
        )
    with factory.begin() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE_ID,
            kind=ManagedInstanceMutationKind.LIFECYCLE,
            operation_id=UUID("94300000-0000-4000-8000-000000000007"),
        )


def test_success_removes_snapshot_but_retains_operation_history(
    factory: sessionmaker[Session],
) -> None:
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    coordinator.update_phase(lease, SnapshotDeleteOperationPhase.DELETING_METADATA)
    coordinator.finish_success(lease)
    with factory() as session:
        assert SnapshotRepository(session).get(SNAPSHOT_ID) is None
        row = session.get(SnapshotDeleteOperationRow, str(queued.operation_id))
        assert row is not None and row.snapshot_id == str(SNAPSHOT_ID)
        assert row.state == SnapshotDeleteOperationState.SUCCEEDED
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_failure_and_stale_running_remain_deleting_and_reconcilable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    clock = Clock()
    coordinator = SnapshotDeleteOperationCoordinator(factory, clock=clock)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    clock.value += SNAPSHOT_DELETE_LEASE_DURATION + timedelta(seconds=1)
    running = coordinator.get(queued.operation_id)
    assert running is not None
    coordinator.terminalize_expired(running)
    operation = coordinator.get(queued.operation_id)
    assert operation is not None
    assert operation.state is SnapshotDeleteOperationState.RECONCILIATION_REQUIRED
    with factory() as session:
        snapshot = SnapshotRepository(session).get(SNAPSHOT_ID)
    assert snapshot is not None and snapshot.state is SnapshotState.DELETING
    assert (
        coordinator.inspect_reconciliation(operation, data_root=tmp_path)
        is SnapshotDeleteReconciliationState.SNAPSHOT_PRESENT_ARTIFACT_ABSENT
    )

    directory = snapshot_directory(tmp_path, SNAPSHOT_ID)
    directory.mkdir(parents=True)
    assert (
        coordinator.inspect_reconciliation(operation, data_root=tmp_path)
        is SnapshotDeleteReconciliationState.SNAPSHOT_AND_ARTIFACT_PRESENT
    )


def test_concurrent_delete_submissions_create_one_operation(
    factory: sessionmaker[Session],
) -> None:
    barrier = threading.Barrier(3)
    operation_ids: list[UUID] = []
    conflicts: list[bool] = []

    def submit() -> None:
        barrier.wait()
        try:
            operation_ids.append(
                SnapshotDeleteOperationCoordinator(factory)
                .queue(INSTANCE_ID, SNAPSHOT_ID)
                .operation_id
            )
        except SnapshotDeleteConflictError:
            conflicts.append(True)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(operation_ids) + len(conflicts) == 2
    assert len(set(operation_ids)) <= 1
    with factory() as session:
        assert len(list(session.scalars(select(SnapshotDeleteOperationRow)))) == 1


@dataclass
class Deleted:
    snapshot_id: str
    artifact_absent: bool = True


class DeleteBroker:
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.calls: list[str] = []

    def delete_managed_snapshot_artifact(self, *, snapshot_id: str) -> Deleted:
        self.calls.append(snapshot_id)
        if self.fail:
            raise RuntimeError("broker failure")
        delete_snapshot_artifact(
            data_root=self.root,
            snapshot_id=UUID(snapshot_id),
            owner_uid=(self.root / "snapshots").stat().st_uid,
            owner_gid=(self.root / "snapshots").stat().st_gid,
        )
        return Deleted(snapshot_id)


def prepare_artifact(tmp_path: Path) -> Path:
    root = tmp_path / "storage"
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True, mode=0o750)
    snapshots.chmod(0o750)
    artifact = snapshot_directory(root, SNAPSHOT_ID)
    artifact.mkdir(mode=0o750)
    (artifact / "manifest.json").write_bytes(b"secret")
    return root


def test_service_deletes_artifact_then_metadata_and_never_touches_instance(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    root = prepare_artifact(tmp_path)
    broker = DeleteBroker(root)
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    DisposableSnapshotDeletionService(
        factory, broker, coordinator, lease, data_root=root
    ).delete()
    with factory() as session:
        snapshot = SnapshotRepository(session).get(SNAPSHOT_ID)
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
    assert snapshot is not None and snapshot.state is SnapshotState.DELETING
    assert instance is not None and instance.runtime_state == "stopped"
    assert not snapshot_directory(root, SNAPSHOT_ID).exists()
    coordinator.finish_success(lease)
    with factory() as session:
        assert SnapshotRepository(session).get(SNAPSHOT_ID) is None
    assert broker.calls == [str(SNAPSHOT_ID)]


@pytest.mark.parametrize("terminal", [SnapshotState.FAILED, SnapshotState.QUARANTINED])
def test_failed_and_quarantined_without_artifacts_delete_cleanly(
    factory: sessionmaker[Session], tmp_path: Path, terminal: SnapshotState
) -> None:
    root = tmp_path / "storage"
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True, mode=0o750)
    snapshots.chmod(0o750)
    with factory.begin() as session:
        row = session.get(SnapshotRow, str(SNAPSHOT_ID))
        assert row is not None
        row.state = terminal
        row.validation_result = SnapshotValidationResult.FAILED
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    DisposableSnapshotDeletionService(
        factory, DeleteBroker(root), coordinator, lease, data_root=root
    ).delete()
    coordinator.finish_success(lease)
    with factory() as session:
        assert SnapshotRepository(session).get(SNAPSHOT_ID) is None


def test_broker_failure_keeps_deleting_snapshot_for_reconciliation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    root = prepare_artifact(tmp_path)
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(SnapshotDeletionError, match="could not be proven"):
        DisposableSnapshotDeletionService(
            factory, DeleteBroker(root, fail=True), coordinator, lease, data_root=root
        ).delete()
    coordinator.finish_failure(lease, reason="Delete state is uncertain.")
    operation = coordinator.get(queued.operation_id)
    with factory() as session:
        snapshot = SnapshotRepository(session).get(SNAPSHOT_ID)
    assert operation is not None and operation.reconciliation_required
    assert snapshot is not None and snapshot.state is SnapshotState.DELETING
    assert snapshot_directory(root, SNAPSHOT_ID).exists()


def test_crash_after_artifact_removal_exposes_absent_reconciliation_state(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    root = prepare_artifact(tmp_path)
    coordinator = SnapshotDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    DisposableSnapshotDeletionService(
        factory, DeleteBroker(root), coordinator, lease, data_root=root
    ).delete()
    coordinator.finish_failure(lease, reason="Process stopped before metadata commit.")
    operation = coordinator.get(queued.operation_id)
    assert operation is not None
    assert (
        coordinator.inspect_reconciliation(operation, data_root=root)
        is SnapshotDeleteReconciliationState.SNAPSHOT_PRESENT_ARTIFACT_ABSENT
    )

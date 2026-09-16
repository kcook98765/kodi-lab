from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mutations import (
    ManagedInstanceMutationConflictError,
    ManagedInstanceMutationCoordinator,
)
from kodi_lab.application.reset_operations import (
    ResetOperationConflictError,
    ResetOperationCoordinator,
)
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateConflictError,
    SnapshotCreateOperationCoordinator,
)
from kodi_lab.application.snapshot_restore_operations import (
    SNAPSHOT_RESTORE_LEASE_DURATION,
    SnapshotRestoreConflictError,
    SnapshotRestoreLeaseLostError,
    SnapshotRestoreOperationCoordinator,
    SnapshotRestoreUnsupportedError,
)
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    Snapshot,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotRestoreReconciliationState,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    FixtureDefinitionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import SnapshotRepository

INSTANCE_ID = UUID("93100000-0000-4000-8000-000000000001")
SECOND_ID = UUID("93100000-0000-4000-8000-000000000002")
REVISION_ID = UUID("93100000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("93100000-0000-4000-8000-000000000004")
SNAPSHOT_ID = UUID("93100000-0000-4000-8000-000000000005")
OTHER_REVISION_ID = UUID("93100000-0000-4000-8000-000000000006")
FIXTURE_ID = UUID("93100000-0000-4000-8000-000000000007")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 16, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'restore-operations.db'}")
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
            RuntimeRevisionRow(
                id=str(OTHER_REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=8,
                source_kind="test",
                source_reference="test-other",
                content_digest="sha256:" + "e" * 64,
                manifest_reference="test-other",
                manifest_digest="sha256:" + "f" * 64,
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
            FixtureDefinitionRow(
                id=str(FIXTURE_ID),
                slug="incompatible-fixture",
                fixture_version=1,
                size_category="small",
                content_category="mixed",
                content_digest="sha256:" + "1" * 64,
                compatible_kodi_version="21.3",
                description="Fixture mismatch proof.",
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
        SnapshotRepository(session).create(
            Snapshot(
                id=SNAPSHOT_ID,
                source_managed_instance_id=INSTANCE_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=datetime(2026, 9, 16, tzinfo=UTC),
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="drifted",
                manifest_version=1,
                archive_digest="sha256:" + "c" * 64,
                manifest_digest="sha256:" + "d" * 64,
                archive_size=10240,
                member_count=2,
                state=SnapshotState.AVAILABLE,
                validation_at=datetime(2026, 9, 16, tzinfo=UTC),
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    yield result
    engine.dispose()


def test_queue_reserves_restore_mutation_and_global_heavy(
    factory: sessionmaker[Session],
) -> None:
    status = SnapshotRestoreOperationCoordinator(factory).queue(
        INSTANCE_ID, SNAPSHOT_ID
    )
    assert status.snapshot_id == SNAPSHOT_ID
    with factory() as session:
        mutation = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
        heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
        assert mutation is not None and mutation.operation_kind == "snapshot_restore"
        assert heavy is not None and heavy.operation_kind == "snapshot_restore"


def test_duplicate_returns_existing_and_wrong_source_fails(
    factory: sessionmaker[Session],
) -> None:
    coordinator = SnapshotRestoreOperationCoordinator(factory)
    first = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    assert (
        coordinator.queue(INSTANCE_ID, SNAPSHOT_ID).operation_id == first.operation_id
    )
    with pytest.raises(SnapshotRestoreUnsupportedError):
        coordinator.queue(SECOND_ID, SNAPSHOT_ID)


def test_exact_revision_and_fixture_mismatches_fail_before_reservation(
    factory: sessionmaker[Session],
) -> None:
    coordinator = SnapshotRestoreOperationCoordinator(factory)
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.runtime_revision_id = str(OTHER_REVISION_ID)
    with pytest.raises(SnapshotRestoreUnsupportedError, match="RuntimeRevision"):
        coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)

    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.runtime_revision_id = str(REVISION_ID)
        instance.fixture_definition_id = str(FIXTURE_ID)
    with pytest.raises(SnapshotRestoreUnsupportedError, match="fixture assignment"):
        coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)

    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_restore_blocks_reset_create_lifecycle_and_second_global_heavy(
    factory: sessionmaker[Session],
) -> None:
    SnapshotRestoreOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)
    with pytest.raises(ResetOperationConflictError):
        ResetOperationCoordinator(factory).queue(INSTANCE_ID)
    with pytest.raises(SnapshotCreateConflictError):
        SnapshotCreateOperationCoordinator(factory).queue(SECOND_ID, label=None)
    with pytest.raises(ManagedInstanceMutationConflictError):
        ManagedInstanceMutationCoordinator(factory).acquire(
            INSTANCE_ID, ManagedInstanceMutationKind.LIFECYCLE
        )


def test_concurrent_restore_queue_has_one_operation(
    factory: sessionmaker[Session],
) -> None:
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def queue() -> None:
        barrier.wait()
        try:
            SnapshotRestoreOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)
            outcomes.append("accepted")
        except SnapshotRestoreConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=queue) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 2
    assert "accepted" in outcomes


def test_lease_prior_state_wrong_token_and_stale_crash_visibility(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = SnapshotRestoreOperationCoordinator(factory, clock=clock)
    operation_id = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID).operation_id
    lease = coordinator.acquire(operation_id)
    assert lease is not None
    coordinator.record_prior_state(lease, InstanceRuntimeState.RUNNING)
    old_container_id = "a" * 64
    coordinator.record_container_identity(lease, old_container_id=old_container_id)
    operation = coordinator.get(operation_id)
    assert operation is not None
    assert operation.prior_source_runtime_state is InstanceRuntimeState.RUNNING
    assert operation.lease_expires_at == clock.value + SNAPSHOT_RESTORE_LEASE_DURATION
    wrong = type(lease)(
        lease.operation_id, lease.snapshot_id, lease.managed_instance_id, "wrong"
    )
    with pytest.raises(SnapshotRestoreLeaseLostError):
        coordinator.update_phase(wrong, SnapshotRestoreOperationPhase.STOPPING)
    clock.value += SNAPSHOT_RESTORE_LEASE_DURATION + timedelta(seconds=1)
    stale = coordinator.get(operation_id)
    assert stale is not None
    assert stale.state is SnapshotRestoreOperationState.RECONCILIATION_REQUIRED
    assert stale.phase is SnapshotRestoreOperationPhase.ACQUIRING_LEASES

    class Inventory:
        def managed_container_inventory(self) -> ManagedContainerInventoryResult:
            return ManagedContainerInventoryResult(
                containers=[
                    ManagedContainerInventoryEntry(
                        managed_instance_id=str(INSTANCE_ID),
                        container_id=old_container_id,
                        image_id="sha256:" + "b" * 64,
                        runtime_state=ContainerRuntimeState.STOPPED,
                        health_state=ContainerHealthState.NOT_CONFIGURED,
                    )
                ]
            )

    assert (
        coordinator.inspect_reconciliation(stale, Inventory())
        is SnapshotRestoreReconciliationState.OLD_CONTAINER_PRESENT
    )
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None

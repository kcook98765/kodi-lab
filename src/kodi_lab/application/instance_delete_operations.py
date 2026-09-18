"""Durable disposable ManagedInstance deletion coordination."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    SnapshotReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstanceDeleteOperation,
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceDeletePriorState,
    ManagedInstanceMutationKind,
    SnapshotSourceBinding,
    SnapshotState,
)
from kodi_lab.snapshot.storage import snapshot_artifact_presence
from kodi_lab.storage.repositories import (
    AddonRepositoryAttachmentOperationRepository,
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    AddonZipSmokeTestOperationRepository,
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    ResetOperationRepository,
    SnapshotCreateOperationRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRepository,
    SnapshotRestoreOperationRepository,
    TargetRepository,
)

INSTANCE_DELETE_LEASE_DURATION = timedelta(minutes=10)
ELIGIBLE_SNAPSHOT_STATES = frozenset(
    {SnapshotState.AVAILABLE, SnapshotState.FAILED, SnapshotState.QUARANTINED}
)


class ManagedInstanceDeleteConflictError(RuntimeError):
    """Deletion conflicts with active or unreconciled typed work."""


class ManagedInstanceDeleteUnsupportedError(RuntimeError):
    """Current trusted state cannot safely support instance deletion."""


class ManagedInstanceDeleteNotFoundError(LookupError):
    """The requested disposable ManagedInstance does not exist."""


class ManagedInstanceDeleteLeaseLostError(RuntimeError):
    """The executor no longer owns its deletion leases."""


class ManagedInstanceDeleteReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_instance_exists: bool
    container_count: int
    snapshot_rows_remaining: int
    detached_snapshot_rows: int
    deleting_snapshot_rows: int
    artifact_present_count: int
    artifact_absent_count: int
    artifact_inconsistent_count: int


class ManagedInstanceDeleteStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    source_instance_name: str = Field(max_length=256)
    preserve_snapshots: bool
    snapshot_count: int
    aggregate_snapshot_bytes: int
    current_snapshot_id: UUID | None
    completed_snapshot_count: int
    prior_state: ManagedInstanceDeletePriorState | None
    state: ManagedInstanceDeleteOperationState
    phase: ManagedInstanceDeleteOperationPhase
    requested_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    reconciliation: ManagedInstanceDeleteReconciliation | None = None


class _InventoryBroker(Protocol):
    def managed_container_inventory(self) -> object: ...


@dataclass(frozen=True, slots=True)
class ManagedInstanceDeleteLease:
    operation_id: UUID
    managed_instance_id: UUID
    preserve_snapshots: bool
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class ManagedInstanceDeleteOperationCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_dispatcher: StaleOperationReconciliationDispatcher | None = None,
        reconciliation: ManagedInstanceReconciliationService | None = None,
        snapshot_reconciliation: SnapshotReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._stale = stale_dispatcher or StaleOperationReconciliationDispatcher(
            session_factory, clock=clock
        )
        self._reconciliation = reconciliation or ManagedInstanceReconciliationService(
            session_factory
        )
        self._snapshot_reconciliation = (
            snapshot_reconciliation or SnapshotReconciliationService(session_factory)
        )

    def queue(
        self, instance_id: UUID, *, preserve_snapshots: bool
    ) -> ManagedInstanceDeleteStatus:
        self._stale.sweep_global()
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.INSTANCE_DELETE
        )
        now = self._clock()
        try:
            with self._session_factory.begin() as session:
                instances = ManagedInstanceRepository(session)
                targets = TargetRepository(session)
                snapshots = SnapshotRepository(session)
                operations = ManagedInstanceDeleteOperationRepository(session)
                instance = instances.get(instance_id)
                target = targets.get(instance_id)
                if instance is None or target is None:
                    raise ManagedInstanceDeleteNotFoundError(
                        "Managed instance does not exist."
                    )
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise ManagedInstanceDeleteUnsupportedError(
                        "Persistent ManagedInstance deletion is not supported."
                    )
                if not 1 <= len(target.name) <= 256:
                    raise ManagedInstanceDeleteUnsupportedError(
                        "Managed instance display name is not safely bounded."
                    )
                if operations.reconciliation_for_instance(instance_id):
                    raise ManagedInstanceDeleteConflictError(
                        "Prior instance deletion requires operator reconciliation."
                    )
                current = snapshots.list_for_instance(instance_id)
                for snapshot in current:
                    self._snapshot_reconciliation.require_allowed(
                        snapshot.id, ReconciliationAction.INSTANCE_DELETE
                    )
                if any(item.state not in ELIGIBLE_SNAPSHOT_STATES for item in current):
                    raise ManagedInstanceDeleteConflictError(
                        "Active or transitional Snapshot state blocks "
                        "instance deletion."
                    )
                self._require_no_active_operations(session, instance_id)
                operation = ManagedInstanceDeleteOperation(
                    id=uuid4(),
                    original_managed_instance_id=instance_id,
                    source_instance_name=target.name,
                    runtime_revision_id=instance.runtime_revision_id,
                    fixture_definition_id=instance.fixture_definition_id,
                    preserve_snapshots=preserve_snapshots,
                    snapshot_count=len(current),
                    aggregate_snapshot_bytes=sum(
                        item.archive_size or 0 for item in current
                    ),
                    requested_at=now,
                )
                operations.create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.INSTANCE_DELETE,
                    operation_id=operation.id,
                )
                if not preserve_snapshots:
                    GlobalHeavyOperationLeaseRepository(session).reserve(
                        kind=GlobalHeavyOperationKind.INSTANCE_DELETE,
                        operation_id=operation.id,
                    )
        except IntegrityError as error:
            raise ManagedInstanceDeleteConflictError(
                "Another instance mutation or global heavy operation is active."
            ) from error
        return self.to_status(operation)

    @staticmethod
    def _require_no_active_operations(session: Session, instance_id: UUID) -> None:
        active = (
            ResetOperationRepository(session).active_for_instance(instance_id),
            SnapshotCreateOperationRepository(session).active_for_instance(instance_id),
            SnapshotRestoreOperationRepository(session).active_for_instance(
                instance_id
            ),
            SnapshotDeleteOperationRepository(session).active_for_instance(instance_id),
        )
        if any(item is not None for item in active):
            raise ManagedInstanceDeleteConflictError(
                "Active instance or Snapshot operation blocks instance deletion."
            )

    def acquire(self, operation_id: UUID) -> ManagedInstanceDeleteLease | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + INSTANCE_DELETE_LEASE_DURATION
        with self._session_factory.begin() as session:
            operations = ManagedInstanceDeleteOperationRepository(session)
            operation = operations.get(operation_id)
            if (
                operation is None
                or operation.state is not ManagedInstanceDeleteOperationState.QUEUED
            ):
                return None
            lease = ManagedInstanceDeleteLease(
                operation.id,
                operation.original_managed_instance_id,
                operation.preserve_snapshots,
                token,
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not mutation:
                return None
            if not lease.preserve_snapshots and not GlobalHeavyOperationLeaseRepository(
                session
            ).claim(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion global-heavy reservation was lost."
                )
            if not operations.acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                lease_expires_at=expires,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion reservation was lost."
                )
            return lease

    def update_phase(
        self,
        lease: ManagedInstanceDeleteLease,
        phase: ManagedInstanceDeleteOperationPhase,
    ) -> None:
        now = self._clock()
        expires = now + INSTANCE_DELETE_LEASE_DURATION
        with self._session_factory.begin() as session:
            operation_ok = ManagedInstanceDeleteOperationRepository(
                session
            ).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=expires,
            )
            mutation_ok = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy_ok = True
            if not lease.preserve_snapshots:
                heavy_ok = GlobalHeavyOperationLeaseRepository(session).renew(
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                    now=now,
                    expires_at=expires,
                )
            if not operation_ok or not mutation_ok or not heavy_ok:
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion leases are no longer owned."
                )

    def set_container_resolution(
        self,
        lease: ManagedInstanceDeleteLease,
        *,
        prior_state: ManagedInstanceDeletePriorState,
        container_id: str | None,
    ) -> None:
        with self._session_factory.begin() as session:
            if not ManagedInstanceDeleteOperationRepository(
                session
            ).set_container_resolution(
                lease.operation_id,
                token_digest=lease.token_digest,
                prior_state=prior_state,
                container_id=container_id,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion container identity was not owned."
                )

    def begin_snapshot(
        self, lease: ManagedInstanceDeleteLease, snapshot_id: UUID
    ) -> None:
        with self._session_factory.begin() as session:
            snapshot = SnapshotRepository(session).get(snapshot_id)
            operation = ManagedInstanceDeleteOperationRepository(session).get(
                lease.operation_id
            )
            if (
                snapshot is None
                or snapshot.source_managed_instance_id != lease.managed_instance_id
                or snapshot.state not in ELIGIBLE_SNAPSHOT_STATES
                or operation is None
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot deletion set changed before artifact deletion."
                )
            if not SnapshotRepository(session).mark_deleting(
                snapshot_id, prior_state=snapshot.state
            ) or not ManagedInstanceDeleteOperationRepository(session).update_progress(
                lease.operation_id,
                token_digest=lease.token_digest,
                current_snapshot_id=snapshot_id,
                completed_snapshot_count=operation.completed_snapshot_count,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot deletion progress was not owned."
                )

    def finish_snapshot(
        self, lease: ManagedInstanceDeleteLease, snapshot_id: UUID
    ) -> None:
        with self._session_factory.begin() as session:
            operation = ManagedInstanceDeleteOperationRepository(session).get(
                lease.operation_id
            )
            if operation is None or operation.current_snapshot_id != snapshot_id:
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot deletion progress identity changed."
                )
            snapshots = SnapshotRepository(session)
            if not snapshots.delete_deleting(snapshot_id):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot metadata deletion was not owned."
                )
            if not ManagedInstanceDeleteOperationRepository(session).update_progress(
                lease.operation_id,
                token_digest=lease.token_digest,
                current_snapshot_id=None,
                completed_snapshot_count=operation.completed_snapshot_count + 1,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot deletion progress could not be recorded."
                )

    def finish_success(self, lease: ManagedInstanceDeleteLease) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            instances = ManagedInstanceRepository(session)
            targets = TargetRepository(session)
            snapshots = SnapshotRepository(session)
            operations = ManagedInstanceDeleteOperationRepository(session)
            instance = instances.get(lease.managed_instance_id)
            target = targets.get(lease.managed_instance_id)
            operation = operations.get(lease.operation_id)
            if instance is None or target is None or operation is None:
                raise ManagedInstanceDeleteLeaseLostError(
                    "Original instance metadata is unavailable."
                )
            if (
                instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id != operation.runtime_revision_id
                or instance.fixture_definition_id != operation.fixture_definition_id
                or target.name != operation.source_instance_name
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Original instance identity changed during deletion."
                )
            current = snapshots.list_for_instance(lease.managed_instance_id)
            if any(item.state not in ELIGIBLE_SNAPSHOT_STATES for item in current):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Active Snapshot state appeared during instance deletion."
                )
            if lease.preserve_snapshots:
                if len(current) != operation.snapshot_count:
                    raise ManagedInstanceDeleteLeaseLostError(
                        "Authoritative Snapshot set changed during preservation."
                    )
                detached = snapshots.detach_for_deleted_source(
                    lease.managed_instance_id,
                    source_name=operation.source_instance_name,
                    deleted_at=now,
                )
                if detached != operation.snapshot_count:
                    raise ManagedInstanceDeleteLeaseLostError(
                        "Snapshot preservation was not complete."
                    )
                if not operations.update_progress(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    current_snapshot_id=None,
                    completed_snapshot_count=operation.snapshot_count,
                ):
                    raise ManagedInstanceDeleteLeaseLostError(
                        "Snapshot preservation progress was not owned."
                    )
            elif (
                current
                or operation.completed_snapshot_count != operation.snapshot_count
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Snapshot destruction is not complete."
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance mutation lease was not owned."
                )
            if not lease.preserve_snapshots and not GlobalHeavyOperationLeaseRepository(
                session
            ).release(operation_id=lease.operation_id, token_digest=lease.token_digest):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion heavy lease was not owned."
                )
            notes = instance.notes
            ManagedInstanceDevelopmentSourceRepository(session).delete_for_instance(
                lease.managed_instance_id
            )
            DevelopmentSourceAttachmentOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            AddonRepositoryAttachmentOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            DevelopmentSourceDeploymentRepository(session).delete_for_instance(
                lease.managed_instance_id
            )
            DevelopmentSourceDeploymentOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            DevelopmentSourceEnablementOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            DevelopmentSourceSmokeTestOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            AddonZipInstallationEvidenceRepository(session).delete_for_instance(
                lease.managed_instance_id
            )
            AddonZipInstallationOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            AddonZipEnablementOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            AddonZipSmokeTestOperationRepository(session).clear_live_instance(
                lease.managed_instance_id
            )
            if not instances.delete(lease.managed_instance_id):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Managed instance metadata could not be deleted."
                )
            if not targets.delete(lease.managed_instance_id):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Managed instance target metadata could not be deleted."
                )
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=ManagedInstanceDeleteOperationState.SUCCEEDED,
                phase=ManagedInstanceDeleteOperationPhase.SUCCEEDED,
                failure_reason=None,
                reconciliation_required=False,
                notes_at_deletion=notes,
                now=now,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion history was not terminalized."
                )

    def finish_failure(self, lease: ManagedInstanceDeleteLease, *, reason: str) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            operation = ManagedInstanceDeleteOperationRepository(session)
            if not operation.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=ManagedInstanceDeleteOperationState.RECONCILIATION_REQUIRED,
                phase=ManagedInstanceDeleteOperationPhase.RECONCILIATION_REQUIRED,
                failure_reason=reason[:256],
                reconciliation_required=True,
                notes_at_deletion=None,
                now=now,
            ):
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion failure state was not owned."
                )
            ManagedInstanceMutationLeaseRepository(session).release_for_operation(
                lease.managed_instance_id, lease.operation_id
            )
            if not lease.preserve_snapshots:
                GlobalHeavyOperationLeaseRepository(session).release_for_operation(
                    lease.operation_id
                )

    def _terminalize_expired(self) -> None:
        self._stale.sweep_startup()

    def get(self, operation_id: UUID) -> ManagedInstanceDeleteOperation | None:
        self._terminalize_expired()
        with self._session_factory() as session:
            return ManagedInstanceDeleteOperationRepository(session).get(operation_id)

    def queued(self) -> list[ManagedInstanceDeleteOperation]:
        self._terminalize_expired()
        with self._session_factory() as session:
            return ManagedInstanceDeleteOperationRepository(session).list_queued()

    def inspect_reconciliation(
        self,
        operation: ManagedInstanceDeleteOperation,
        *,
        broker: _InventoryBroker,
        data_root: Path,
    ) -> ManagedInstanceDeleteReconciliation:
        with self._session_factory() as session:
            instance_exists = (
                ManagedInstanceRepository(session).get(
                    operation.original_managed_instance_id
                )
                is not None
            )
            snapshots = SnapshotRepository(session).list_for_original_instance(
                operation.original_managed_instance_id
            )
        inventory = broker.managed_container_inventory()
        containers = [
            item
            for item in getattr(inventory, "containers", ())
            if item.managed_instance_id == str(operation.original_managed_instance_id)
        ]
        presence = [
            snapshot_artifact_presence(data_root, item.id) for item in snapshots
        ]
        return ManagedInstanceDeleteReconciliation(
            managed_instance_exists=instance_exists,
            container_count=len(containers),
            snapshot_rows_remaining=sum(
                item.source_binding is SnapshotSourceBinding.ATTACHED
                for item in snapshots
            ),
            detached_snapshot_rows=sum(
                item.source_binding is SnapshotSourceBinding.DELETED_SOURCE
                for item in snapshots
            ),
            deleting_snapshot_rows=sum(
                item.state is SnapshotState.DELETING for item in snapshots
            ),
            artifact_present_count=presence.count("directory"),
            artifact_absent_count=presence.count("absent"),
            artifact_inconsistent_count=len(presence)
            - presence.count("directory")
            - presence.count("absent"),
        )

    @staticmethod
    def to_status(
        operation: ManagedInstanceDeleteOperation,
    ) -> ManagedInstanceDeleteStatus:
        return ManagedInstanceDeleteStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            source_instance_name=operation.source_instance_name,
            preserve_snapshots=operation.preserve_snapshots,
            snapshot_count=operation.snapshot_count,
            aggregate_snapshot_bytes=operation.aggregate_snapshot_bytes,
            current_snapshot_id=operation.current_snapshot_id,
            completed_snapshot_count=operation.completed_snapshot_count,
            prior_state=operation.prior_state,
            state=operation.state,
            phase=operation.phase,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            heartbeat_at=operation.heartbeat_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
        )

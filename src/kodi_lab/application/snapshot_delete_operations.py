"""Durable explicit snapshot deletion with one global-heavy lease."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    SnapshotDeleteOperation,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotDeleteReconciliationState,
    SnapshotState,
)
from kodi_lab.snapshot.storage import snapshot_artifact_presence
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRepository,
)

SNAPSHOT_DELETE_LEASE_DURATION = timedelta(minutes=10)
TERMINAL_RETENTION_PER_INSTANCE = 20
ELIGIBLE_STATES = frozenset(
    {SnapshotState.AVAILABLE, SnapshotState.FAILED, SnapshotState.QUARANTINED}
)


class SnapshotDeleteConflictError(RuntimeError):
    """Deletion conflicts with active heavy work or an existing deletion."""


class SnapshotDeleteUnsupportedError(RuntimeError):
    """The requested Snapshot is not eligible for explicit deletion."""


class SnapshotDeleteNotFoundError(LookupError):
    """The routed source instance or Snapshot does not exist."""


class SnapshotDeleteLeaseLostError(RuntimeError):
    """The executor no longer owns its delete operation and heavy lease."""


class SnapshotDeleteStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    state: SnapshotDeleteOperationState
    phase: SnapshotDeleteOperationPhase
    requested_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    reconciliation_state: SnapshotDeleteReconciliationState | None = None


@dataclass(frozen=True, slots=True)
class SnapshotDeleteLease:
    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class SnapshotDeleteOperationCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def queue(self, instance_id: UUID, snapshot_id: UUID) -> SnapshotDeleteStatus:
        self._terminalize_stale_heavy_operation()
        now = self._clock()
        try:
            with self._session_factory.begin() as session:
                if ManagedInstanceRepository(session).get(instance_id) is None:
                    raise SnapshotDeleteNotFoundError(
                        "Managed instance does not exist."
                    )
                snapshots = SnapshotRepository(session)
                operations = SnapshotDeleteOperationRepository(session)
                snapshot = snapshots.get(snapshot_id)
                if snapshot is None:
                    raise SnapshotDeleteNotFoundError("Snapshot does not exist.")
                if snapshot.source_managed_instance_id != instance_id:
                    raise SnapshotDeleteUnsupportedError(
                        "Snapshot belongs to a different managed instance."
                    )
                if snapshot.state is SnapshotState.DELETING:
                    existing = operations.active_for_snapshot(snapshot_id)
                    if existing is not None:
                        return self.to_status(existing)
                    raise SnapshotDeleteUnsupportedError(
                        "Snapshot deletion requires operator reconciliation."
                    )
                if snapshot.state not in ELIGIBLE_STATES:
                    raise SnapshotDeleteUnsupportedError(
                        "Snapshot is not in a terminal deletable state."
                    )
                operation = SnapshotDeleteOperation(
                    id=uuid4(),
                    snapshot_id=snapshot.id,
                    managed_instance_id=instance_id,
                    prior_snapshot_state=snapshot.state,
                    requested_at=now,
                )
                if not snapshots.mark_deleting(snapshot_id, prior_state=snapshot.state):
                    raise SnapshotDeleteConflictError(
                        "Snapshot state changed before deletion could be reserved."
                    )
                operations.create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.SNAPSHOT_DELETE,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise SnapshotDeleteConflictError(
                "Another snapshot deletion or global heavy operation is active."
            ) from error
        return self.to_status(operation)

    def _terminalize_stale_heavy_operation(self) -> None:
        with self._session_factory() as session:
            heavy = GlobalHeavyOperationLeaseRepository(session).current()
            if (
                heavy is None
                or heavy.operation_kind is not GlobalHeavyOperationKind.SNAPSHOT_DELETE
                or heavy.lease_token_digest is None
                or heavy.lease_expires_at is None
                or datetime.fromisoformat(heavy.lease_expires_at.replace("Z", "+00:00"))
                > self._clock()
            ):
                return
            operation = SnapshotDeleteOperationRepository(session).get(
                UUID(heavy.operation_id)
            )
        if operation is not None:
            self.terminalize_expired(operation)

    def acquire(self, operation_id: UUID) -> SnapshotDeleteLease | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + SNAPSHOT_DELETE_LEASE_DURATION
        with self._session_factory.begin() as session:
            operations = SnapshotDeleteOperationRepository(session)
            operation = operations.get(operation_id)
            if operation is None:
                return None
            lease = SnapshotDeleteLease(
                operation.id,
                operation.snapshot_id,
                operation.managed_instance_id,
                token,
            )
            if not GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                return None
            if not operations.acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                lease_expires_at=expires,
            ):
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot delete reservation was lost."
                )
            return lease

    def update_phase(
        self, lease: SnapshotDeleteLease, phase: SnapshotDeleteOperationPhase
    ) -> None:
        now = self._clock()
        expires = now + SNAPSHOT_DELETE_LEASE_DURATION
        with self._session_factory.begin() as session:
            operation = SnapshotDeleteOperationRepository(session).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).renew(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not operation or not heavy:
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot delete leases are no longer owned."
                )

    def finish_success(self, lease: SnapshotDeleteLease) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            snapshots = SnapshotRepository(session)
            operations = SnapshotDeleteOperationRepository(session)
            if not snapshots.delete_deleting(lease.snapshot_id):
                raise SnapshotDeleteLeaseLostError(
                    "Deleting Snapshot metadata was not owned."
                )
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=SnapshotDeleteOperationState.SUCCEEDED,
                phase=SnapshotDeleteOperationPhase.SUCCEEDED,
                failure_reason=None,
                reconciliation_required=False,
                now=now,
            ):
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot deletion operation was not owned."
                )
            if not GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id, token_digest=lease.token_digest
            ):
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot delete heavy lease was not owned."
                )
            operations.prune_terminal(
                lease.managed_instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )

    def finish_failure(
        self,
        lease: SnapshotDeleteLease,
        *,
        reason: str,
        reconciliation_required: bool = True,
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            operations = SnapshotDeleteOperationRepository(session)
            state = (
                SnapshotDeleteOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else SnapshotDeleteOperationState.FAILED
            )
            phase = (
                SnapshotDeleteOperationPhase.RECONCILIATION_REQUIRED
                if reconciliation_required
                else SnapshotDeleteOperationPhase.FAILED
            )
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=state,
                phase=phase,
                failure_reason=reason[:256],
                reconciliation_required=reconciliation_required,
                now=now,
            ):
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot deletion operation was not owned."
                )
            if not GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id, token_digest=lease.token_digest
            ):
                raise SnapshotDeleteLeaseLostError(
                    "Snapshot delete heavy lease was not owned."
                )
            operations.prune_terminal(
                lease.managed_instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )

    def terminalize_expired(self, operation: SnapshotDeleteOperation) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            operations = SnapshotDeleteOperationRepository(session)
            operations.mark_stale_reconciliation(operation.id, now=now)
            GlobalHeavyOperationLeaseRepository(session).release_for_operation(
                operation.id
            )

    def inspect_reconciliation(
        self, operation: SnapshotDeleteOperation, *, data_root: Path
    ) -> SnapshotDeleteReconciliationState:
        with self._session_factory() as session:
            snapshot = SnapshotRepository(session).get(operation.snapshot_id)
        if snapshot is None or snapshot.state is not SnapshotState.DELETING:
            return SnapshotDeleteReconciliationState.INCONSISTENT_STORAGE
        presence = snapshot_artifact_presence(data_root, operation.snapshot_id)
        if presence == "directory":
            return SnapshotDeleteReconciliationState.SNAPSHOT_AND_ARTIFACT_PRESENT
        if presence == "absent":
            return SnapshotDeleteReconciliationState.SNAPSHOT_PRESENT_ARTIFACT_ABSENT
        return SnapshotDeleteReconciliationState.INCONSISTENT_STORAGE

    def get(self, operation_id: UUID) -> SnapshotDeleteOperation | None:
        with self._session_factory() as session:
            return SnapshotDeleteOperationRepository(session).get(operation_id)

    def queued(self) -> list[SnapshotDeleteOperation]:
        self._terminalize_stale_heavy_operation()
        with self._session_factory() as session:
            return SnapshotDeleteOperationRepository(session).list_queued()

    @staticmethod
    def to_status(operation: SnapshotDeleteOperation) -> SnapshotDeleteStatus:
        return SnapshotDeleteStatus(
            operation_id=operation.id,
            snapshot_id=operation.snapshot_id,
            managed_instance_id=operation.managed_instance_id,
            state=operation.state,
            phase=operation.phase,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            heartbeat_at=operation.heartbeat_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
        )

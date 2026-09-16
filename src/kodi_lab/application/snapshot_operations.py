"""Durable create-only snapshot queue and dual-lease coordination."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstanceMutationKind,
    Snapshot,
    SnapshotCreateOperation,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotState,
    SnapshotValidationResult,
    normalize_snapshot_label,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    SnapshotCreateOperationRepository,
    SnapshotRepository,
)

SNAPSHOT_LEASE_DURATION = timedelta(minutes=10)
TERMINAL_RETENTION_PER_INSTANCE = 20


class SnapshotCreateConflictError(RuntimeError):
    """Snapshot creation conflicts with active mutation or heavy work."""


class SnapshotCreateUnsupportedError(RuntimeError):
    """The requested source cannot be captured by this slice."""


class SnapshotCreateLeaseLostError(RuntimeError):
    """The executor no longer owns all three coordinated leases."""


class SnapshotCreateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    state: SnapshotCreateOperationState
    phase: SnapshotCreateOperationPhase
    requested_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class QueuedSnapshotCreate:
    operation: SnapshotCreateStatus
    snapshot: Snapshot


@dataclass(frozen=True, slots=True)
class SnapshotCreateLease:
    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class SnapshotCreateOperationCoordinator:
    """Own short SQLite transactions around one create-specific workflow."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def queue(self, instance_id: UUID, *, label: str | None) -> QueuedSnapshotCreate:
        self._terminalize_stale_heavy_operation()
        try:
            normalized_label = normalize_snapshot_label(label)
        except ValueError as error:
            raise SnapshotCreateUnsupportedError(str(error)) from error
        now = self._clock()
        operation_id = uuid4()
        snapshot_id = uuid4()
        try:
            with self._session_factory.begin() as session:
                instance = ManagedInstanceRepository(session).get(instance_id)
                if instance is None:
                    raise SnapshotCreateUnsupportedError(
                        "Managed instance does not exist."
                    )
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise SnapshotCreateUnsupportedError(
                        "Persistent snapshot capture is not supported."
                    )
                snapshot = Snapshot(
                    id=snapshot_id,
                    source_managed_instance_id=instance_id,
                    runtime_revision_id=instance.runtime_revision_id,
                    captured_fixture_definition_id=instance.fixture_definition_id,
                    captured_at=now,
                    prior_runtime_state=None,
                    label=normalized_label,
                    manifest_version=1,
                    archive_digest=None,
                    manifest_digest=None,
                    archive_size=None,
                    member_count=None,
                    state=SnapshotState.CREATING,
                    validation_at=None,
                    validation_result=SnapshotValidationResult.PENDING,
                )
                operation = SnapshotCreateOperation(
                    id=operation_id,
                    snapshot_id=snapshot_id,
                    managed_instance_id=instance_id,
                    requested_at=now,
                )
                SnapshotRepository(session).create(snapshot)
                SnapshotCreateOperationRepository(session).create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.SNAPSHOT_CREATE,
                    operation_id=operation_id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.SNAPSHOT_CREATE,
                    operation_id=operation_id,
                )
        except IntegrityError as error:
            raise SnapshotCreateConflictError(
                "Another managed-instance mutation or global heavy operation is active."
            ) from error
        return QueuedSnapshotCreate(self.to_status(operation), snapshot)

    def _terminalize_stale_heavy_operation(self) -> None:
        with self._session_factory() as session:
            heavy = GlobalHeavyOperationLeaseRepository(session).current()
            if (
                heavy is None
                or heavy.operation_kind is not GlobalHeavyOperationKind.SNAPSHOT_CREATE
                or heavy.lease_token_digest is None
                or heavy.lease_expires_at is None
                or datetime.fromisoformat(heavy.lease_expires_at.replace("Z", "+00:00"))
                > self._clock()
            ):
                return
            operation = SnapshotCreateOperationRepository(session).get(
                UUID(heavy.operation_id)
            )
        if operation is not None:
            self.terminalize_expired(operation)

    def acquire(self, operation_id: UUID) -> SnapshotCreateLease | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._session_factory.begin() as session:
            operations = SnapshotCreateOperationRepository(session)
            operation = operations.get(operation_id)
            if operation is None:
                return None
            lease = SnapshotCreateLease(
                operation_id=operation.id,
                snapshot_id=operation.snapshot_id,
                managed_instance_id=operation.managed_instance_id,
                token=token,
            )
            expires = now + SNAPSHOT_LEASE_DURATION
            mutation = ManagedInstanceMutationLeaseRepository(session)
            heavy = GlobalHeavyOperationLeaseRepository(session)
            if not mutation.claim(
                operation.managed_instance_id,
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                return None
            if not heavy.claim(
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                raise SnapshotCreateLeaseLostError(
                    "Global heavy-operation reservation is no longer owned."
                )
            if not operations.acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                lease_expires_at=expires,
            ):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot operation reservation is no longer owned."
                )
            return lease

    def update_phase(
        self, lease: SnapshotCreateLease, phase: SnapshotCreateOperationPhase
    ) -> None:
        now = self._clock()
        expires = now + SNAPSHOT_LEASE_DURATION
        with self._session_factory.begin() as session:
            operations = SnapshotCreateOperationRepository(session)
            mutation = ManagedInstanceMutationLeaseRepository(session)
            heavy = GlobalHeavyOperationLeaseRepository(session)
            updated = operations.update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=expires,
            )
            renewed_mutation = mutation.renew(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            renewed_heavy = heavy.renew(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not (updated and renewed_mutation and renewed_heavy):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot create leases are no longer owned."
                )

    def finish_success(self, lease: SnapshotCreateLease) -> None:
        self._finish(
            lease,
            state=SnapshotCreateOperationState.SUCCEEDED,
            phase=SnapshotCreateOperationPhase.SUCCEEDED,
            reason=None,
            reconciliation_required=False,
        )

    def finish_failure(
        self,
        lease: SnapshotCreateLease,
        *,
        reason: str,
        reconciliation_required: bool,
    ) -> None:
        operation = self.get(lease.operation_id)
        phase = (
            SnapshotCreateOperationPhase.RECONCILIATION_REQUIRED
            if reconciliation_required
            else SnapshotCreateOperationPhase.FAILED
        )
        if operation is not None and not reconciliation_required:
            phase = operation.phase
        self._finish(
            lease,
            state=(
                SnapshotCreateOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else SnapshotCreateOperationState.FAILED
            ),
            phase=phase,
            reason=reason.strip()[:256] or "Snapshot creation could not be completed.",
            reconciliation_required=reconciliation_required,
        )

    def _finish(
        self,
        lease: SnapshotCreateLease,
        *,
        state: SnapshotCreateOperationState,
        phase: SnapshotCreateOperationPhase,
        reason: str | None,
        reconciliation_required: bool,
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            operations = SnapshotCreateOperationRepository(session)
            snapshot = SnapshotRepository(session).get(lease.snapshot_id)
            if state is SnapshotCreateOperationState.SUCCEEDED and (
                snapshot is None or snapshot.state is not SnapshotState.AVAILABLE
            ):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot artifact is not independently available."
                )
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=state,
                phase=phase,
                failure_reason=reason,
                reconciliation_required=reconciliation_required,
                now=now,
            ):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot operation lease is no longer owned."
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot mutation lease is no longer owned."
                )
            if not GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise SnapshotCreateLeaseLostError(
                    "Snapshot global heavy lease is no longer owned."
                )
            operations.prune_terminal(
                lease.managed_instance_id,
                retain=TERMINAL_RETENTION_PER_INSTANCE,
            )

    def get(self, operation_id: UUID) -> SnapshotCreateOperation | None:
        with self._session_factory() as session:
            operation = SnapshotCreateOperationRepository(session).get(operation_id)
        if (
            operation is not None
            and operation.state is SnapshotCreateOperationState.RUNNING
            and (
                operation.lease_expires_at is None
                or operation.lease_expires_at <= self._clock()
            )
        ):
            return self.terminalize_expired(operation)
        return operation

    def terminalize_expired(
        self, operation: SnapshotCreateOperation
    ) -> SnapshotCreateOperation:
        if operation.state is not SnapshotCreateOperationState.RUNNING:
            return operation
        now = self._clock()
        if operation.lease_expires_at is not None and operation.lease_expires_at > now:
            return operation
        with self._session_factory.begin() as session:
            operations = SnapshotCreateOperationRepository(session)
            if operations.mark_stale_reconciliation(operation.id, now=now):
                SnapshotRepository(session).mark_invalid(
                    operation.snapshot_id, quarantined=True, validated_at=now
                )
                ManagedInstanceMutationLeaseRepository(session).release_for_operation(
                    operation.managed_instance_id, operation.id
                )
                GlobalHeavyOperationLeaseRepository(session).release_for_operation(
                    operation.id
                )
                operations.prune_terminal(
                    operation.managed_instance_id,
                    retain=TERMINAL_RETENTION_PER_INSTANCE,
                )
        with self._session_factory() as session:
            refreshed = SnapshotCreateOperationRepository(session).get(operation.id)
        assert refreshed is not None
        return refreshed

    def queued(self) -> list[SnapshotCreateOperation]:
        with self._session_factory() as session:
            return SnapshotCreateOperationRepository(session).list_queued()

    def list_snapshots(self, instance_id: UUID) -> list[Snapshot]:
        with self._session_factory() as session:
            return SnapshotRepository(session).list_for_instance(instance_id)

    def now(self) -> datetime:
        return self._clock()

    def mark_snapshot_invalid(self, snapshot_id: UUID, *, quarantined: bool) -> bool:
        with self._session_factory.begin() as session:
            return SnapshotRepository(session).mark_invalid(
                snapshot_id,
                quarantined=quarantined,
                validated_at=self._clock(),
            )

    @staticmethod
    def to_status(operation: SnapshotCreateOperation) -> SnapshotCreateStatus:
        return SnapshotCreateStatus(
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

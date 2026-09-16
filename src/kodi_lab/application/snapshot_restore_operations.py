"""Durable, restore-specific queue and dual-lease coordination."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstanceMutationKind,
    SnapshotRestoreOperation,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotRestoreReconciliationState,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    SnapshotRepository,
    SnapshotRestoreOperationRepository,
)

SNAPSHOT_RESTORE_LEASE_DURATION = timedelta(minutes=10)
TERMINAL_RETENTION_PER_INSTANCE = 20


class SnapshotRestoreConflictError(RuntimeError):
    """Restore conflicts with another instance mutation or heavy operation."""


class SnapshotRestoreUnsupportedError(RuntimeError):
    """Restore eligibility failed before Docker mutation."""


class SnapshotRestoreLeaseLostError(RuntimeError):
    """The executor no longer owns its typed operation and shared leases."""


class SnapshotRestoreStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    state: SnapshotRestoreOperationState
    phase: SnapshotRestoreOperationPhase
    prior_source_runtime_state: InstanceRuntimeState | None
    requested_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    reconciliation_state: SnapshotRestoreReconciliationState | None = None


class _InventoryBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...


@dataclass(frozen=True, slots=True)
class SnapshotRestoreLease:
    operation_id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class SnapshotRestoreOperationCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def queue(self, instance_id: UUID, snapshot_id: UUID) -> SnapshotRestoreStatus:
        self._terminalize_stale_heavy_operation()
        now = self._clock()
        operation = SnapshotRestoreOperation(
            id=uuid4(),
            snapshot_id=snapshot_id,
            managed_instance_id=instance_id,
            requested_at=now,
        )
        try:
            with self._session_factory.begin() as session:
                instances = ManagedInstanceRepository(session)
                snapshots = SnapshotRepository(session)
                instance = instances.get(instance_id)
                snapshot = snapshots.get(snapshot_id)
                if instance is None or snapshot is None:
                    raise SnapshotRestoreUnsupportedError(
                        "Managed instance or snapshot does not exist."
                    )
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise SnapshotRestoreUnsupportedError(
                        "Persistent snapshot restore is not supported."
                    )
                if snapshot.source_managed_instance_id != instance_id:
                    raise SnapshotRestoreUnsupportedError(
                        "Snapshot belongs to a different managed instance."
                    )
                if (
                    snapshot.state is not SnapshotState.AVAILABLE
                    or snapshot.validation_result is not SnapshotValidationResult.PASSED
                ):
                    raise SnapshotRestoreUnsupportedError(
                        "Only an available validated snapshot can be restored."
                    )
                if snapshot.runtime_revision_id != instance.runtime_revision_id:
                    raise SnapshotRestoreUnsupportedError(
                        "Snapshot RuntimeRevision differs from the current exact pin."
                    )
                if (
                    snapshot.captured_fixture_definition_id
                    != instance.fixture_definition_id
                ):
                    raise SnapshotRestoreUnsupportedError(
                        "Snapshot fixture assignment differs from current metadata."
                    )
                existing = SnapshotRestoreOperationRepository(
                    session
                ).active_for_instance(instance_id)
                if existing is not None and existing.snapshot_id == snapshot_id:
                    return self.to_status(existing)
                SnapshotRestoreOperationRepository(session).create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.SNAPSHOT_RESTORE,
                    operation_id=operation.id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.SNAPSHOT_RESTORE,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise SnapshotRestoreConflictError(
                "Another managed-instance mutation or global heavy operation is active."
            ) from error
        return self.to_status(operation)

    def _terminalize_stale_heavy_operation(self) -> None:
        with self._session_factory() as session:
            heavy = GlobalHeavyOperationLeaseRepository(session).current()
            if (
                heavy is None
                or heavy.operation_kind is not GlobalHeavyOperationKind.SNAPSHOT_RESTORE
                or heavy.lease_token_digest is None
                or heavy.lease_expires_at is None
                or datetime.fromisoformat(heavy.lease_expires_at.replace("Z", "+00:00"))
                > self._clock()
            ):
                return
            operation = SnapshotRestoreOperationRepository(session).get(
                UUID(heavy.operation_id)
            )
        if operation is not None:
            self.terminalize_expired(operation)

    def acquire(self, operation_id: UUID) -> SnapshotRestoreLease | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + SNAPSHOT_RESTORE_LEASE_DURATION
        with self._session_factory.begin() as session:
            operations = SnapshotRestoreOperationRepository(session)
            operation = operations.get(operation_id)
            if operation is None:
                return None
            lease = SnapshotRestoreLease(
                operation.id,
                operation.snapshot_id,
                operation.managed_instance_id,
                token,
            )
            if not ManagedInstanceMutationLeaseRepository(session).claim(
                operation.managed_instance_id,
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                return None
            if not GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            ):
                raise SnapshotRestoreLeaseLostError("Global heavy lease was lost.")
            if not operations.acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                lease_expires_at=expires,
            ):
                raise SnapshotRestoreLeaseLostError("Restore reservation was lost.")
            return lease

    def update_phase(
        self, lease: SnapshotRestoreLease, phase: SnapshotRestoreOperationPhase
    ) -> None:
        now = self._clock()
        expires = now + SNAPSHOT_RESTORE_LEASE_DURATION
        with self._session_factory.begin() as session:
            ok = SnapshotRestoreOperationRepository(session).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=expires,
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).renew(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not (ok and mutation and heavy):
                raise SnapshotRestoreLeaseLostError(
                    "Restore leases are no longer owned."
                )

    def record_prior_state(
        self, lease: SnapshotRestoreLease, state: InstanceRuntimeState
    ) -> None:
        with self._session_factory.begin() as session:
            if not SnapshotRestoreOperationRepository(session).set_prior_state(
                lease.operation_id, token_digest=lease.token_digest, state=state
            ):
                raise SnapshotRestoreLeaseLostError(
                    "Prior source state was not recorded."
                )

    def record_container_identity(
        self,
        lease: SnapshotRestoreLease,
        *,
        old_container_id: str | None = None,
        fresh_container_id: str | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            if not SnapshotRestoreOperationRepository(session).set_container_identity(
                lease.operation_id,
                token_digest=lease.token_digest,
                old_container_id=old_container_id,
                fresh_container_id=fresh_container_id,
            ):
                raise SnapshotRestoreLeaseLostError(
                    "Restore container identity was not recorded."
                )

    def finish_success(self, lease: SnapshotRestoreLease) -> None:
        self._finish(
            lease,
            SnapshotRestoreOperationState.SUCCEEDED,
            SnapshotRestoreOperationPhase.SUCCEEDED,
            None,
            False,
        )

    def finish_failure(
        self, lease: SnapshotRestoreLease, *, reason: str, reconciliation_required: bool
    ) -> None:
        operation = self.get(lease.operation_id)
        phase = (
            operation.phase
            if operation is not None
            else SnapshotRestoreOperationPhase.FAILED
        )
        self._finish(
            lease,
            (
                SnapshotRestoreOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else SnapshotRestoreOperationState.FAILED
            ),
            phase,
            reason.strip()[:256] or "Snapshot restore could not be completed.",
            reconciliation_required,
        )

    def _finish(
        self,
        lease: SnapshotRestoreLease,
        state: SnapshotRestoreOperationState,
        phase: SnapshotRestoreOperationPhase,
        reason: str | None,
        reconciliation_required: bool,
    ) -> None:
        with self._session_factory.begin() as session:
            operations = SnapshotRestoreOperationRepository(session)
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=state,
                phase=phase,
                failure_reason=reason,
                reconciliation_required=reconciliation_required,
                now=self._clock(),
            ):
                raise SnapshotRestoreLeaseLostError("Restore operation lease was lost.")
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise SnapshotRestoreLeaseLostError("Mutation lease was lost.")
            if not GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id, token_digest=lease.token_digest
            ):
                raise SnapshotRestoreLeaseLostError("Global heavy lease was lost.")
            operations.prune_terminal(
                lease.managed_instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )

    def get(self, operation_id: UUID) -> SnapshotRestoreOperation | None:
        with self._session_factory() as session:
            operation = SnapshotRestoreOperationRepository(session).get(operation_id)
        if (
            operation is not None
            and operation.state is SnapshotRestoreOperationState.RUNNING
            and (
                operation.lease_expires_at is None
                or operation.lease_expires_at <= self._clock()
            )
        ):
            return self.terminalize_expired(operation)
        return operation

    def terminalize_expired(
        self, operation: SnapshotRestoreOperation
    ) -> SnapshotRestoreOperation:
        if operation.state is not SnapshotRestoreOperationState.RUNNING:
            return operation
        now = self._clock()
        if operation.lease_expires_at is not None and operation.lease_expires_at > now:
            return operation
        with self._session_factory.begin() as session:
            repository = SnapshotRestoreOperationRepository(session)
            if repository.mark_stale_reconciliation(operation.id, now=now):
                ManagedInstanceMutationLeaseRepository(session).release_for_operation(
                    operation.managed_instance_id, operation.id
                )
                GlobalHeavyOperationLeaseRepository(session).release_for_operation(
                    operation.id
                )
        with self._session_factory() as session:
            refreshed = SnapshotRestoreOperationRepository(session).get(operation.id)
        assert refreshed is not None
        return refreshed

    def queued(self) -> list[SnapshotRestoreOperation]:
        with self._session_factory() as session:
            return SnapshotRestoreOperationRepository(session).list_queued()

    @staticmethod
    def inspect_reconciliation(
        operation: SnapshotRestoreOperation, broker: _InventoryBroker
    ) -> SnapshotRestoreReconciliationState:
        matches = [
            item
            for item in broker.managed_container_inventory().containers
            if item.managed_instance_id == str(operation.managed_instance_id)
        ]
        if not matches:
            return SnapshotRestoreReconciliationState.UNPROVISIONED
        if len(matches) != 1:
            return SnapshotRestoreReconciliationState.AMBIGUOUS_TRANSITIONAL
        item = matches[0]
        if operation.old_container_id == item.container_id:
            return SnapshotRestoreReconciliationState.OLD_CONTAINER_PRESENT
        if operation.fresh_container_id != item.container_id:
            return SnapshotRestoreReconciliationState.AMBIGUOUS_TRANSITIONAL
        if item.runtime_state is ContainerRuntimeState.STOPPED:
            return SnapshotRestoreReconciliationState.FRESH_CONTAINER_STOPPED
        if item.runtime_state is ContainerRuntimeState.RUNNING:
            return SnapshotRestoreReconciliationState.FRESH_CONTAINER_RUNNING
        return SnapshotRestoreReconciliationState.AMBIGUOUS_TRANSITIONAL

    @staticmethod
    def to_status(operation: SnapshotRestoreOperation) -> SnapshotRestoreStatus:
        return SnapshotRestoreStatus(
            operation_id=operation.id,
            snapshot_id=operation.snapshot_id,
            managed_instance_id=operation.managed_instance_id,
            state=operation.state,
            phase=operation.phase,
            prior_source_runtime_state=operation.prior_source_runtime_state,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            heartbeat_at=operation.heartbeat_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
            reconciliation_state=None,
        )

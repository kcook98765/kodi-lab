"""Reset-specific durable coordination, leasing, and asynchronous execution."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.reset import ResetError, ResetReconciliationRequiredError
from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivitySnapshot,
    ResetActivityState,
)
from kodi_lab.domain import (
    ManagedInstanceMutationKind,
    ResetOperation,
    ResetOperationPhase,
    ResetOperationState,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ResetOperationRepository,
)

LEASE_DURATION = timedelta(minutes=10)
TERMINAL_RETENTION_PER_INSTANCE = 20


class ResetOperationConflictError(RuntimeError):
    """A durable reset operation prevents the requested mutation."""


class ResetLeaseLostError(RuntimeError):
    """The executor no longer owns a fresh durable lease."""


class ResetOperationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    managed_instance_id: UUID
    state: ResetOperationState
    phase: ResetOperationPhase
    requested_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool


@dataclass(frozen=True, slots=True)
class QueuedResetOperation:
    operation: ResetOperationStatus
    created: bool


@dataclass(frozen=True, slots=True)
class ResetLease:
    operation_id: UUID
    managed_instance_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class ResetOperationCoordinator:
    """Own short reset-operation transactions; never spans Docker work."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_dispatcher: StaleOperationReconciliationDispatcher | None = None,
        reconciliation: ManagedInstanceReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._stale = stale_dispatcher or StaleOperationReconciliationDispatcher(
            session_factory, clock=clock
        )
        self._reconciliation = reconciliation or ManagedInstanceReconciliationService(
            session_factory
        )

    def queue(self, instance_id: UUID) -> QueuedResetOperation:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(instance_id, ReconciliationAction.RESET)
        self.active_for_instance(instance_id)
        operation = ResetOperation(
            id=uuid4(), managed_instance_id=instance_id, requested_at=self._clock()
        )
        try:
            with self._session_factory.begin() as session:
                repository = ResetOperationRepository(session)
                existing = repository.active_for_instance(instance_id)
                if existing is not None:
                    return QueuedResetOperation(self.to_status(existing), False)
                latest = repository.latest_for_instance(instance_id)
                if latest is not None and latest.reconciliation_required:
                    raise ResetOperationConflictError(
                        "Reset reconciliation is required before another request."
                    )
                repository.create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.RESET,
                    operation_id=operation.id,
                )
                return QueuedResetOperation(self.to_status(operation), True)
        except IntegrityError:
            with self._session_factory() as session:
                existing = ResetOperationRepository(session).active_for_instance(
                    instance_id
                )
                if existing is None:
                    raise ResetOperationConflictError(
                        "A reset operation could not be queued safely."
                    ) from None
                return QueuedResetOperation(self.to_status(existing), False)

    def acquire(self, operation_id: UUID) -> ResetLease | None:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._session_factory.begin() as session:
            repository = ResetOperationRepository(session)
            operation = repository.get(operation_id)
            if operation is None:
                return None
            lease = ResetLease(operation.id, operation.managed_instance_id, token)
            if not ManagedInstanceMutationLeaseRepository(session).claim(
                operation.managed_instance_id,
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=now + LEASE_DURATION,
            ):
                return None
            acquired = repository.acquire(
                operation_id,
                token_digest=lease.token_digest,
                now=now,
                lease_expires_at=now + LEASE_DURATION,
            )
            return lease if acquired else None

    def update_phase(self, lease: ResetLease, phase: ResetOperationPhase) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            updated = ResetOperationRepository(session).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=now + LEASE_DURATION,
            )
            renewed = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=now + LEASE_DURATION,
            )
            if not updated or not renewed:
                raise ResetLeaseLostError("reset operation lease is no longer owned")

    def finish_success(self, lease: ResetLease) -> None:
        self._finish(
            lease,
            state=ResetOperationState.SUCCEEDED,
            phase=ResetOperationPhase.SUCCEEDED,
            reason=None,
            reconciliation_required=False,
        )

    def finish_failure(
        self,
        lease: ResetLease,
        *,
        reason: str,
        reconciliation_required: bool,
    ) -> None:
        current = self.latest_for_instance(lease.managed_instance_id)
        phase = (
            ResetOperationPhase.PREFLIGHT
            if current is None or current.id != lease.operation_id
            else current.phase
        )
        self._finish(
            lease,
            state=(
                ResetOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else ResetOperationState.FAILED
            ),
            phase=phase,
            reason=reason.strip()[:256] or "Reset could not be completed.",
            reconciliation_required=reconciliation_required,
        )

    def _finish(
        self,
        lease: ResetLease,
        *,
        state: ResetOperationState,
        phase: ResetOperationPhase,
        reason: str | None,
        reconciliation_required: bool,
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            repository = ResetOperationRepository(session)
            updated = repository.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=state,
                phase=phase,
                failure_reason=reason,
                reconciliation_required=reconciliation_required,
                now=now,
            )
            if not updated:
                raise ResetLeaseLostError("reset operation lease is no longer owned")
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise ResetLeaseLostError("reset mutation lease is no longer owned")
            repository.prune_terminal(
                lease.managed_instance_id,
                retain=TERMINAL_RETENTION_PER_INSTANCE,
            )

    def active_for_instance(self, instance_id: UUID) -> ResetOperation | None:
        with self._session_factory() as session:
            operation = ResetOperationRepository(session).active_for_instance(
                instance_id
            )
        if (
            operation is not None
            and operation.state is ResetOperationState.RUNNING
            and not operation.lease_is_fresh(self._clock())
        ):
            self.terminalize_expired(operation)
            return None
        return operation

    def latest_for_instance(self, instance_id: UUID) -> ResetOperation | None:
        with self._session_factory() as session:
            return ResetOperationRepository(session).latest_for_instance(instance_id)

    def queued(self) -> list[ResetOperation]:
        with self._session_factory() as session:
            return ResetOperationRepository(session).list_queued()

    def terminalize_expired(self, operation: ResetOperation) -> ResetOperation:
        if operation.state is not ResetOperationState.RUNNING:
            return operation
        now = self._clock()
        if operation.lease_expires_at is not None and operation.lease_expires_at > now:
            return operation
        self._stale.sweep_instance(operation.managed_instance_id)
        refreshed = self.latest_for_instance(operation.managed_instance_id)
        assert refreshed is not None
        return refreshed

    def require_no_active(self, instance_id: UUID) -> None:
        if self.active_for_instance(instance_id) is not None:
            raise ResetOperationConflictError(
                "A reset operation is active for this managed instance."
            )

    @staticmethod
    def to_status(operation: ResetOperation) -> ResetOperationStatus:
        return ResetOperationStatus(
            operation_id=operation.id,
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


class DurableResetActivityObserver:
    """Publish local progress and lease-owned durable phase/result updates."""

    def __init__(
        self,
        local: ResetActivityRegistry,
        coordinator: ResetOperationCoordinator,
        lease: ResetLease,
    ) -> None:
        self._local = local
        self._coordinator = coordinator
        self._lease = lease

    def begin(self, instance_id: UUID) -> ResetActivitySnapshot:
        self._coordinator.update_phase(self._lease, ResetOperationPhase.PREFLIGHT)
        return self._local.begin(instance_id)

    def transition(
        self, instance_id: UUID, generation: int, state: ResetActivityState
    ) -> ResetActivitySnapshot:
        self._coordinator.update_phase(self._lease, ResetOperationPhase(state.value))
        return self._local.transition(instance_id, generation, state)

    def succeed(self, instance_id: UUID, generation: int) -> ResetActivitySnapshot:
        self._coordinator.finish_success(self._lease)
        return self._local.succeed(instance_id, generation)

    def fail(
        self,
        instance_id: UUID,
        generation: int,
        *,
        failure_phase: str,
        failure_reason: str,
        reconciliation_required: bool,
    ) -> ResetActivitySnapshot:
        self._coordinator.finish_failure(
            self._lease,
            reason=failure_reason,
            reconciliation_required=reconciliation_required,
        )
        return self._local.fail(
            instance_id,
            generation,
            failure_phase=failure_phase,
            failure_reason=failure_reason,
            reconciliation_required=reconciliation_required,
        )


class _ResetService(Protocol):
    def reset(self, managed_instance_id: UUID) -> object: ...


type ResetServiceFactory = Callable[[DurableResetActivityObserver], _ResetService]


class ResetExecutor:
    """Small in-process runner whose correctness comes from the durable lease."""

    def __init__(
        self,
        coordinator: ResetOperationCoordinator,
        local_activity: ResetActivityRegistry,
        service_factory: ResetServiceFactory,
        *,
        max_workers: int = 2,
    ) -> None:
        self._coordinator = coordinator
        self._local = local_activity
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="reset"
        )
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise ResetOperationConflictError("Reset executor is shutting down.")
            self._pool.submit(self._execute, operation_id)

    def _execute(self, operation_id: UUID) -> None:
        lease = self._coordinator.acquire(operation_id)
        if lease is None:
            return
        observer = DurableResetActivityObserver(self._local, self._coordinator, lease)
        try:
            self._service_factory(observer).reset(lease.managed_instance_id)
        except ResetError as error:
            with suppress(ResetLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason=error.safe_reason,
                    reconciliation_required=isinstance(
                        error, ResetReconciliationRequiredError
                    ),
                )
            return
        except ResetLeaseLostError:
            return
        except Exception:
            with suppress(ResetLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason="Reset could not be completed.",
                    reconciliation_required=False,
                )

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=False, cancel_futures=True)

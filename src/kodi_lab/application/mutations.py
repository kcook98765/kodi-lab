"""Narrow per-instance mutation exclusion shared by typed workflows."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

MUTATION_LEASE_DURATION = timedelta(minutes=10)


class ManagedInstanceMutationConflictError(RuntimeError):
    """Another typed mutation owns this managed instance."""


class ManagedInstanceMutationLeaseLostError(RuntimeError):
    """The caller no longer owns its exact mutation lease."""


@dataclass(frozen=True, slots=True)
class ManagedInstanceMutationLease:
    managed_instance_id: UUID
    operation_kind: ManagedInstanceMutationKind
    operation_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class ManagedInstanceMutationCoordinator:
    """Acquire typed short-lived leases without creating a generic job system."""

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

    def acquire(
        self, instance_id: UUID, kind: ManagedInstanceMutationKind
    ) -> ManagedInstanceMutationLease:
        self._stale.sweep_instance(instance_id)
        action = {
            ManagedInstanceMutationKind.LIFECYCLE: ReconciliationAction.PROVISION,
            ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT: (
                ReconciliationAction.FIXTURE_ASSIGNMENT
            ),
            ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT: (
                ReconciliationAction.RUNTIME_REASSIGNMENT
            ),
            ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT: (
                ReconciliationAction.DEVELOPMENT_SOURCE_ATTACHMENT
            ),
            ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT: (
                ReconciliationAction.DEVELOPMENT_SOURCE_ENABLEMENT
            ),
        }.get(kind)
        if action is not None:
            self._reconciliation.require_allowed(instance_id, action)
        lease = ManagedInstanceMutationLease(
            managed_instance_id=instance_id,
            operation_kind=kind,
            operation_id=uuid4(),
            token=secrets.token_urlsafe(32),
        )
        now = self._clock()
        try:
            with self._session_factory.begin() as session:
                repository = ManagedInstanceMutationLeaseRepository(session)
                repository.release_expired_short_mutation(instance_id, now=now)
                repository.reserve(
                    instance_id, kind=kind, operation_id=lease.operation_id
                )
                if not repository.claim(
                    instance_id,
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                    now=now,
                    expires_at=now + MUTATION_LEASE_DURATION,
                ):
                    raise ManagedInstanceMutationConflictError(
                        "Managed-instance mutation authority could not be acquired."
                    )
        except IntegrityError as error:
            raise ManagedInstanceMutationConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return lease

    def release(self, lease: ManagedInstanceMutationLease) -> None:
        with self._session_factory.begin() as session:
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise ManagedInstanceMutationLeaseLostError(
                    "Managed-instance mutation lease is no longer owned."
                )

    @contextmanager
    def hold(
        self, instance_id: UUID, kind: ManagedInstanceMutationKind
    ) -> Iterator[ManagedInstanceMutationLease]:
        lease = self.acquire(instance_id, kind)
        try:
            yield lease
        finally:
            self.release(lease)

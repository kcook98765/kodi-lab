"""Coordination for typed synchronous lifecycle-operation history."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mutations import MUTATION_LEASE_DURATION
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
    ManagedInstanceSemanticState,
    TargetAvailability,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeCurrentProjection,
    ManagedRuntimeProjectionRepository,
    ManagedRuntimeProjectionResult,
    ManagedRuntimeReconciliationBlockedError,
)


class LifecycleOperationConflictError(RuntimeError):
    """Lifecycle history or lease authority conflicts with another mutation."""


class LifecycleOperationLeaseLostError(RuntimeError):
    """The operation can no longer prove its exact mutation authority."""


@dataclass(frozen=True, slots=True)
class RunningLifecycleOperation:
    operation_id: UUID
    instance_id: UUID
    token: str
    runtime_state_revision: int = 0
    preserve_native_projection: bool = False

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


_ACTION = {
    ManagedInstanceLifecycleKind.PROVISION: ReconciliationAction.PROVISION,
    ManagedInstanceLifecycleKind.START: ReconciliationAction.START,
    ManagedInstanceLifecycleKind.STOP: ReconciliationAction.STOP,
    ManagedInstanceLifecycleKind.DEPROVISION: ReconciliationAction.DEPROVISION,
}


class ManagedInstanceLifecycleOperationCoordinator:
    """Atomically bind a running typed record to the existing mutation lease."""

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

    def begin(
        self,
        instance_id: UUID,
        *,
        instance_name: str,
        kind: ManagedInstanceLifecycleKind,
        prior_state: ManagedInstanceSemanticState,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
    ) -> RunningLifecycleOperation:
        return self._begin(
            instance_id,
            instance_name=instance_name,
            kind=kind,
            prior_state=prior_state,
            operation_id=operation_id,
            precondition=precondition,
            on_claim=on_claim,
        )

    def begin_bridge_preparation(
        self, instance_id: UUID, *, precondition: Callable[[Session], None]
    ) -> RunningLifecycleOperation:
        """Own resident preparation while preserving native lifecycle identity.

        No public lifecycle request can select this path. Unknown/interrupted
        results use ordinary typed START reconciliation and invalidate observations.
        """
        return self._begin(
            instance_id,
            instance_name="Resident Bridge preparation",
            kind=ManagedInstanceLifecycleKind.START,
            prior_state=ManagedInstanceSemanticState.RUNNING_USABLE,
            precondition=precondition,
            preserve_native_projection=True,
        )

    def _begin(
        self,
        instance_id: UUID,
        *,
        instance_name: str,
        kind: ManagedInstanceLifecycleKind,
        prior_state: ManagedInstanceSemanticState,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
        preserve_native_projection: bool = False,
    ) -> RunningLifecycleOperation:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(instance_id, _ACTION[kind])
        handle = RunningLifecycleOperation(
            operation_id or uuid4(),
            instance_id,
            secrets.token_urlsafe(32),
            preserve_native_projection=preserve_native_projection,
        )
        now = self._clock()
        operation = ManagedInstanceLifecycleOperation(
            id=handle.operation_id,
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            instance_name=instance_name,
            kind=kind,
            requested_at=now,
            started_at=now,
            prior_state=prior_state,
        )
        try:
            with self._session_factory.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if kind is ManagedInstanceLifecycleKind.START:
                    from kodi_lab.storage.retained_development import (
                        require_retained_selection,
                    )

                    require_retained_selection(session, str(instance_id))
                if precondition is not None:
                    precondition(session)
                ManagedInstanceLifecycleOperationRepository(session).create(operation)
                leases = ManagedInstanceMutationLeaseRepository(session)
                leases.reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.LIFECYCLE,
                    operation_id=handle.operation_id,
                    preserve_native_projection=preserve_native_projection,
                )
                observation = ManagedRuntimeProjectionRepository(
                    session
                ).observation_token(instance_id)
                if observation is None:
                    raise LifecycleOperationConflictError(
                        "Managed runtime projection is unavailable."
                    )
                handle = replace(
                    handle, runtime_state_revision=observation.runtime_state_revision
                )
                if not leases.claim(
                    instance_id,
                    operation_id=handle.operation_id,
                    token_digest=handle.token_digest,
                    now=now,
                    expires_at=now + MUTATION_LEASE_DURATION,
                ):
                    raise LifecycleOperationConflictError(
                        "Lifecycle mutation authority could not be acquired."
                    )
                if on_claim is not None:
                    on_claim(session, handle)
        except ManagedRuntimeReconciliationBlockedError as error:
            raise LifecycleOperationConflictError(
                "Runtime recovery review is required before this lifecycle action."
            ) from error
        except IntegrityError as error:
            raise LifecycleOperationConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return handle

    def terminalize(
        self,
        handle: RunningLifecycleOperation,
        *,
        state: ManagedInstanceLifecycleOperationState,
        resulting_state: ManagedInstanceSemanticState,
        failure_reason: str | None = None,
        on_terminal: Callable[[Session, ManagedInstanceLifecycleOperation], None]
        | None = None,
    ) -> ManagedInstanceLifecycleOperation:
        now = self._clock()
        reconciliation_required = (
            state is ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
        )
        with self._session_factory.begin() as session:
            operations = ManagedInstanceLifecycleOperationRepository(session)
            if not operations.terminalize(
                handle.operation_id,
                state=state,
                resulting_state=resulting_state,
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
                now=now,
            ):
                raise LifecycleOperationLeaseLostError(
                    "Lifecycle operation is no longer running."
                )
            projection = (
                None
                if failure_reason == "broker_runtime_dispatch_unconfirmed"
                else self._projection(resulting_state)
            )
            if projection is not None:
                applied = ManagedRuntimeProjectionRepository(session).finalize_owned(
                    handle.instance_id,
                    operation_id=handle.operation_id,
                    token_digest=handle.token_digest,
                    owned_revision=handle.runtime_state_revision,
                    projection=projection,
                )
                if applied is not ManagedRuntimeProjectionResult.APPLIED:
                    raise LifecycleOperationLeaseLostError(
                        "Lifecycle projection authority is no longer owned."
                    )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                handle.instance_id,
                operation_id=handle.operation_id,
                token_digest=handle.token_digest,
                preserve_native_projection=handle.preserve_native_projection
                and not reconciliation_required,
                record_use=not handle.preserve_native_projection,
            ):
                raise LifecycleOperationLeaseLostError(
                    "Lifecycle mutation lease is no longer owned."
                )
            operation = operations.get(handle.operation_id)
            assert operation is not None
            if on_terminal is not None:
                on_terminal(session, operation)
            operations.prune_terminal(handle.instance_id)
            retained = operations.get(handle.operation_id)
            assert retained is not None
            return retained

    @staticmethod
    def _projection(
        state: ManagedInstanceSemanticState,
    ) -> ManagedRuntimeCurrentProjection | None:
        if state in {
            ManagedInstanceSemanticState.AMBIGUOUS,
            ManagedInstanceSemanticState.TRANSITIONAL,
        }:
            return None
        usable = state is ManagedInstanceSemanticState.RUNNING_USABLE
        running = state in {
            ManagedInstanceSemanticState.RUNNING_USABLE,
            ManagedInstanceSemanticState.RUNNING_NOT_READY,
        }
        return ManagedRuntimeCurrentProjection(
            runtime_state=(
                InstanceRuntimeState.RUNNING
                if running
                else InstanceRuntimeState.STOPPED
            ),
            availability=(
                TargetAvailability.AVAILABLE
                if usable
                else TargetAvailability.UNAVAILABLE
            ),
            health=HealthStatus.HEALTHY if usable else HealthStatus.WARNING,
            connection_state=(
                ConnectionState.CONNECTED if usable else ConnectionState.DISCONNECTED
            ),
        )

    def record_prior_state(
        self,
        handle: RunningLifecycleOperation,
        prior_state: ManagedInstanceSemanticState,
    ) -> None:
        """Record the trusted observation while exact mutation authority is held."""

        now = self._clock()
        with self._session_factory.begin() as session:
            lease = ManagedInstanceMutationLeaseRepository(session).current(
                handle.instance_id
            )
            if (
                lease is None
                or lease.operation_id != str(handle.operation_id)
                or lease.lease_token_digest != handle.token_digest
                or lease.lease_expires_at is None
                or datetime.fromisoformat(lease.lease_expires_at.replace("Z", "+00:00"))
                <= now
            ):
                raise LifecycleOperationLeaseLostError(
                    "Lifecycle mutation authority is no longer current."
                )
            if not ManagedInstanceLifecycleOperationRepository(
                session
            ).update_prior_state(handle.operation_id, prior_state=prior_state):
                raise LifecycleOperationLeaseLostError(
                    "Lifecycle operation is no longer running."
                )

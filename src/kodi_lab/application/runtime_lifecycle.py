"""Trusted application coordination for Web lifecycle actions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_installations import (
    invalidate_addon_zip_installation_evidence,
)
from kodi_lab.application.development_source_deployments import (
    invalidate_development_source_deployments,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.lifecycle_operations import (
    ManagedInstanceLifecycleOperationCoordinator,
    RunningLifecycleOperation,
)
from kodi_lab.application.lifecycle_status import (
    observe_lifecycle_semantic_state,
    semantic_state_is_unambiguous,
)
from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeMutationUnconfirmedError,
    ManagedBridgeTrustService,
    invalidate_managed_bridge_trust,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reset_operations import ResetOperationCoordinator
from kodi_lab.application.runtime_retirement_receipts import (
    RuntimeRetirementReceiptError,
    RuntimeRetirementScope,
    confirm_retirement_receipt,
    prepare_retirement_receipt,
    require_retirement_scope,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientError,
    ClientProtocolError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
    RuntimeExecutionProfile,
)
from kodi_lab.kodi.readiness import KodiReadinessCoordinator, ReadinessResult
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
    TargetRepository,
)


class LifecycleConflictError(RuntimeError):
    """Trusted current state does not permit the requested semantic action."""


class LifecycleDispatchUnconfirmedError(RuntimeError):
    """The Broker may still be executing an accepted runtime mutation."""


class _ReadinessCoordinator(Protocol):
    def wait_until_usable(
        self, managed_instance_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> ReadinessResult: ...


type ReadinessCoordinatorFactory = Callable[
    ["_LifecycleBroker", RuntimeExecutionProfile], _ReadinessCoordinator
]


class _LifecycleBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult: ...

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult: ...

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult: ...

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: Literal[ContainerRuntimeState.STOPPED],
    ) -> DeprovisionManagedContainerResult: ...


class ManagedRuntimeLifecycleService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _LifecycleBroker,
        readiness: ReadinessTaskRegistry,
        status: ManagedRuntimeStatusService,
        coordinator_factory: ReadinessCoordinatorFactory = KodiReadinessCoordinator,
        reset_operations: ResetOperationCoordinator | None = None,
        lifecycle_operations: ManagedInstanceLifecycleOperationCoordinator
        | None = None,
        bridge_trust: ManagedBridgeTrustService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._readiness = readiness
        self._status = status
        self._coordinator_factory = coordinator_factory
        self._reset_operations = reset_operations
        self._bridge_trust = bridge_trust
        self._operations = lifecycle_operations or (
            ManagedInstanceLifecycleOperationCoordinator(session_factory)
        )

    def provision(
        self,
        instance_id: UUID,
        *,
        precondition: Callable[[Session], None] | None = None,
        operation_id: UUID | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
    ) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(
            instance_id,
            ManagedInstanceLifecycleKind.PROVISION,
            self._provision,
            precondition=precondition,
            operation_id=operation_id,
            on_claim=on_claim,
        )

    def _provision(self, instance_id: UUID, _profile: RuntimeExecutionProfile) -> None:
        try:
            self._broker.provision_managed_container(
                managed_instance_id=str(instance_id)
            )
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error
        except RemoteBrokerError as error:
            from kodi_lab.storage.models import ManagedProvisionReservationRow

            with self._session_factory() as session:
                reservation = session.get(
                    ManagedProvisionReservationRow, str(instance_id)
                )
                uncertain = (
                    reservation is not None
                    and reservation.runtime_revision_id
                    == str(_profile.runtime_revision_id)
                )
            if uncertain or error.code == "internal_error":
                raise LifecycleDispatchUnconfirmedError from error
            raise
        self._readiness.clear(instance_id)

    def start(
        self,
        instance_id: UUID,
        *,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(
            instance_id,
            ManagedInstanceLifecycleKind.START,
            self._start,
            operation_id=operation_id,
            precondition=precondition,
            on_claim=on_claim,
            before_dispatch=before_dispatch,
        )

    def install_reviewed_bridge(self, instance_id: UUID) -> ManagedRuntimeStatus:
        """Closed candidate installation under the existing lifecycle mutation lease."""
        self._require_no_reset(instance_id)

        def install(target_id: UUID, profile: RuntimeExecutionProfile) -> None:
            if (
                profile.kodi_release.canonical
                not in {"19.4", "20.5", "21.3", "22.0-rc.1"}
                or self._bridge_trust is None
            ):
                raise LifecycleConflictError("reviewed Bridge candidate unavailable")
            self._bridge_trust.stage_reviewed(target_id)
            self._stop(target_id, profile)
            self._start(target_id, profile)

        return self._run(instance_id, ManagedInstanceLifecycleKind.START, install)

    def resume_reviewed_bridge(
        self, instance_id: UUID, *, expected_generation: int
    ) -> ManagedRuntimeStatus:
        """Complete matching prepared trust synchronously under the lifecycle lease."""
        from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustError

        self._require_no_reset(instance_id)

        def resume(target_id: UUID, _profile: RuntimeExecutionProfile) -> None:
            if self._bridge_trust is None:
                raise ManagedBridgeTrustError("managed_bridge_candidate_unavailable")
            self._bridge_trust.resume_reviewed(
                target_id, expected_generation=expected_generation
            )

        return self._run(instance_id, ManagedInstanceLifecycleKind.START, resume)

    def _start(self, instance_id: UUID, profile: RuntimeExecutionProfile) -> None:
        container = self._require_container(instance_id)
        try:
            self._broker.start_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_state=container.runtime_state,
            )
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error
        except RemoteBrokerError as error:
            if error.code in {
                "docker_start_failed",
                "post_start_verification_failed",
                "managed_start_result_unverified",
                "managed_start_requires_reconciliation",
                "internal_error",
            }:
                raise LifecycleDispatchUnconfirmedError from error
            raise

    def _queue_readiness(
        self, instance_id: UUID, profile: RuntimeExecutionProfile
    ) -> None:

        def run(cancel_event: Event) -> ReadinessResult:
            coordinator = self._coordinator_factory(self._broker, profile)
            result = coordinator.wait_until_usable(
                str(instance_id), cancelled=cancel_event.is_set
            )
            if (
                result.usability_state.value == "usable"
                and not cancel_event.is_set()
                and self._bridge_trust is not None
            ):
                try:
                    self._bridge_trust.prepare_background(instance_id)
                except Exception:
                    # Optional resident capability must not make native Kodi
                    # operations unusable. No secret or raw error is logged.
                    logging.getLogger(__name__).warning(
                        "Managed resident Bridge preparation unavailable"
                    )
            return result

        # Status recovery may have queued a native-only probe while this START
        # owned mutation. Replace/serialize that runner so Bridge work is not lost.
        self._readiness.start(instance_id, run, replace_checking=True)

    def stop(
        self,
        instance_id: UUID,
        *,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        from kodi_lab.application.browser_remote_coordination import (
            BrowserRemoteCoordinationConflict,
            stop_dispatch_fence,
        )

        try:
            with stop_dispatch_fence(self._session_factory, instance_id):
                return self._run(
                    instance_id,
                    ManagedInstanceLifecycleKind.STOP,
                    self._stop,
                    operation_id=operation_id,
                    precondition=precondition,
                    on_claim=on_claim,
                    before_dispatch=before_dispatch,
                )
        except BrowserRemoteCoordinationConflict as error:
            raise LifecycleConflictError(
                "An input dispatcher is still active."
            ) from error

    def _stop(self, instance_id: UUID, _profile: RuntimeExecutionProfile) -> None:
        container = self._require_container(instance_id)
        self._readiness.cancel(instance_id)
        try:
            result = self._broker.stop_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_state=container.runtime_state,
            )
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error
        except RemoteBrokerError as error:
            if error.code in {
                "docker_stop_failed",
                "post_stop_verification_failed",
                "internal_error",
            }:
                raise LifecycleDispatchUnconfirmedError from error
            raise

        if (
            result.container_id != container.container_id
            or result.managed_instance_id != str(instance_id)
            or result.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise LifecycleDispatchUnconfirmedError
        from kodi_lab.application.browser_remote_coordination import (
            retire_stopped_remote,
        )

        retire_stopped_remote(
            self._session_factory, instance_id, container.container_id
        )
        from kodi_lab.application.novnc_control import HumanViewerOwnership

        HumanViewerOwnership(self._session_factory).retire_stopped(
            instance_id, container.container_id
        )

    def deprovision(
        self,
        instance_id: UUID,
        *,
        expected_scope: RuntimeRetirementScope | None = None,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
    ) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        if (
            expected_scope is not None
            and expected_scope.managed_instance_id != instance_id
        ):
            raise RuntimeRetirementReceiptError("runtime_retirement_scope_changed")

        def protect(session: Session) -> None:
            if expected_scope is not None:
                require_retirement_scope(session, expected_scope)
            if precondition is not None:
                precondition(session)

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            if expected_scope is not None:
                prepare_retirement_receipt(session, handle.operation_id, expected_scope)
            if on_claim is not None:
                on_claim(session, handle)

        def terminal(
            session: Session, operation: ManagedInstanceLifecycleOperation
        ) -> None:
            if expected_scope is not None:
                confirm_retirement_receipt(session, operation, expected_scope)

        return self._run(
            instance_id,
            ManagedInstanceLifecycleKind.DEPROVISION,
            lambda target_id, profile: self._deprovision(
                target_id, profile, expected_scope
            ),
            operation_id=operation_id,
            precondition=protect,
            on_claim=claimed,
            on_terminal=None if expected_scope is None else terminal,
        )

    def _deprovision(
        self,
        instance_id: UUID,
        profile: RuntimeExecutionProfile,
        scope: RuntimeRetirementScope | None = None,
    ) -> None:
        container = self._require_container(instance_id)
        if scope is not None and (
            scope.managed_instance_id != instance_id
            or scope.runtime_revision_id != profile.runtime_revision_id
            or scope.container_id != container.container_id
            or scope.image_id != container.image_id
            or container.runtime_revision_id != str(scope.runtime_revision_id)
        ):
            # This check is inside the leased action, before readiness/trust or
            # installed-evidence invalidation, not only in an earlier UI check.
            raise RuntimeRetirementReceiptError("runtime_retirement_scope_changed")
        if container.runtime_state is not ContainerRuntimeState.STOPPED:
            raise LifecycleConflictError("managed runtime must be stopped")
        self._readiness.clear(instance_id)
        with self._session_factory.begin() as session:
            invalidate_development_source_deployments(session, instance_id)
            invalidate_addon_zip_installation_evidence(session, instance_id)
            invalidate_managed_bridge_trust(session, instance_id)
        try:
            result = self._broker.deprovision_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_revision_id=str(profile.runtime_revision_id),
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if scope is not None and (
                not isinstance(result, DeprovisionManagedContainerResult)
                or result.managed_instance_id != str(scope.managed_instance_id)
                or result.runtime_revision_id != str(scope.runtime_revision_id)
                or result.removed_container_id != scope.container_id
            ):
                raise LifecycleDispatchUnconfirmedError
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error

    def _load_instance(self, instance_id: UUID) -> tuple[str, RuntimeExecutionProfile]:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            target = TargetRepository(session).get(instance_id)
            if target is None:
                raise ManagedInstanceNotFoundError(instance_id)
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            if profile is None:
                raise LifecycleConflictError("runtime execution profile is unavailable")
            return target.name, profile

    def _run(
        self,
        instance_id: UUID,
        kind: ManagedInstanceLifecycleKind,
        action: Callable[[UUID, RuntimeExecutionProfile], None],
        *,
        operation_id: UUID | None = None,
        precondition: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
        before_dispatch: Callable[[], None] | None = None,
        on_terminal: Callable[[Session, ManagedInstanceLifecycleOperation], None]
        | None = None,
    ) -> ManagedRuntimeStatus:
        instance_name, profile = self._load_instance(instance_id)

        def admission(session: Session) -> None:
            from kodi_lab.storage.retained_development import (
                require_runtime_reservations_idle,
            )

            if kind in {
                ManagedInstanceLifecycleKind.START,
                ManagedInstanceLifecycleKind.PROVISION,
            }:
                require_runtime_reservations_idle(session, str(instance_id))
            if kind is ManagedInstanceLifecycleKind.START:
                from kodi_lab.storage.retained_development import (
                    require_retained_container,
                    retained_targets,
                )

                if retained_targets(session):
                    require_retained_container(
                        session,
                        self._broker.managed_container_inventory().containers,
                        str(instance_id),
                    )
            if precondition is not None:
                precondition(session)

        handle = self._operations.begin(
            instance_id,
            instance_name=instance_name,
            kind=kind,
            prior_state=ManagedInstanceSemanticState.AMBIGUOUS,
            operation_id=operation_id,
            precondition=admission,
            on_claim=on_claim,
        )
        try:
            prior = self._observe(instance_id)
            self._operations.record_prior_state(handle, prior)
            if before_dispatch is not None:
                before_dispatch()
            action(instance_id, profile)
            result = self._status.get(instance_id)
            resulting_state = self._observe(instance_id)
        except Exception as error:
            try:
                resulting_state = self._observe(instance_id)
            except Exception:
                resulting_state = ManagedInstanceSemanticState.AMBIGUOUS
            self._terminalize_failure(
                handle, resulting_state, error, on_terminal=on_terminal
            )
            raise
        if self._is_expected_result(kind, resulting_state):
            self._operations.terminalize(
                handle,
                state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                resulting_state=resulting_state,
                on_terminal=on_terminal,
            )
        elif semantic_state_is_unambiguous(resulting_state):
            self._operations.terminalize(
                handle,
                state=ManagedInstanceLifecycleOperationState.FAILED,
                resulting_state=resulting_state,
                failure_reason="Lifecycle postcondition was not satisfied.",
                on_terminal=on_terminal,
            )
            raise LifecycleConflictError("lifecycle postcondition was not satisfied")
        else:
            self._operations.terminalize(
                handle,
                state=(ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED),
                resulting_state=resulting_state,
                failure_reason=(
                    "Lifecycle result is ambiguous; reconciliation is required."
                ),
                on_terminal=on_terminal,
            )
        if kind is ManagedInstanceLifecycleKind.START and self._is_expected_result(
            kind, resulting_state
        ):
            self._queue_readiness(instance_id, profile)
        return result

    @staticmethod
    def _is_expected_result(
        kind: ManagedInstanceLifecycleKind,
        state: ManagedInstanceSemanticState,
    ) -> bool:
        expected = {
            ManagedInstanceLifecycleKind.PROVISION: {
                ManagedInstanceSemanticState.STOPPED
            },
            ManagedInstanceLifecycleKind.START: {
                ManagedInstanceSemanticState.RUNNING_NOT_READY,
                ManagedInstanceSemanticState.RUNNING_USABLE,
            },
            ManagedInstanceLifecycleKind.STOP: {ManagedInstanceSemanticState.STOPPED},
            ManagedInstanceLifecycleKind.DEPROVISION: {
                ManagedInstanceSemanticState.UNPROVISIONED
            },
        }
        return state in expected[kind]

    def _observe(self, instance_id: UUID) -> ManagedInstanceSemanticState:
        return observe_lifecycle_semantic_state(
            self._broker, self._readiness, instance_id
        )

    def _terminalize_failure(
        self,
        handle: RunningLifecycleOperation,
        resulting_state: ManagedInstanceSemanticState,
        error: Exception,
        *,
        on_terminal: Callable[[Session, ManagedInstanceLifecycleOperation], None]
        | None = None,
    ) -> None:
        unconfirmed = isinstance(
            error,
            (LifecycleDispatchUnconfirmedError, ManagedBridgeMutationUnconfirmedError),
        )
        known = isinstance(error, BrokerUnavailableError) or (
            semantic_state_is_unambiguous(resulting_state) and not unconfirmed
        )
        self._operations.terminalize(
            handle,
            state=(
                ManagedInstanceLifecycleOperationState.FAILED
                if known
                else ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
            ),
            resulting_state=resulting_state,
            failure_reason=(
                "broker_runtime_dispatch_unconfirmed"
                if unconfirmed
                else self._safe_reason(error, reconciliation=not known)
            ),
            on_terminal=on_terminal,
        )

    @staticmethod
    def _safe_reason(error: Exception, *, reconciliation: bool) -> str:
        if reconciliation:
            return "Lifecycle outcome is ambiguous; reconciliation is required."
        if isinstance(error, LifecycleConflictError):
            return str(error)[:256]
        if isinstance(error, RemoteBrokerError):
            return error.safe_message[:256]
        if isinstance(error, ClientError):
            return "The runtime service is unavailable."
        return "Lifecycle operation failed."

    def _require_container(self, instance_id: UUID) -> ManagedContainerInventoryEntry:
        inventory = self._broker.managed_container_inventory()
        match = next(
            (
                item
                for item in inventory.containers
                if item.managed_instance_id == str(instance_id)
            ),
            None,
        )
        if match is None:
            raise LifecycleConflictError("managed runtime is not provisioned")
        return match

    def _require_no_reset(self, instance_id: UUID) -> None:
        if self._reset_operations is not None:
            self._reset_operations.require_no_active(instance_id)

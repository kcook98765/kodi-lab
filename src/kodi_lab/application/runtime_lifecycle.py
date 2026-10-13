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
    ManagedBridgeTrustService,
    invalidate_managed_bridge_trust,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reset_operations import ResetOperationCoordinator
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

    def provision(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(
            instance_id,
            ManagedInstanceLifecycleKind.PROVISION,
            self._provision,
        )

    def _provision(self, instance_id: UUID, _profile: RuntimeExecutionProfile) -> None:
        try:
            self._broker.provision_managed_container(
                managed_instance_id=str(instance_id)
            )
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error
        self._readiness.clear(instance_id)

    def start(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(instance_id, ManagedInstanceLifecycleKind.START, self._start)

    def install_reviewed_bridge(self, instance_id: UUID) -> ManagedRuntimeStatus:
        """Closed candidate installation under the existing lifecycle mutation lease."""
        self._require_no_reset(instance_id)

        def install(target_id: UUID, profile: RuntimeExecutionProfile) -> None:
            if (
                profile.kodi_release.canonical != "22.0-rc.1"
                or self._bridge_trust is None
            ):
                raise LifecycleConflictError("reviewed Bridge candidate unavailable")
            self._bridge_trust.stage_reviewed(target_id)
            self._stop(target_id, profile)
            self._start(target_id, profile)

        return self._run(instance_id, ManagedInstanceLifecycleKind.START, install)

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
                    self._bridge_trust.prepare(instance_id)
                except Exception:
                    # Optional resident capability must not make native Kodi
                    # operations unusable. No secret or raw error is logged.
                    logging.getLogger(__name__).warning(
                        "Managed resident Bridge preparation unavailable"
                    )
            return result

        self._readiness.start(instance_id, run)

    def stop(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(instance_id, ManagedInstanceLifecycleKind.STOP, self._stop)

    def _stop(self, instance_id: UUID, _profile: RuntimeExecutionProfile) -> None:
        container = self._require_container(instance_id)
        self._readiness.cancel(instance_id)
        try:
            self._broker.stop_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_state=container.runtime_state,
            )
        except (ClientTransportError, ClientProtocolError) as error:
            raise LifecycleDispatchUnconfirmedError from error

    def deprovision(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        return self._run(
            instance_id,
            ManagedInstanceLifecycleKind.DEPROVISION,
            self._deprovision,
        )

    def _deprovision(self, instance_id: UUID, profile: RuntimeExecutionProfile) -> None:
        container = self._require_container(instance_id)
        if container.runtime_state is not ContainerRuntimeState.STOPPED:
            raise LifecycleConflictError("managed runtime must be stopped")
        self._readiness.clear(instance_id)
        with self._session_factory.begin() as session:
            invalidate_development_source_deployments(session, instance_id)
            invalidate_addon_zip_installation_evidence(session, instance_id)
            invalidate_managed_bridge_trust(session, instance_id)
        try:
            self._broker.deprovision_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_revision_id=str(profile.runtime_revision_id),
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
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
    ) -> ManagedRuntimeStatus:
        instance_name, profile = self._load_instance(instance_id)
        handle = self._operations.begin(
            instance_id,
            instance_name=instance_name,
            kind=kind,
            prior_state=ManagedInstanceSemanticState.AMBIGUOUS,
        )
        try:
            prior = self._observe(instance_id)
            self._operations.record_prior_state(handle, prior)
            action(instance_id, profile)
            result = self._status.get(instance_id)
            resulting_state = self._observe(instance_id)
        except Exception as error:
            try:
                resulting_state = self._observe(instance_id)
            except Exception:
                resulting_state = ManagedInstanceSemanticState.AMBIGUOUS
            self._terminalize_failure(handle, resulting_state, error)
            raise
        if self._is_expected_result(kind, resulting_state):
            self._operations.terminalize(
                handle,
                state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                resulting_state=resulting_state,
            )
        elif semantic_state_is_unambiguous(resulting_state):
            self._operations.terminalize(
                handle,
                state=ManagedInstanceLifecycleOperationState.FAILED,
                resulting_state=resulting_state,
                failure_reason="Lifecycle postcondition was not satisfied.",
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
            )
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
    ) -> None:
        unconfirmed = isinstance(error, LifecycleDispatchUnconfirmedError)
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

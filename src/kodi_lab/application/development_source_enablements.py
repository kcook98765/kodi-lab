"""Enable one exactly copied DevelopmentSource add-on under fail-closed preflight."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_dependencies import (
    AddonDependencyState,
    declaration_mismatch,
    dependency_failure_code,
    evaluate_dependencies,
    first_blocking_dependency,
    is_builtin_dependency,
    reported_constraint,
)
from kodi_lab.application.addon_dependencies import (
    DependencyStatus as DependencyStatus,
)
from kodi_lab.application.mutations import MUTATION_LEASE_DURATION
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development import (
    DevelopmentSourceDependency,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)
from kodi_lab.development.launch import python_service_descriptor_eligible
from kodi_lab.domain import (
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceEnablementOperation,
    DevelopmentSourceEnablementOperationPhase,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    InstanceType,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RuntimeExecutionProfile,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonDetailsUnsupportedResponseError,
    KodiAddonEnablementAdapter,
    KodiAddonEnablementAmbiguousError,
    KodiAddonEnableRejectedError,
    KodiAddonInventoryAdapter,
    KodiAddonInventoryIdentityError,
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonInventoryUnsupportedResponseError,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    require_source_session_admission,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

TERMINAL_RETENTION_PER_INSTANCE = 20
_PYTHON_EXTENSION_POINTS = frozenset(
    {
        "xbmc.python.pluginsource",
        "xbmc.python.module",
        "xbmc.python.script",
        "xbmc.python.weather",
        "xbmc.python.lyrics",
        "xbmc.python.library",
    }
)


class DevelopmentSourceEnablementError(RuntimeError):
    code = "enablement_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DevelopmentSourceEnablementConflictError(DevelopmentSourceEnablementError):
    code = "enablement_conflict"


DevelopmentSourceDependencyState = AddonDependencyState


class DevelopmentSourceEnablementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: DevelopmentSourceEnablementOperationState
    result_classification: DevelopmentSourceEnablementResult | None
    prior_enabled: bool | None
    result_enabled: bool | None
    enable_performed: bool
    service_start_authorized: bool = False
    service_execution: Literal["not_observed"] = "not_observed"
    blocked_dependency_id: str | None = Field(default=None, max_length=128)
    blocked_dependency_reason: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    requested_at: datetime
    completed_at: datetime | None
    dependencies: list[DevelopmentSourceDependencyState] = Field(
        default_factory=list, max_length=256
    )


class _EnablementBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def verify_attached_development_source_destination(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult: ...


class _InventoryAdapter(Protocol):
    def get_inventory(self) -> KodiAddonInventoryResult: ...


class _EnableAdapter(Protocol):
    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord: ...

    def enable(self, addon_id: str) -> None: ...


type InventoryAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _InventoryAdapter
]
type EnableAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _EnableAdapter
]


@dataclass(frozen=True, slots=True)
class RunningDevelopmentSourceEnablement:
    operation_id: UUID
    instance_id: UUID
    token: str
    selected_generation: int | None = None
    selected_container_id: str | None = None

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class DevelopmentSourceEnablementOperationCoordinator:
    """Bind a typed running history row to one per-instance mutation lease."""

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
        source_id: UUID,
        *,
        source_logical_name: str,
        addon_id: str,
        addon_version: str,
        runtime_revision_id: UUID,
        service_start_authorized: bool = False,
    ) -> RunningDevelopmentSourceEnablement:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.DEVELOPMENT_SOURCE_ENABLEMENT
        )
        handle = RunningDevelopmentSourceEnablement(
            uuid4(), instance_id, secrets.token_urlsafe(32)
        )
        now = self._clock()
        operation = DevelopmentSourceEnablementOperation(
            id=handle.operation_id,
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            development_source_id=source_id,
            source_logical_name=source_logical_name,
            addon_id=addon_id,
            addon_version=addon_version,
            runtime_revision_id=runtime_revision_id,
            requested_at=now,
            started_at=now,
            state=DevelopmentSourceEnablementOperationState.RUNNING,
            phase=DevelopmentSourceEnablementOperationPhase.PREFLIGHT,
            service_start_authorized=service_start_authorized,
        )
        try:
            with self._session_factory.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                selected = require_source_session_admission(
                    session,
                    target_id=str(instance_id),
                    revision_id=str(runtime_revision_id),
                    source_id=str(source_id),
                    container_id=None,
                )
                handle = replace(
                    handle,
                    selected_generation=None
                    if selected is None
                    else selected.generation,
                    selected_container_id=None
                    if selected is None
                    else selected.container_id,
                )
                leases = ManagedInstanceMutationLeaseRepository(session)
                leases.reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT,
                    operation_id=handle.operation_id,
                )
                if not leases.claim(
                    instance_id,
                    operation_id=handle.operation_id,
                    token_digest=handle.token_digest,
                    now=now,
                    expires_at=now + MUTATION_LEASE_DURATION,
                ):
                    raise DevelopmentSourceEnablementConflictError(
                        "Enablement mutation authority could not be acquired."
                    )
                DevelopmentSourceEnablementOperationRepository(session).create(
                    operation
                )
        except IntegrityError as error:
            raise DevelopmentSourceEnablementConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return handle

    def _renew_owned(
        self, session: Session, handle: RunningDevelopmentSourceEnablement
    ) -> None:
        leases = ManagedInstanceMutationLeaseRepository(session)
        current = leases.current(handle.instance_id)
        now = self._clock()
        if (
            current is None
            or current.operation_kind
            is not ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT
            or not leases.renew(
                handle.instance_id,
                operation_id=handle.operation_id,
                token_digest=handle.token_digest,
                now=now,
                expires_at=now + MUTATION_LEASE_DURATION,
            )
        ):
            raise DevelopmentSourceEnablementConflictError(
                "Enablement mutation lease is no longer owned and current."
            )

    def set_preflight_state(
        self,
        handle: RunningDevelopmentSourceEnablement,
        *,
        prior_enabled: bool,
        precondition: Callable[[Session], None] | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._renew_owned(session, handle)
            if precondition is not None:
                precondition(session)
            if not DevelopmentSourceEnablementOperationRepository(
                session
            ).set_preflight_state(handle.operation_id, prior_enabled=prior_enabled):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement history is no longer running."
                )

    def mark_mutation_started(
        self,
        handle: RunningDevelopmentSourceEnablement,
        *,
        precondition: Callable[[Session], None] | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._renew_owned(session, handle)
            if precondition is not None:
                precondition(session)
            if not DevelopmentSourceEnablementOperationRepository(
                session
            ).mark_mutation_started(handle.operation_id):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement mutation marker could not be recorded."
                )

    def update_phase(
        self,
        handle: RunningDevelopmentSourceEnablement,
        phase: DevelopmentSourceEnablementOperationPhase,
    ) -> None:
        with self._session_factory.begin() as session:
            if not DevelopmentSourceEnablementOperationRepository(session).update_phase(
                handle.operation_id, phase
            ):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement history is no longer running."
                )

    def terminalize(
        self,
        handle: RunningDevelopmentSourceEnablement,
        *,
        state: DevelopmentSourceEnablementOperationState,
        result: DevelopmentSourceEnablementResult,
        result_enabled: bool | None,
        failure_reason: str | None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
    ) -> DevelopmentSourceEnablementOperation:
        now = self._clock()
        reconciliation = (
            state is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
        )
        with self._session_factory.begin() as session:
            operations = DevelopmentSourceEnablementOperationRepository(session)
            if not operations.terminalize(
                handle.operation_id,
                state=state,
                result_classification=result,
                result_enabled=result_enabled,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                failure_reason=failure_reason,
                reconciliation_required=reconciliation,
                now=now,
            ):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement operation is no longer running."
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                handle.instance_id,
                operation_id=handle.operation_id,
                token_digest=handle.token_digest,
            ):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement mutation lease is no longer owned."
                )
            operations.prune_terminal(
                handle.instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )
            operation = operations.get(handle.operation_id)
            assert operation is not None
            return operation


class DevelopmentSourceEnablementService:
    """Perform the complete copied-source enable-only sequence synchronously."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _EnablementBroker,
        validator: DevelopmentSourceValidator,
        *,
        coordinator: DevelopmentSourceEnablementOperationCoordinator | None = None,
        inventory_adapter_factory: InventoryAdapterFactory | None = None,
        enable_adapter_factory: EnableAdapterFactory | None = None,
        after_enable_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._validator = validator
        self._coordinator = (
            coordinator
            or DevelopmentSourceEnablementOperationCoordinator(session_factory)
        )
        self._inventory_factory = inventory_adapter_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._enable_factory = enable_adapter_factory or (
            lambda observation, profile: KodiAddonEnablementAdapter(
                observation, profile
            )
        )
        self._after_enable_hook = after_enable_hook

    def enable(
        self, instance_id: UUID, source_id: UUID, *, allow_service_start: bool = False
    ) -> DevelopmentSourceEnablementStatus:
        if type(allow_service_start) is not bool:
            raise DevelopmentSourceEnablementError(
                "Service start authorization must be Boolean.",
                code="enablement_not_allowed",
            )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            source = DevelopmentSourceRepository(session).get(source_id)
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                instance_id, source_id
            )
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                instance_id, source_id
            )
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if instance is None or source is None or attachment is None:
            raise DevelopmentSourceEnablementError(
                "Development source is not attached.", code="source_not_attached"
            )
        if instance.instance_type is not InstanceType.DISPOSABLE or profile is None:
            raise DevelopmentSourceEnablementError(
                "Enablement is unsupported for this instance.",
                code="enablement_unsupported",
            )
        addon_version = (
            source.observed_version
            if evidence is None
            else evidence.observed_source_version
        )
        handle = self._coordinator.begin(
            instance_id,
            source_id,
            source_logical_name=source.logical_name,
            addon_id=source.addon_id,
            addon_version=addon_version,
            runtime_revision_id=instance.runtime_revision_id,
            service_start_authorized=allow_service_start,
        )
        dependencies: list[DevelopmentSourceDependencyState] = []
        mutation_started = False
        try:
            try:
                source_observation = self._validator.validate(source.relative_locator)
            except DevelopmentSourceValidationError as error:
                return self._failed(handle, error.code.value, dependencies)
            if (
                source_observation.addon_id != source.addon_id
                or source_observation.observed_version != addon_version
            ):
                return self._failed(handle, "deployment_evidence_stale", dependencies)
            service_declared = (
                source_observation.has_auto_start_behavior
                or "xbmc.service" in source.extension_types
            )
            pure_service = tuple(
                point
                for point in source_observation.extension_types
                if point != "xbmc.addon.metadata"
            ) == ("xbmc.service",)
            if service_declared:
                if not allow_service_start:
                    return self._failed(
                        handle, "auto_start_addon_unsupported", dependencies
                    )
                if not python_service_descriptor_eligible(source_observation):
                    return self._failed(
                        handle, "service_descriptor_unsupported", dependencies
                    )
            if (
                source_observation.is_python_addon
                or service_declared
                or any(
                    point in _PYTHON_EXTENSION_POINTS
                    for point in source.extension_types
                )
            ) and profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS:
                return self._failed(
                    handle, "runtime_bytecode_policy_incompatible", dependencies
                )
            if service_declared and (
                profile.expected_kodi_version != "21.3"
                or profile.expected_version_tag != "stable"
                or profile.runtime_uid == 0
                or profile.runtime_gid == 0
            ):
                return self._failed(handle, "service_runtime_unqualified", dependencies)
            runtime = self._resolve_runtime(instance_id, instance.runtime_revision_id)
            if isinstance(runtime, str):
                return self._failed(handle, runtime, dependencies)
            observation, container_id = runtime

            def selected_precondition(session: Session) -> None:
                selected = require_source_session_admission(
                    session,
                    target_id=str(instance_id),
                    revision_id=str(instance.runtime_revision_id),
                    source_id=str(source_id),
                    container_id=container_id,
                )
                generation = None if selected is None else selected.generation
                if generation != handle.selected_generation or (
                    handle.selected_container_id is not None
                    and container_id != handle.selected_container_id
                ):
                    raise DevelopmentSessionConflict(
                        "development_session_identity_changed"
                    )

            with self._session_factory() as session:
                selected_precondition(session)
            evidence_code = self._validate_evidence(
                instance_id,
                source_id,
                source.addon_id,
                instance.runtime_revision_id,
                container_id,
            )
            if evidence_code is not None:
                return self._failed(handle, evidence_code, dependencies)
            try:
                verified = self._broker.verify_attached_development_source_destination(
                    managed_instance_id=str(instance_id),
                    development_source_id=str(source_id),
                    expected_container_id=container_id,
                )
            except ClientError:
                return self._failed(
                    handle, "deployment_verification_failed", dependencies
                )
            if (
                not verified.matched
                or verified.managed_instance_id != str(instance_id)
                or verified.development_source_id != str(source_id)
                or verified.runtime_revision_id != str(instance.runtime_revision_id)
                or verified.container_id != container_id
                or verified.addon_id != source.addon_id
                or verified.addon_version != addon_version
            ):
                return self._failed(
                    handle, "deployment_verification_failed", dependencies
                )
            if service_declared and not verified.python_service_descriptor_eligible:
                return self._failed(
                    handle, "service_descriptor_unsupported", dependencies
                )
            try:
                inventory = self._inventory_factory(
                    observation, profile
                ).get_inventory()
            except JsonRpcUnavailableError:
                return self._failed(handle, "jsonrpc_unavailable", dependencies)
            except (
                ValueError,
                KodiAddonInventoryIdentityError,
                KodiAddonInventoryUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                KodiAddonInventoryLimitExceededError,
            ):
                return self._failed(handle, "unsupported_response", dependencies)
            inventory_by_id = {item.addon_id: item for item in inventory.items}
            recognized = inventory_by_id.get(source.addon_id)
            if recognized is None:
                return self._failed(handle, "addon_not_recognized", dependencies)
            if recognized.version != addon_version:
                return self._failed(handle, "addon_version_mismatch", dependencies)
            adapter = self._enable_factory(observation, profile)
            try:
                details = adapter.get_addon_details(source.addon_id)
            except JsonRpcUnavailableError:
                return self._failed(handle, "jsonrpc_unavailable", dependencies)
            except (
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                ValueError,
            ):
                return self._failed(handle, "unsupported_response", dependencies)
            if details.addon_id != source.addon_id:
                return self._failed(handle, "addon_not_recognized", dependencies)
            if details.version != addon_version:
                return self._failed(handle, "addon_version_mismatch", dependencies)
            if details.kodi_type == "xbmc.service" or service_declared:
                native_item = inventory_by_id.get(source.addon_id)
                if not (
                    allow_service_start
                    and pure_service
                    and details.kodi_type == "xbmc.service"
                    and native_item is not None
                    and native_item.kodi_type == "xbmc.service"
                ):
                    return self._failed(
                        handle, "auto_start_addon_unsupported", dependencies
                    )
            if (
                details.kodi_type in _PYTHON_EXTENSION_POINTS
                and profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
            ):
                return self._failed(
                    handle, "runtime_bytecode_policy_incompatible", dependencies
                )
            mismatch = declaration_mismatch(
                source_observation.dependencies, details.dependencies
            )
            if mismatch is not None:
                dependencies = [mismatch]
                return self._dependency_blocked(
                    handle, "dependency_state_unknown", mismatch, dependencies
                )
            dependencies = evaluate_dependencies(
                source_observation.dependencies,
                inventory_by_id,
                kodi_release=profile.kodi_release.canonical,
            )
            blocked = first_blocking_dependency(dependencies)
            if blocked is not None:
                code = dependency_failure_code(blocked.status)
                return self._dependency_blocked(handle, code, blocked, dependencies)
            self._coordinator.set_preflight_state(
                handle,
                prior_enabled=details.enabled,
                precondition=selected_precondition,
            )
            if details.enabled:
                operation = self._coordinator.terminalize(
                    handle,
                    state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
                    result=DevelopmentSourceEnablementResult.ALREADY_ENABLED,
                    result_enabled=True,
                    failure_reason=None,
                )
                return self._status(operation, dependencies)
            self._coordinator.mark_mutation_started(
                handle,
                precondition=selected_precondition,
            )
            mutation_started = True
            try:
                adapter.enable(source.addon_id)
            except KodiAddonEnableRejectedError:
                return self._failed(
                    handle,
                    "kodi_enable_rejected",
                    dependencies,
                    result=DevelopmentSourceEnablementResult.REJECTED,
                    result_enabled=False,
                )
            except KodiAddonEnablementAmbiguousError:
                return self._reconciliation(handle, dependencies)
            if self._after_enable_hook is not None:
                self._after_enable_hook()
            self._coordinator.update_phase(
                handle, DevelopmentSourceEnablementOperationPhase.POSTCONDITION
            )
            try:
                after = adapter.get_addon_details(source.addon_id)
            except (
                JsonRpcUnavailableError,
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
            ):
                return self._reconciliation(handle, dependencies)
            if after.addon_id != source.addon_id or after.version != addon_version:
                return self._reconciliation(handle, dependencies)
            if service_declared and after.kodi_type != "xbmc.service":
                return self._reconciliation(handle, dependencies)
            if not after.enabled:
                return self._failed(
                    handle,
                    "kodi_enable_rejected",
                    dependencies,
                    result=DevelopmentSourceEnablementResult.FAILED,
                    result_enabled=False,
                )
            operation = self._coordinator.terminalize(
                handle,
                state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
                result=DevelopmentSourceEnablementResult.ENABLED,
                result_enabled=True,
                failure_reason=None,
            )
            return self._status(operation, dependencies)
        except DevelopmentSessionConflict as error:
            if mutation_started:
                return self._reconciliation(handle, dependencies)
            return self._failed(handle, error.code, dependencies)
        except Exception:
            if mutation_started:
                return self._reconciliation(handle, dependencies)
            return self._failed(handle, "enablement_failed", dependencies)

    def _resolve_runtime(
        self, instance_id: UUID, runtime_revision_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, str] | str:
        try:
            items = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
        except ClientError:
            return "kodi_not_ready"
        if not items:
            return "instance_not_running"
        if (
            len(items) != 1
            or items[0].runtime_state is not ContainerRuntimeState.RUNNING
        ):
            return "instance_not_running"
        container = items[0]
        if container.health_state is not ContainerHealthState.HEALTHY:
            return "kodi_not_ready"
        try:
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
            )
        except ClientError:
            return "kodi_not_ready"
        if (
            observation.managed_instance_id != str(instance_id)
            or observation.runtime_revision_id != str(runtime_revision_id)
            or observation.container_id != container.container_id
            or observation.runtime_state is not ContainerRuntimeState.RUNNING
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            return "kodi_not_ready"
        return observation, container.container_id

    def _validate_evidence(
        self,
        instance_id: UUID,
        source_id: UUID,
        addon_id: str,
        runtime_revision_id: UUID,
        container_id: str,
    ) -> str | None:
        with self._session_factory() as session:
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                instance_id, source_id
            )
            if evidence is None:
                return "source_not_deployed"
            if evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE:
                return "deployment_evidence_stale"
            if evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN:
                return "deployment_evidence_unknown"
            operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                evidence.last_successful_operation_id
            )
        if (
            evidence.addon_id != addon_id
            or evidence.runtime_revision_id != runtime_revision_id
            or evidence.trusted_container_id != container_id
            or operation is None
            or operation.state
            is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
            or operation.managed_instance_id != instance_id
            or operation.development_source_id != source_id
            or operation.runtime_revision_id != runtime_revision_id
            or operation.expected_container_id != container_id
        ):
            return "deployment_evidence_stale"
        return None

    @staticmethod
    def _declarations_match(
        source: tuple[DevelopmentSourceDependency, ...],
        reported: tuple[KodiReportedAddonDependency, ...],
    ) -> DevelopmentSourceDependencyState | None:
        return declaration_mismatch(source, reported)

    @staticmethod
    def _reported_constraint(value: str | None) -> str | None:
        """Map Kodi's exact neutral version sentinel to no declaration constraint."""

        return reported_constraint(value)

    @staticmethod
    def _preflight_dependencies(
        declarations: tuple[DevelopmentSourceDependency, ...],
        inventory: Mapping[str, KodiAddonInventoryRecord],
        *,
        kodi_release: str | None = None,
    ) -> list[DevelopmentSourceDependencyState]:
        return evaluate_dependencies(declarations, inventory, kodi_release=kodi_release)

    @staticmethod
    def _is_builtin(dependency_id: str) -> bool:
        return is_builtin_dependency(dependency_id)

    def _dependency_blocked(
        self,
        handle: RunningDevelopmentSourceEnablement,
        code: str,
        blocked: DevelopmentSourceDependencyState,
        dependencies: list[DevelopmentSourceDependencyState],
    ) -> DevelopmentSourceEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=DevelopmentSourceEnablementOperationState.FAILED,
            result=DevelopmentSourceEnablementResult.DEPENDENCY_BLOCKED,
            result_enabled=False,
            failure_reason=code,
            blocked_dependency_id=blocked.dependency_id,
            blocked_dependency_reason=code,
        )
        return self._status(operation, dependencies)

    def _failed(
        self,
        handle: RunningDevelopmentSourceEnablement,
        code: str,
        dependencies: list[DevelopmentSourceDependencyState],
        *,
        result: DevelopmentSourceEnablementResult = (
            DevelopmentSourceEnablementResult.FAILED
        ),
        result_enabled: bool | None = None,
    ) -> DevelopmentSourceEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=DevelopmentSourceEnablementOperationState.FAILED,
            result=result,
            result_enabled=result_enabled,
            failure_reason=code,
        )
        return self._status(operation, dependencies)

    def _reconciliation(
        self,
        handle: RunningDevelopmentSourceEnablement,
        dependencies: list[DevelopmentSourceDependencyState],
    ) -> DevelopmentSourceEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=(DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED),
            result=DevelopmentSourceEnablementResult.RECONCILIATION_REQUIRED,
            result_enabled=None,
            failure_reason="enablement_reconciliation_required",
        )
        return self._status(operation, dependencies)

    @staticmethod
    def _status(
        operation: DevelopmentSourceEnablementOperation,
        dependencies: list[DevelopmentSourceDependencyState],
    ) -> DevelopmentSourceEnablementStatus:
        return DevelopmentSourceEnablementStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            managed_instance_id=operation.managed_instance_id,
            development_source_id=operation.development_source_id,
            addon_id=operation.addon_id,
            addon_version=operation.addon_version,
            state=operation.state,
            result_classification=operation.result_classification,
            prior_enabled=operation.prior_enabled,
            result_enabled=operation.result_enabled,
            enable_performed=operation.enable_performed,
            service_start_authorized=operation.service_start_authorized,
            blocked_dependency_id=operation.blocked_dependency_id,
            blocked_dependency_reason=operation.blocked_dependency_reason,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
            requested_at=operation.requested_at,
            completed_at=operation.completed_at,
            dependencies=dependencies,
        )

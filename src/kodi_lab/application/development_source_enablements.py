"""Enable one exactly copied DevelopmentSource add-on under fail-closed preflight."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
_BUILTIN_DEPENDENCY_IDS = frozenset(
    {"xbmc.gui", "xbmc.json", "xbmc.metadata", "xbmc.python"}
)
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


class DependencyStatus(StrEnum):
    PRESENT_ENABLED = "present_enabled"
    MISSING = "missing"
    DISABLED = "disabled"
    OPTIONAL_MISSING = "optional_missing"
    VERSION_UNVERIFIED = "version_unverified"
    UNKNOWN = "unknown"


class DevelopmentSourceDependencyState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dependency_id: str = Field(max_length=128)
    declared_version: str | None = Field(default=None, max_length=128)
    declared_minversion: str | None = Field(default=None, max_length=128)
    optional: bool
    recognized: bool
    enabled: bool | None
    reported_version: str | None = Field(default=None, max_length=128)
    status: DependencyStatus


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
        )
        try:
            with self._session_factory.begin() as session:
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

    def set_preflight_state(
        self, handle: RunningDevelopmentSourceEnablement, *, prior_enabled: bool
    ) -> None:
        with self._session_factory.begin() as session:
            if not DevelopmentSourceEnablementOperationRepository(
                session
            ).set_preflight_state(handle.operation_id, prior_enabled=prior_enabled):
                raise DevelopmentSourceEnablementConflictError(
                    "Enablement history is no longer running."
                )

    def mark_mutation_started(self, handle: RunningDevelopmentSourceEnablement) -> None:
        with self._session_factory.begin() as session:
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
        self, instance_id: UUID, source_id: UUID
    ) -> DevelopmentSourceEnablementStatus:
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
            if source_observation.has_auto_start_behavior or "xbmc.service" in (
                source.extension_types
            ):
                return self._failed(
                    handle, "auto_start_addon_unsupported", dependencies
                )
            if (
                source_observation.is_python_addon
                or any(
                    point in _PYTHON_EXTENSION_POINTS
                    for point in source.extension_types
                )
            ) and profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS:
                return self._failed(
                    handle, "runtime_bytecode_policy_incompatible", dependencies
                )
            runtime = self._resolve_runtime(instance_id, instance.runtime_revision_id)
            if isinstance(runtime, str):
                return self._failed(handle, runtime, dependencies)
            observation, container_id = runtime
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
                or verified.runtime_revision_id != str(instance.runtime_revision_id)
                or verified.container_id != container_id
                or verified.addon_id != source.addon_id
                or verified.addon_version != addon_version
            ):
                return self._failed(
                    handle, "deployment_verification_failed", dependencies
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
            if details.kodi_type == "xbmc.service":
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
            mismatch = self._declarations_match(
                source_observation.dependencies, details.dependencies
            )
            if mismatch is not None:
                dependencies = [mismatch]
                return self._dependency_blocked(
                    handle, "dependency_state_unknown", mismatch, dependencies
                )
            dependencies = self._preflight_dependencies(
                source_observation.dependencies, inventory_by_id
            )
            blocked = next(
                (
                    item
                    for item in dependencies
                    if not item.optional
                    and item.status
                    in {
                        DependencyStatus.MISSING,
                        DependencyStatus.DISABLED,
                        DependencyStatus.VERSION_UNVERIFIED,
                        DependencyStatus.UNKNOWN,
                    }
                ),
                None,
            )
            if blocked is not None:
                code = {
                    DependencyStatus.MISSING: "dependency_missing",
                    DependencyStatus.DISABLED: "dependency_disabled",
                    DependencyStatus.VERSION_UNVERIFIED: (
                        "dependency_version_unverified"
                    ),
                    DependencyStatus.UNKNOWN: "dependency_state_unknown",
                }[blocked.status]
                return self._dependency_blocked(handle, code, blocked, dependencies)
            self._coordinator.set_preflight_state(handle, prior_enabled=details.enabled)
            if details.enabled:
                operation = self._coordinator.terminalize(
                    handle,
                    state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
                    result=DevelopmentSourceEnablementResult.ALREADY_ENABLED,
                    result_enabled=True,
                    failure_reason=None,
                )
                return self._status(operation, dependencies)
            self._coordinator.mark_mutation_started(handle)
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
        by_id = {item.dependency_id: item for item in reported}
        source_ids = {item.dependency_id for item in source}
        if source_ids != set(by_id):
            dependency_id = sorted(source_ids ^ set(by_id))[0]
            declared = next(
                (item for item in source if item.dependency_id == dependency_id), None
            )
            return DevelopmentSourceDependencyState(
                dependency_id=dependency_id,
                declared_version=None if declared is None else declared.version,
                declared_minversion=None if declared is None else declared.minversion,
                optional=False if declared is None else declared.optional,
                recognized=False,
                enabled=None,
                reported_version=(
                    None
                    if dependency_id not in by_id
                    else by_id[dependency_id].version or by_id[dependency_id].minversion
                ),
                status=DependencyStatus.UNKNOWN,
            )
        for declared in source:
            actual = by_id[declared.dependency_id]
            if (
                declared.optional != actual.optional
                or declared.version
                != DevelopmentSourceEnablementService._reported_constraint(
                    actual.version
                )
                or declared.minversion
                != DevelopmentSourceEnablementService._reported_constraint(
                    actual.minversion
                )
            ):
                return DevelopmentSourceDependencyState(
                    dependency_id=declared.dependency_id,
                    declared_version=declared.version,
                    declared_minversion=declared.minversion,
                    optional=declared.optional,
                    recognized=True,
                    enabled=None,
                    reported_version=actual.version or actual.minversion,
                    status=DependencyStatus.UNKNOWN,
                )
        return None

    @staticmethod
    def _reported_constraint(value: str | None) -> str | None:
        """Map Kodi's exact neutral version sentinel to no declaration constraint."""

        return None if value in (None, "0.0.0") else value

    @staticmethod
    def _preflight_dependencies(
        declarations: tuple[DevelopmentSourceDependency, ...],
        inventory: Mapping[str, KodiAddonInventoryRecord],
    ) -> list[DevelopmentSourceDependencyState]:
        result: list[DevelopmentSourceDependencyState] = []
        for declared in declarations:
            record = inventory.get(declared.dependency_id)
            constraint = declared.version or declared.minversion
            if record is None and DevelopmentSourceEnablementService._is_builtin(
                declared.dependency_id
            ):
                result.append(
                    DevelopmentSourceDependencyState(
                        dependency_id=declared.dependency_id,
                        declared_version=declared.version,
                        declared_minversion=declared.minversion,
                        optional=declared.optional,
                        recognized=True,
                        enabled=True,
                        reported_version=None,
                        status=(
                            DependencyStatus.PRESENT_ENABLED
                            if constraint is None
                            else DependencyStatus.VERSION_UNVERIFIED
                        ),
                    )
                )
                continue
            if record is None:
                result.append(
                    DevelopmentSourceDependencyState(
                        dependency_id=declared.dependency_id,
                        declared_version=declared.version,
                        declared_minversion=declared.minversion,
                        optional=declared.optional,
                        recognized=False,
                        enabled=None,
                        reported_version=None,
                        status=(
                            DependencyStatus.OPTIONAL_MISSING
                            if declared.optional
                            else DependencyStatus.MISSING
                        ),
                    )
                )
                continue
            enabled = bool(record.enabled)
            reported_version = str(record.version)
            if not enabled:
                status = DependencyStatus.DISABLED
            elif constraint is not None:
                status = DependencyStatus.VERSION_UNVERIFIED
            else:
                status = DependencyStatus.PRESENT_ENABLED
            result.append(
                DevelopmentSourceDependencyState(
                    dependency_id=declared.dependency_id,
                    declared_version=declared.version,
                    declared_minversion=declared.minversion,
                    optional=declared.optional,
                    recognized=True,
                    enabled=enabled,
                    reported_version=reported_version,
                    status=status,
                )
            )
        return result

    @staticmethod
    def _is_builtin(dependency_id: str) -> bool:
        return dependency_id in _BUILTIN_DEPENDENCY_IDS or dependency_id.startswith(
            "kodi.binary.global."
        )

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
            blocked_dependency_id=operation.blocked_dependency_id,
            blocked_dependency_reason=operation.blocked_dependency_reason,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
            requested_at=operation.requested_at,
            completed_at=operation.completed_at,
            dependencies=dependencies,
        )

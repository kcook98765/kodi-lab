"""ZIP-specific enable-only authority rooted in verified installation evidence."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
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
    VerifyAddonZipEnablementDestinationResult,
)
from kodi_lab.domain import (
    AddonZipEnablementOperation,
    AddonZipEnablementOperationPhase,
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipExtensionClassification,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
    AddonZipOrigin,
    AddonZipPayloadVerificationOutcome,
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
    KodiAddonInventoryResult,
    KodiAddonInventoryUnsupportedResponseError,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

EXPECTED_KODI_TYPE = "xbmc.python.pluginsource"
TERMINAL_RETENTION_PER_INSTANCE = 20


class AddonZipEnablementError(RuntimeError):
    code = "addon_zip_enablement_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AddonZipEnablementConflictError(AddonZipEnablementError):
    code = "addon_zip_enablement_conflict"


class AddonZipEnablementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    artifact_id: UUID
    installation_operation_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: AddonZipEnablementOperationState
    result: AddonZipEnablementResult | None
    payload_verification: AddonZipPayloadVerificationOutcome
    prior_enabled: bool | None
    result_enabled: bool | None
    enable_performed: bool
    blocked_dependency_id: str | None = Field(default=None, max_length=128)
    blocked_dependency_reason: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    requested_at: datetime
    completed_at: datetime | None
    dependencies: list[AddonDependencyState] = Field(
        default_factory=list, max_length=256
    )


class _EnablementBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def verify_addon_zip_enablement_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        enablement_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipEnablementDestinationResult: ...


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
class RunningAddonZipEnablement:
    operation_id: UUID
    instance_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AddonZipEnablementOperationCoordinator:
    """Bind ZIP-specific history to one exact per-instance mutation lease."""

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
        artifact_id: UUID,
        installation_operation_id: UUID,
        *,
        addon_id: str,
        addon_version: str,
        runtime_revision_id: UUID,
    ) -> RunningAddonZipEnablement:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.ADDON_ZIP_ENABLEMENT
        )
        handle = RunningAddonZipEnablement(
            operation_id=uuid4(),
            instance_id=instance_id,
            token=secrets.token_urlsafe(32),
        )
        now = self._clock()
        operation = AddonZipEnablementOperation(
            id=handle.operation_id,
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            artifact_id=artifact_id,
            installation_operation_id=installation_operation_id,
            addon_id=addon_id,
            addon_version=addon_version,
            runtime_revision_id=runtime_revision_id,
            requested_at=now,
            started_at=now,
            state=AddonZipEnablementOperationState.RUNNING,
            phase=AddonZipEnablementOperationPhase.PREFLIGHT,
        )
        try:
            with self._session_factory.begin() as session:
                leases = ManagedInstanceMutationLeaseRepository(session)
                leases.reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.ADDON_ZIP_ENABLEMENT,
                    operation_id=handle.operation_id,
                )
                if not leases.claim(
                    instance_id,
                    operation_id=handle.operation_id,
                    token_digest=handle.token_digest,
                    now=now,
                    expires_at=now + MUTATION_LEASE_DURATION,
                ):
                    raise AddonZipEnablementConflictError(
                        "ZIP enablement authority could not be acquired."
                    )
                AddonZipEnablementOperationRepository(session).create(operation)
        except IntegrityError as error:
            raise AddonZipEnablementConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return handle

    def update(self, handle: RunningAddonZipEnablement, **values: object) -> None:
        with self._session_factory.begin() as session:
            if not AddonZipEnablementOperationRepository(session).update_fields(
                handle.operation_id, **values
            ):
                raise AddonZipEnablementConflictError(
                    "ZIP enablement history is no longer running."
                )

    def terminalize(
        self,
        handle: RunningAddonZipEnablement,
        *,
        state: AddonZipEnablementOperationState,
        result: AddonZipEnablementResult,
        payload_verification: AddonZipPayloadVerificationOutcome,
        result_enabled: bool | None,
        failure_reason: str | None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
        evidence_confidence: AddonZipInstallationConfidence | None = None,
    ) -> AddonZipEnablementOperation:
        now = self._clock()
        reconciliation = (
            state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
        )
        with self._session_factory.begin() as session:
            operations = AddonZipEnablementOperationRepository(session)
            if not operations.terminalize(
                handle.operation_id,
                state=state,
                result_classification=result,
                payload_verification=payload_verification,
                result_enabled=result_enabled,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                failure_reason=failure_reason,
                reconciliation_required=reconciliation,
                now=now,
            ):
                raise AddonZipEnablementConflictError(
                    "ZIP enablement operation is no longer running."
                )
            if evidence_confidence is not None:
                operation = operations.get(handle.operation_id)
                assert operation is not None
                AddonZipInstallationEvidenceRepository(
                    session
                ).mark_artifact_confidence(
                    handle.instance_id,
                    operation.artifact_id,
                    evidence_confidence,
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                handle.instance_id,
                operation_id=handle.operation_id,
                token_digest=handle.token_digest,
            ):
                raise AddonZipEnablementConflictError(
                    "ZIP enablement lease is no longer owned."
                )
            operations.prune_terminal(
                handle.instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )
            operation = operations.get(handle.operation_id)
            assert operation is not None
            return operation


class AddonZipEnablementService:
    """Perform the complete ZIP-specific enable-only sequence synchronously."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _EnablementBroker,
        *,
        coordinator: AddonZipEnablementOperationCoordinator | None = None,
        inventory_adapter_factory: InventoryAdapterFactory | None = None,
        enable_adapter_factory: EnableAdapterFactory | None = None,
        after_enable_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator or AddonZipEnablementOperationCoordinator(
            session_factory
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

    def enable(self, instance_id: UUID, artifact_id: UUID) -> AddonZipEnablementStatus:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            artifact = AddonZipArtifactRepository(session).get(artifact_id)
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
            evidence = (
                None
                if artifact is None
                else AddonZipInstallationEvidenceRepository(session).get(
                    instance_id, artifact.addon_id
                )
            )
            installation = (
                None
                if evidence is None or evidence.installation_operation_id is None
                else AddonZipInstallationOperationRepository(session).get(
                    evidence.installation_operation_id
                )
            )
        if instance is None or artifact is None:
            raise AddonZipEnablementError(
                "Managed instance or accepted ZIP artifact is unavailable.",
                code="zip_enablement_authority_invalid",
            )
        if artifact.origin is not AddonZipOrigin.UPLOADED_ZIP:
            raise AddonZipEnablementError(
                "Repository artifacts require repository-specific "
                "enablement authority.",
                code="zip_enablement_authority_invalid",
            )
        if evidence is None or evidence.artifact_id != artifact_id:
            raise AddonZipEnablementError(
                "The uploaded ZIP has no successful installation evidence.",
                code="zip_not_installed",
            )
        if installation is None:
            raise AddonZipEnablementError(
                "ZIP installation history is unavailable.",
                code="zip_installation_history_invalid",
            )
        handle = self._coordinator.begin(
            instance_id,
            artifact_id,
            installation.id,
            addon_id=artifact.addon_id,
            addon_version=artifact.addon_version,
            runtime_revision_id=instance.runtime_revision_id,
        )
        dependencies: list[AddonDependencyState] = []
        payload = AddonZipPayloadVerificationOutcome.NOT_CHECKED
        mutation_started = False
        try:
            if (
                instance.instance_type is not InstanceType.DISPOSABLE
                or profile is None
                or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
                or artifact.extension_classification
                is not AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
            ):
                return self._failed(
                    handle, "zip_enablement_unsupported", payload, dependencies
                )
            authority_failure = self._validate_installation_authority(
                instance_id,
                artifact_id,
                instance.runtime_revision_id,
                artifact.addon_id,
                artifact.addon_version,
                evidence,
                installation,
            )
            if authority_failure is not None:
                return self._failed(handle, authority_failure, payload, dependencies)
            runtime = self._resolve_runtime(instance_id, instance.runtime_revision_id)
            if isinstance(runtime, str):
                return self._failed(handle, runtime, payload, dependencies)
            observation, container_id = runtime
            self._coordinator.update(handle, expected_container_id=container_id)
            if evidence.trusted_container_id != container_id:
                return self._failed(
                    handle, "zip_installation_lineage_stale", payload, dependencies
                )
            self._coordinator.update(
                handle, phase=AddonZipEnablementOperationPhase.PAYLOAD_VERIFICATION
            )
            try:
                verified = self._broker.verify_addon_zip_enablement_destination(
                    managed_instance_id=str(instance_id),
                    artifact_id=str(artifact_id),
                    enablement_operation_id=str(handle.operation_id),
                    expected_container_id=container_id,
                )
            except ClientError:
                payload = AddonZipPayloadVerificationOutcome.UNKNOWN
                return self._failed(
                    handle,
                    "payload_verification_unknown",
                    payload,
                    dependencies,
                    evidence_confidence=AddonZipInstallationConfidence.UNKNOWN,
                )
            if (
                verified.managed_instance_id != str(instance_id)
                or verified.artifact_id != str(artifact_id)
                or verified.enablement_operation_id != str(handle.operation_id)
                or verified.runtime_revision_id != str(instance.runtime_revision_id)
                or verified.container_id != container_id
                or verified.addon_id != artifact.addon_id
                or verified.addon_version != artifact.addon_version
            ):
                payload = AddonZipPayloadVerificationOutcome.UNKNOWN
                return self._failed(
                    handle,
                    "payload_verification_unknown",
                    payload,
                    dependencies,
                    evidence_confidence=AddonZipInstallationConfidence.UNKNOWN,
                )
            if not verified.matched:
                payload = AddonZipPayloadVerificationOutcome.CHANGED
                return self._failed(
                    handle,
                    "installed_payload_changed",
                    payload,
                    dependencies,
                    evidence_confidence=AddonZipInstallationConfidence.STALE,
                )
            payload = AddonZipPayloadVerificationOutcome.MATCHED
            self._coordinator.update(handle, payload_verification=payload)
            try:
                inventory = self._inventory_factory(
                    observation, profile
                ).get_inventory()
            except JsonRpcUnavailableError:
                return self._failed(
                    handle, "jsonrpc_unavailable", payload, dependencies
                )
            except (
                ValueError,
                KodiAddonInventoryIdentityError,
                KodiAddonInventoryUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                KodiAddonInventoryLimitExceededError,
            ):
                return self._failed(
                    handle, "unsupported_response", payload, dependencies
                )
            inventory_by_id = {item.addon_id: item for item in inventory.items}
            recognized = inventory_by_id.get(artifact.addon_id)
            if recognized is None:
                return self._failed(
                    handle, "addon_not_recognized", payload, dependencies
                )
            if recognized.version != artifact.addon_version:
                return self._failed(
                    handle, "addon_version_mismatch", payload, dependencies
                )
            if recognized.kodi_type != EXPECTED_KODI_TYPE:
                return self._failed(
                    handle, "addon_type_mismatch", payload, dependencies
                )
            adapter = self._enable_factory(observation, profile)
            try:
                details = adapter.get_addon_details(artifact.addon_id)
            except JsonRpcUnavailableError:
                return self._failed(
                    handle, "jsonrpc_unavailable", payload, dependencies
                )
            except (
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                ValueError,
            ):
                return self._failed(
                    handle, "unsupported_response", payload, dependencies
                )
            if details.addon_id != artifact.addon_id:
                return self._failed(
                    handle, "addon_not_recognized", payload, dependencies
                )
            if details.version != artifact.addon_version:
                return self._failed(
                    handle, "addon_version_mismatch", payload, dependencies
                )
            if details.kodi_type != EXPECTED_KODI_TYPE:
                return self._failed(
                    handle, "addon_type_mismatch", payload, dependencies
                )
            mismatch = declaration_mismatch(artifact.dependencies, details.dependencies)
            if mismatch is not None:
                dependencies = [mismatch]
                return self._dependency_blocked(
                    handle,
                    "dependency_state_unknown",
                    mismatch,
                    payload,
                    dependencies,
                )
            dependencies = evaluate_dependencies(
                artifact.dependencies,
                inventory_by_id,
                kodi_release=profile.kodi_release.canonical,
            )
            blocked = first_blocking_dependency(dependencies)
            if blocked is not None:
                return self._dependency_blocked(
                    handle,
                    dependency_failure_code(blocked.status),
                    blocked,
                    payload,
                    dependencies,
                )
            self._coordinator.update(handle, prior_enabled=details.enabled)
            if details.enabled:
                operation = self._coordinator.terminalize(
                    handle,
                    state=AddonZipEnablementOperationState.SUCCEEDED,
                    result=AddonZipEnablementResult.ALREADY_ENABLED,
                    payload_verification=payload,
                    result_enabled=True,
                    failure_reason=None,
                )
                return self._status(operation, dependencies)
            self._coordinator.update(
                handle,
                phase=AddonZipEnablementOperationPhase.MUTATION,
                enable_performed=True,
            )
            mutation_started = True
            try:
                adapter.enable(artifact.addon_id)
            except KodiAddonEnableRejectedError:
                return self._failed(
                    handle,
                    "kodi_enable_rejected",
                    payload,
                    dependencies,
                    result=AddonZipEnablementResult.REJECTED,
                    result_enabled=False,
                )
            except KodiAddonEnablementAmbiguousError:
                return self._reconciliation(handle, payload, dependencies)
            if self._after_enable_hook is not None:
                self._after_enable_hook()
            self._coordinator.update(
                handle, phase=AddonZipEnablementOperationPhase.POSTCONDITION
            )
            try:
                after = adapter.get_addon_details(artifact.addon_id)
            except (
                JsonRpcUnavailableError,
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
            ):
                return self._reconciliation(handle, payload, dependencies)
            if (
                after.addon_id != artifact.addon_id
                or after.version != artifact.addon_version
                or after.kodi_type != EXPECTED_KODI_TYPE
            ):
                return self._reconciliation(handle, payload, dependencies)
            if not after.enabled:
                return self._failed(
                    handle,
                    "kodi_enable_rejected",
                    payload,
                    dependencies,
                    result_enabled=False,
                )
            operation = self._coordinator.terminalize(
                handle,
                state=AddonZipEnablementOperationState.SUCCEEDED,
                result=AddonZipEnablementResult.ENABLED,
                payload_verification=payload,
                result_enabled=True,
                failure_reason=None,
            )
            return self._status(operation, dependencies)
        except Exception:
            if mutation_started:
                return self._reconciliation(handle, payload, dependencies)
            return self._failed(
                handle, "addon_zip_enablement_failed", payload, dependencies
            )

    @staticmethod
    def _validate_installation_authority(
        instance_id: UUID,
        artifact_id: UUID,
        runtime_revision_id: UUID,
        addon_id: str,
        addon_version: str,
        evidence: AddonZipInstallationEvidence,
        installation: AddonZipInstallationOperation,
    ) -> str | None:
        if evidence.confidence is AddonZipInstallationConfidence.STALE:
            return "zip_installation_evidence_stale"
        if evidence.confidence is AddonZipInstallationConfidence.UNKNOWN:
            return "zip_installation_evidence_unknown"
        if (
            evidence.result
            is not AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED
            or evidence.managed_instance_id != instance_id
            or evidence.original_managed_instance_id != instance_id
            or evidence.artifact_id != artifact_id
            or evidence.runtime_revision_id != runtime_revision_id
            or evidence.addon_id != addon_id
            or evidence.addon_version != addon_version
            or installation.state is not AddonZipInstallationOperationState.SUCCEEDED
            or installation.original_managed_instance_id != instance_id
            or installation.managed_instance_id != instance_id
            or installation.artifact_id != artifact_id
            or installation.id != evidence.installation_operation_id
            or installation.runtime_revision_id != runtime_revision_id
            or installation.addon_id != addon_id
            or installation.addon_version != addon_version
            or installation.expected_container_id != evidence.trusted_container_id
            or not installation.stopped_readback_matched
            or not installation.final_readback_matched
            or installation.recognition_enabled is not False
        ):
            return "zip_installation_lineage_stale"
        return None

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
        if not items or len(items) != 1:
            return "instance_not_running"
        container = items[0]
        if container.runtime_state is not ContainerRuntimeState.RUNNING:
            return "instance_not_running"
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

    def _dependency_blocked(
        self,
        handle: RunningAddonZipEnablement,
        code: str,
        blocked: AddonDependencyState,
        payload: AddonZipPayloadVerificationOutcome,
        dependencies: list[AddonDependencyState],
    ) -> AddonZipEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=AddonZipEnablementOperationState.FAILED,
            result=AddonZipEnablementResult.DEPENDENCY_BLOCKED,
            payload_verification=payload,
            result_enabled=False,
            failure_reason=code,
            blocked_dependency_id=blocked.dependency_id,
            blocked_dependency_reason=code,
        )
        return self._status(operation, dependencies)

    def _failed(
        self,
        handle: RunningAddonZipEnablement,
        code: str,
        payload: AddonZipPayloadVerificationOutcome,
        dependencies: list[AddonDependencyState],
        *,
        result: AddonZipEnablementResult = AddonZipEnablementResult.FAILED,
        result_enabled: bool | None = None,
        evidence_confidence: AddonZipInstallationConfidence | None = None,
    ) -> AddonZipEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=AddonZipEnablementOperationState.FAILED,
            result=result,
            payload_verification=payload,
            result_enabled=result_enabled,
            failure_reason=code,
            evidence_confidence=evidence_confidence,
        )
        return self._status(operation, dependencies)

    def _reconciliation(
        self,
        handle: RunningAddonZipEnablement,
        payload: AddonZipPayloadVerificationOutcome,
        dependencies: list[AddonDependencyState],
    ) -> AddonZipEnablementStatus:
        operation = self._coordinator.terminalize(
            handle,
            state=AddonZipEnablementOperationState.RECONCILIATION_REQUIRED,
            result=AddonZipEnablementResult.RECONCILIATION_REQUIRED,
            payload_verification=payload,
            result_enabled=None,
            failure_reason="zip_enablement_reconciliation_required",
            evidence_confidence=AddonZipInstallationConfidence.UNKNOWN,
        )
        return self._status(operation, dependencies)

    @staticmethod
    def _status(
        operation: AddonZipEnablementOperation,
        dependencies: list[AddonDependencyState],
    ) -> AddonZipEnablementStatus:
        return AddonZipEnablementStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            managed_instance_id=operation.managed_instance_id,
            artifact_id=operation.artifact_id,
            installation_operation_id=operation.installation_operation_id,
            addon_id=operation.addon_id,
            addon_version=operation.addon_version,
            state=operation.state,
            result=operation.result_classification,
            payload_verification=operation.payload_verification,
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

"""Durable installed ZIP Python video-plugin smoke-test workflow."""

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

from kodi_lab.application.addon_dependencies import (
    AddonDependencyState,
    declaration_mismatch,
    dependency_failure_code,
    evaluate_dependencies,
    first_blocking_dependency,
)
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
    VerifyAddonZipSmokeDestinationResult,
)
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipExtensionClassification,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
    AddonZipOrigin,
    AddonZipSmokeTestExecutionResult,
    AddonZipSmokeTestOperation,
    AddonZipSmokeTestOperationPhase,
    AddonZipSmokeTestOperationState,
    AddonZipSmokeTestOverallResult,
    AddonZipSmokeTestPostProvenance,
    InstanceType,
    ManagedInstance,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RuntimeExecutionProfile,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonDetailsUnsupportedResponseError,
    KodiAddonEnablementAdapter,
    KodiAddonInventoryAdapter,
    KodiAddonInventoryIdentityError,
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryResult,
    KodiAddonInventoryUnsupportedResponseError,
    KodiAddonSmokeAdapter,
    KodiAddonSmokeAmbiguousError,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    AddonZipSmokeTestOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

SMOKE_OPERATION_BOUND = timedelta(seconds=90)
TERMINAL_RETENTION_PER_INSTANCE = 20


class AddonZipSmokeTestError(RuntimeError):
    code = "addon_zip_smoke_test_failed"


class AddonZipSmokeTestConflictError(AddonZipSmokeTestError):
    code = "addon_zip_smoke_test_conflict"


class AddonZipSmokeTestNotFoundError(AddonZipSmokeTestError):
    code = "addon_zip_smoke_test_not_found"


class AddonZipSmokeTestStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    artifact_id: UUID
    installation_operation_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: AddonZipSmokeTestOperationState
    phase: AddonZipSmokeTestOperationPhase
    overall_result: AddonZipSmokeTestOverallResult | None
    execution_result: AddonZipSmokeTestExecutionResult
    post_execution_provenance: AddonZipSmokeTestPostProvenance
    invocation_duration_ms: int | None = Field(default=None, ge=0, le=90_000)
    returned_item_count: int | None = Field(default=None, ge=0, le=1)
    blocked_dependency_id: str | None = Field(default=None, max_length=128)
    blocked_dependency_reason: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class _SmokeBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def verify_addon_zip_smoke_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        smoke_operation_id: str,
        expected_container_id: str,
    ) -> VerifyAddonZipSmokeDestinationResult: ...


class _InventoryAdapter(Protocol):
    def get_inventory(self) -> KodiAddonInventoryResult: ...


class _DetailsAdapter(Protocol):
    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord: ...


class _SmokeAdapter(Protocol):
    def invoke(self, addon_id: str) -> KodiAddonSmokeResult: ...


type InventoryAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _InventoryAdapter
]
type DetailsAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _DetailsAdapter
]
type SmokeAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _SmokeAdapter
]


@dataclass(frozen=True, slots=True)
class AddonZipSmokeTestLease:
    operation_id: UUID
    instance_id: UUID
    artifact_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AddonZipSmokeTestOperationCoordinator:
    """Reserve only the exact instance mutation lease for durable smoke work."""

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

    def queue(self, instance_id: UUID, artifact_id: UUID) -> AddonZipSmokeTestStatus:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.ADDON_ZIP_SMOKE_TEST
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            artifact = AddonZipArtifactRepository(session).get(artifact_id)
            evidence = (
                None
                if artifact is None
                else AddonZipInstallationEvidenceRepository(session).get(
                    instance_id, artifact.addon_id
                )
            )
        if instance is None or artifact is None or evidence is None:
            raise AddonZipSmokeTestNotFoundError(
                "Managed instance, ZIP artifact, or installation evidence is missing."
            )
        if artifact.origin is not AddonZipOrigin.UPLOADED_ZIP:
            raise AddonZipSmokeTestNotFoundError(
                "Repository artifacts require repository-specific smoke authority."
            )
        if evidence.artifact_id != artifact_id:
            raise AddonZipSmokeTestNotFoundError(
                "The accepted ZIP artifact has no matching installation evidence."
            )
        operation = AddonZipSmokeTestOperation(
            id=uuid4(),
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            artifact_id=artifact_id,
            installation_operation_id=evidence.installation_operation_id,
            addon_id=artifact.addon_id,
            addon_version=artifact.addon_version,
            runtime_revision_id=instance.runtime_revision_id,
            requested_at=self._clock(),
            state=AddonZipSmokeTestOperationState.QUEUED,
            phase=AddonZipSmokeTestOperationPhase.ACQUIRE,
        )
        try:
            with self._session_factory.begin() as session:
                operations = AddonZipSmokeTestOperationRepository(session)
                if operations.active_for_instance(instance_id) is not None:
                    raise AddonZipSmokeTestConflictError(
                        "A smoke test is already active for this instance."
                    )
                operations.create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.ADDON_ZIP_SMOKE_TEST,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise AddonZipSmokeTestConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return self.to_status(operation)

    def acquire(self, operation_id: UUID) -> AddonZipSmokeTestLease | None:
        with self._session_factory() as session:
            operation = AddonZipSmokeTestOperationRepository(session).get(operation_id)
        if operation is None or operation.managed_instance_id is None:
            return None
        lease = AddonZipSmokeTestLease(
            operation.id,
            operation.managed_instance_id,
            operation.artifact_id,
            secrets.token_urlsafe(32),
        )
        now = self._clock()
        expires = now + SMOKE_OPERATION_BOUND
        with self._session_factory.begin() as session:
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            acquired = AddonZipSmokeTestOperationRepository(session).acquire(
                lease.operation_id,
                token_digest=lease.token_digest,
                expected_container_id=None,
                now=now,
                lease_expires_at=expires,
            )
            if not (mutation and acquired):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test authority could not be acquired."
                )
        return lease

    def set_expected_container(
        self, lease: AddonZipSmokeTestLease, container_id: str
    ) -> None:
        with self._session_factory.begin() as session:
            if not AddonZipSmokeTestOperationRepository(session).set_expected_container(
                lease.operation_id,
                token_digest=lease.token_digest,
                expected_container_id=container_id,
                now=self._clock(),
            ):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test runtime precondition changed."
                )

    def update_phase(
        self,
        lease: AddonZipSmokeTestLease,
        phase: AddonZipSmokeTestOperationPhase,
    ) -> None:
        with self._session_factory.begin() as session:
            if not AddonZipSmokeTestOperationRepository(session).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=self._clock(),
            ):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test authority expired or changed."
                )

    def mark_invocation_dispatched(self, lease: AddonZipSmokeTestLease) -> None:
        with self._session_factory.begin() as session:
            if not AddonZipSmokeTestOperationRepository(
                session
            ).mark_invocation_dispatched(
                lease.operation_id,
                token_digest=lease.token_digest,
                now=self._clock(),
            ):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test dispatch marker could not be recorded."
                )

    def finish(
        self,
        lease: AddonZipSmokeTestLease,
        *,
        state: AddonZipSmokeTestOperationState,
        overall_result: AddonZipSmokeTestOverallResult | None,
        execution_result: AddonZipSmokeTestExecutionResult,
        post_provenance: AddonZipSmokeTestPostProvenance,
        duration_ms: int | None = None,
        item_count: int | None = None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
        failure_reason: str | None = None,
        confidence: AddonZipInstallationConfidence | None = None,
    ) -> AddonZipSmokeTestOperation:
        now = self._clock()
        reconciliation = (
            state is AddonZipSmokeTestOperationState.RECONCILIATION_REQUIRED
        )
        with self._session_factory.begin() as session:
            if confidence is not None:
                AddonZipInstallationEvidenceRepository(
                    session
                ).mark_artifact_confidence(
                    lease.instance_id, lease.artifact_id, confidence
                )
            operations = AddonZipSmokeTestOperationRepository(session)
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.token_digest,
                state=state,
                overall_result=overall_result,
                execution_result=execution_result,
                post_execution_provenance=post_provenance,
                invocation_duration_ms=duration_ms,
                returned_item_count=item_count,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                failure_reason=failure_reason,
                reconciliation_required=reconciliation,
                now=now,
            ):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test operation ownership was lost."
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise AddonZipSmokeTestConflictError(
                    "Smoke-test mutation lease cleanup could not be proven."
                )
            operations.prune_terminal(
                lease.instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )
            operation = operations.get(lease.operation_id)
            assert operation is not None
            return operation

    def get(self, operation_id: UUID) -> AddonZipSmokeTestOperation | None:
        with self._session_factory() as session:
            return AddonZipSmokeTestOperationRepository(session).get(operation_id)

    def queued(self) -> list[AddonZipSmokeTestOperation]:
        with self._session_factory() as session:
            return AddonZipSmokeTestOperationRepository(session).list_queued()

    @staticmethod
    def to_status(
        operation: AddonZipSmokeTestOperation,
    ) -> AddonZipSmokeTestStatus:
        return AddonZipSmokeTestStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            managed_instance_id=operation.managed_instance_id,
            artifact_id=operation.artifact_id,
            installation_operation_id=operation.installation_operation_id,
            addon_id=operation.addon_id,
            addon_version=operation.addon_version,
            state=operation.state,
            phase=operation.phase,
            overall_result=operation.overall_result,
            execution_result=operation.execution_result,
            post_execution_provenance=operation.post_execution_provenance,
            invocation_duration_ms=operation.invocation_duration_ms,
            returned_item_count=operation.returned_item_count,
            blocked_dependency_id=operation.blocked_dependency_id,
            blocked_dependency_reason=operation.blocked_dependency_reason,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
        )


class AddonZipSmokeTestService:
    """Perform one fixed ZIP plugin-directory invocation with exact provenance."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _SmokeBroker,
        coordinator: AddonZipSmokeTestOperationCoordinator,
        lease: AddonZipSmokeTestLease,
        *,
        inventory_adapter_factory: InventoryAdapterFactory | None = None,
        details_adapter_factory: DetailsAdapterFactory | None = None,
        smoke_adapter_factory: SmokeAdapterFactory | None = None,
        after_dispatch_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease
        self._inventory_factory = inventory_adapter_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._details_factory = details_adapter_factory or (
            lambda observation, profile: KodiAddonEnablementAdapter(
                observation, profile
            )
        )
        self._smoke_factory = smoke_adapter_factory or (
            lambda observation, profile: KodiAddonSmokeAdapter(observation, profile)
        )
        self._after_dispatch_hook = after_dispatch_hook

    def run(self) -> AddonZipSmokeTestStatus:
        dispatched = False
        execution = AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED
        duration: int | None = None
        count: int | None = None
        try:
            context = self._load_context()
            if isinstance(context, str):
                return self._failed(context)
            instance, artifact, evidence, installation, profile = context
            self._coordinator.update_phase(
                self._lease, AddonZipSmokeTestOperationPhase.VALIDATE
            )
            if (
                artifact.extension_classification
                is not AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
            ):
                return self._failed(
                    "unsupported_addon_type",
                    overall=AddonZipSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            if profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS:
                return self._failed("runtime_bytecode_policy_incompatible")
            runtime = self._resolve_runtime(instance.runtime_revision_id)
            if isinstance(runtime, str):
                return self._failed(runtime)
            observation, container_id = runtime
            self._coordinator.set_expected_container(self._lease, container_id)
            authority_error = self._validate_evidence(
                artifact,
                evidence,
                installation,
                instance.runtime_revision_id,
                container_id,
            )
            if authority_error is not None:
                return self._failed(authority_error)
            try:
                inventory = self._inventory_factory(
                    observation, profile
                ).get_inventory()
            except JsonRpcUnavailableError:
                return self._failed("jsonrpc_unavailable")
            except (
                ValueError,
                KodiAddonInventoryIdentityError,
                KodiAddonInventoryUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                KodiAddonInventoryLimitExceededError,
            ):
                return self._failed("unsupported_response")
            inventory_by_id = {item.addon_id: item for item in inventory.items}
            recognized = inventory_by_id.get(artifact.addon_id)
            if recognized is None:
                return self._failed("addon_not_recognized")
            if recognized.version != artifact.addon_version:
                return self._failed("addon_version_mismatch")
            if not recognized.enabled:
                return self._failed("addon_disabled")
            if recognized.kodi_type != "xbmc.python.pluginsource":
                return self._failed(
                    "unsupported_addon_type",
                    overall=AddonZipSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            details_adapter = self._details_factory(observation, profile)
            try:
                details = details_adapter.get_addon_details(artifact.addon_id)
            except JsonRpcUnavailableError:
                return self._failed("jsonrpc_unavailable")
            except (
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                ValueError,
            ):
                return self._failed("unsupported_response")
            if (
                details.addon_id != artifact.addon_id
                or details.version != artifact.addon_version
            ):
                return self._failed("addon_version_mismatch")
            if not details.enabled:
                return self._failed("addon_disabled")
            if details.kodi_type != "xbmc.python.pluginsource":
                return self._failed(
                    "unsupported_addon_type",
                    overall=AddonZipSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            mismatch = declaration_mismatch(artifact.dependencies, details.dependencies)
            if mismatch is not None:
                return self._dependency_blocked("dependency_state_unknown", mismatch)
            dependencies = evaluate_dependencies(artifact.dependencies, inventory_by_id)
            blocked = first_blocking_dependency(dependencies)
            if blocked is not None:
                return self._dependency_blocked(
                    dependency_failure_code(blocked.status), blocked
                )
            self._coordinator.update_phase(
                self._lease, AddonZipSmokeTestOperationPhase.VERIFY_PRE_EXECUTION
            )
            pre = self._verify_destination(container_id)
            if pre == "changed":
                return self._failed(
                    "installed_tree_changed",
                    overall=AddonZipSmokeTestOverallResult.INSTALLED_TREE_CHANGED,
                    confidence=AddonZipInstallationConfidence.STALE,
                )
            if pre != "matched":
                return self._failed(
                    "installed_tree_verification_unknown",
                    overall=AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    confidence=AddonZipInstallationConfidence.UNKNOWN,
                )
            self._coordinator.mark_invocation_dispatched(self._lease)
            dispatched = True
            if self._after_dispatch_hook is not None:
                self._after_dispatch_hook()
            adapter = self._smoke_factory(observation, profile)
            try:
                invocation = adapter.invoke(artifact.addon_id)
                execution = AddonZipSmokeTestExecutionResult.DIRECTORY_RESULT
                duration = invocation.duration_ms
                count = invocation.item_count
            except KodiAddonSmokeJsonRpcError as error:
                execution = AddonZipSmokeTestExecutionResult.JSONRPC_ERROR
                duration = error.duration_ms
            except KodiAddonSmokeTimeoutError as error:
                return self._reconciliation(
                    AddonZipSmokeTestOverallResult.TIMEOUT,
                    AddonZipSmokeTestExecutionResult.TIMEOUT,
                    duration_ms=error.duration_ms,
                    reason="smoke_invocation_timeout",
                )
            except KodiAddonSmokeAmbiguousError:
                return self._reconciliation(
                    AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    AddonZipSmokeTestExecutionResult.AMBIGUOUS,
                    reason="smoke_invocation_ambiguous",
                )
            self._coordinator.update_phase(
                self._lease, AddonZipSmokeTestOperationPhase.VERIFY_POST_EXECUTION
            )
            post = self._verify_destination(container_id)
            if post == "changed":
                return self._terminal(
                    state=AddonZipSmokeTestOperationState.FAILED,
                    overall=AddonZipSmokeTestOverallResult.INSTALLED_TREE_CHANGED,
                    execution=execution,
                    post=AddonZipSmokeTestPostProvenance.CHANGED,
                    duration_ms=duration,
                    item_count=count,
                    reason="installed_tree_changed",
                    confidence=AddonZipInstallationConfidence.STALE,
                )
            if post != "matched":
                return self._reconciliation(
                    AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    execution,
                    duration_ms=duration,
                    item_count=count,
                    reason="post_execution_verification_unknown",
                )
            if execution is AddonZipSmokeTestExecutionResult.JSONRPC_ERROR:
                return self._terminal(
                    state=AddonZipSmokeTestOperationState.FAILED,
                    overall=AddonZipSmokeTestOverallResult.EXECUTION_FAILED,
                    execution=execution,
                    post=AddonZipSmokeTestPostProvenance.MATCHED,
                    duration_ms=duration,
                    reason="kodi_plugin_execution_failed",
                )
            return self._terminal(
                state=AddonZipSmokeTestOperationState.SUCCEEDED,
                overall=AddonZipSmokeTestOverallResult.PASSED,
                execution=execution,
                post=AddonZipSmokeTestPostProvenance.MATCHED,
                duration_ms=duration,
                item_count=count,
            )
        except Exception:
            if dispatched:
                return self._reconciliation(
                    AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    AddonZipSmokeTestExecutionResult.AMBIGUOUS,
                    duration_ms=duration,
                    item_count=count,
                    reason="smoke_invocation_ambiguous",
                )
            return self._failed("addon_zip_smoke_test_failed")

    def _load_context(
        self,
    ) -> (
        tuple[
            ManagedInstance,
            AddonZipArtifact,
            AddonZipInstallationEvidence,
            AddonZipInstallationOperation,
            RuntimeExecutionProfile,
        ]
        | str
    ):
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(self._lease.instance_id)
            artifact = AddonZipArtifactRepository(session).get(self._lease.artifact_id)
            evidence = (
                None
                if artifact is None
                else AddonZipInstallationEvidenceRepository(session).get(
                    self._lease.instance_id, artifact.addon_id
                )
            )
            installation = (
                None
                if evidence is None
                else AddonZipInstallationOperationRepository(session).get(
                    evidence.installation_operation_id
                )
            )
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if (
            instance is None
            or artifact is None
            or evidence is None
            or installation is None
        ):
            return "zip_smoke_authority_invalid"
        if instance.instance_type is not InstanceType.DISPOSABLE or profile is None:
            return "smoke_test_unsupported"
        if instance.runtime_revision_id != self._operation().runtime_revision_id:
            return "runtime_revision_mismatch"
        return instance, artifact, evidence, installation, profile

    def _resolve_runtime(
        self, revision_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, str] | str:
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(self._lease.instance_id)
            ]
        except ClientError:
            return "kodi_not_ready"
        if len(matches) != 1:
            return "instance_not_running"
        container = matches[0]
        if container.runtime_state is not ContainerRuntimeState.RUNNING:
            return "instance_not_running"
        if container.health_state is not ContainerHealthState.HEALTHY:
            return "kodi_not_ready"
        try:
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=container.container_id,
            )
        except ClientError:
            return "kodi_not_ready"
        if (
            observation.managed_instance_id != str(self._lease.instance_id)
            or observation.runtime_revision_id != str(revision_id)
            or observation.container_id != container.container_id
            or observation.runtime_state is not ContainerRuntimeState.RUNNING
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            return "kodi_not_ready"
        return observation, container.container_id

    def _validate_evidence(
        self,
        artifact: AddonZipArtifact,
        evidence: AddonZipInstallationEvidence,
        installation: AddonZipInstallationOperation,
        revision_id: UUID,
        container_id: str,
    ) -> str | None:
        operation = self._operation()
        if evidence.confidence is AddonZipInstallationConfidence.STALE:
            return "zip_installation_evidence_stale"
        if evidence.confidence is AddonZipInstallationConfidence.UNKNOWN:
            return "zip_installation_evidence_unknown"
        if (
            evidence.confidence
            is not AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
            or evidence.result
            is not AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED
            or evidence.managed_instance_id != self._lease.instance_id
            or evidence.original_managed_instance_id != self._lease.instance_id
            or evidence.artifact_id != artifact.id
            or evidence.installation_operation_id != operation.installation_operation_id
            or evidence.runtime_revision_id != revision_id
            or evidence.trusted_container_id != container_id
            or evidence.addon_id != artifact.addon_id
            or evidence.addon_version != artifact.addon_version
            or evidence.payload_manifest_digest != artifact.payload_manifest_digest
            or installation.id != evidence.installation_operation_id
            or installation.state is not AddonZipInstallationOperationState.SUCCEEDED
            or installation.original_managed_instance_id != self._lease.instance_id
            or installation.managed_instance_id != self._lease.instance_id
            or installation.artifact_id != artifact.id
            or installation.runtime_revision_id != revision_id
            or installation.expected_container_id != container_id
            or installation.addon_id != artifact.addon_id
            or installation.addon_version != artifact.addon_version
            or not installation.application_started
            or not installation.stopped_readback_matched
            or not installation.final_readback_matched
            or installation.recognition_enabled is not False
        ):
            return "zip_installation_lineage_stale"
        return None

    def _verify_destination(self, container_id: str) -> str:
        operation = self._operation()
        try:
            result = self._broker.verify_addon_zip_smoke_destination(
                managed_instance_id=str(self._lease.instance_id),
                artifact_id=str(self._lease.artifact_id),
                smoke_operation_id=str(self._lease.operation_id),
                expected_container_id=container_id,
            )
        except ClientError:
            return "unknown"
        if (
            result.managed_instance_id != str(self._lease.instance_id)
            or result.artifact_id != str(self._lease.artifact_id)
            or result.smoke_operation_id != str(self._lease.operation_id)
            or result.runtime_revision_id != str(operation.runtime_revision_id)
            or result.container_id != container_id
            or result.addon_id != operation.addon_id
            or result.addon_version != operation.addon_version
        ):
            return "unknown"
        return "matched" if result.matched else "changed"

    def _operation(self) -> AddonZipSmokeTestOperation:
        operation = self._coordinator.get(self._lease.operation_id)
        if operation is None:
            raise AddonZipSmokeTestConflictError("Smoke-test history no longer exists.")
        return operation

    def _dependency_blocked(
        self, code: str, blocked: AddonDependencyState
    ) -> AddonZipSmokeTestStatus:
        return self._terminal(
            state=AddonZipSmokeTestOperationState.FAILED,
            overall=AddonZipSmokeTestOverallResult.DEPENDENCY_BLOCKED,
            execution=AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED,
            post=AddonZipSmokeTestPostProvenance.NOT_CHECKED,
            blocked_dependency_id=blocked.dependency_id,
            blocked_dependency_reason=code,
            reason=code,
        )

    def _failed(
        self,
        reason: str,
        *,
        overall: AddonZipSmokeTestOverallResult | None = None,
        confidence: AddonZipInstallationConfidence | None = None,
    ) -> AddonZipSmokeTestStatus:
        return self._terminal(
            state=AddonZipSmokeTestOperationState.FAILED,
            overall=overall,
            execution=AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED,
            post=AddonZipSmokeTestPostProvenance.NOT_CHECKED,
            reason=reason,
            confidence=confidence,
        )

    def _reconciliation(
        self,
        overall: AddonZipSmokeTestOverallResult,
        execution: AddonZipSmokeTestExecutionResult,
        *,
        duration_ms: int | None = None,
        item_count: int | None = None,
        reason: str,
    ) -> AddonZipSmokeTestStatus:
        return self._terminal(
            state=AddonZipSmokeTestOperationState.RECONCILIATION_REQUIRED,
            overall=overall,
            execution=execution,
            post=AddonZipSmokeTestPostProvenance.UNKNOWN,
            duration_ms=duration_ms,
            item_count=item_count,
            reason=reason,
            confidence=AddonZipInstallationConfidence.UNKNOWN,
        )

    def _terminal(
        self,
        *,
        state: AddonZipSmokeTestOperationState,
        overall: AddonZipSmokeTestOverallResult | None,
        execution: AddonZipSmokeTestExecutionResult,
        post: AddonZipSmokeTestPostProvenance,
        duration_ms: int | None = None,
        item_count: int | None = None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
        reason: str | None = None,
        confidence: AddonZipInstallationConfidence | None = None,
    ) -> AddonZipSmokeTestStatus:
        operation = self._coordinator.finish(
            self._lease,
            state=state,
            overall_result=overall,
            execution_result=execution,
            post_provenance=post,
            duration_ms=duration_ms,
            item_count=item_count,
            blocked_dependency_id=blocked_dependency_id,
            blocked_dependency_reason=blocked_dependency_reason,
            failure_reason=reason,
            confidence=confidence,
        )
        return self._coordinator.to_status(operation)


type AddonZipSmokeTestServiceFactory = Callable[
    [AddonZipSmokeTestLease], AddonZipSmokeTestService
]


class AddonZipSmokeTestExecutor:
    """Run durable smoke operations outside normal Web request lifetimes."""

    def __init__(
        self,
        coordinator: AddonZipSmokeTestOperationCoordinator,
        service_factory: AddonZipSmokeTestServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="addon-smoke")
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise RuntimeError("smoke-test executor is closed")
            self._pool.submit(self._run, operation_id)

    def _run(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
            if lease is None:
                return
            self._service_factory(lease).run()
        except Exception:
            return

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        with suppress(Exception):
            self._pool.shutdown(wait=True, cancel_futures=False)

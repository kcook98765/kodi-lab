"""Durable copied Python video-plugin smoke-test workflow."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_dependencies import (
    dependency_failure_code,
    first_blocking_dependency,
)
from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceDependencyState,
    DevelopmentSourceEnablementService,
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
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
    copied_video_plugin_library,
)
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceSmokeTestExecutionResult,
    DevelopmentSourceSmokeTestOperation,
    DevelopmentSourceSmokeTestOperationPhase,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    DevelopmentSourceSmokeTestPostProvenance,
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
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    require_source_session_admission,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

SMOKE_OPERATION_BOUND = timedelta(seconds=90)
TERMINAL_RETENTION_PER_INSTANCE = 20


class DevelopmentSourceSmokeTestError(RuntimeError):
    code = "development_source_smoke_test_failed"


class DevelopmentSourceSmokeTestConflictError(DevelopmentSourceSmokeTestError):
    code = "development_source_smoke_test_conflict"


class DevelopmentSourceSmokeTestNotFoundError(DevelopmentSourceSmokeTestError):
    code = "development_source_smoke_test_not_found"


class DevelopmentSourceSmokeTestStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: DevelopmentSourceSmokeTestOperationState
    phase: DevelopmentSourceSmokeTestOperationPhase
    overall_result: DevelopmentSourceSmokeTestOverallResult | None
    execution_result: DevelopmentSourceSmokeTestExecutionResult
    post_execution_provenance: DevelopmentSourceSmokeTestPostProvenance
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

    def verify_attached_development_source_destination(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult: ...


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
class DevelopmentSourceSmokeTestLease:
    operation_id: UUID
    instance_id: UUID
    source_id: UUID
    token: str
    selected_generation: int | None = None
    expected_container_id: str | None = None

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class DevelopmentSourceSmokeTestOperationCoordinator:
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

    def queue(
        self, instance_id: UUID, source_id: UUID
    ) -> DevelopmentSourceSmokeTestStatus:
        self._stale.sweep_instance(instance_id)
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.DEVELOPMENT_SOURCE_SMOKE_TEST
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            source = DevelopmentSourceRepository(session).get(source_id)
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                instance_id, source_id
            )
        if instance is None or source is None:
            raise DevelopmentSourceSmokeTestNotFoundError(
                "Managed instance or DevelopmentSource does not exist."
            )
        operation = DevelopmentSourceSmokeTestOperation(
            id=uuid4(),
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            development_source_id=source_id,
            source_logical_name=source.logical_name,
            addon_id=source.addon_id,
            addon_version=(
                source.observed_version
                if evidence is None
                else evidence.observed_source_version
            ),
            runtime_revision_id=instance.runtime_revision_id,
            requested_at=self._clock(),
            state=DevelopmentSourceSmokeTestOperationState.QUEUED,
            phase=DevelopmentSourceSmokeTestOperationPhase.ACQUIRE,
        )
        try:
            with self._session_factory.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                selected = require_source_session_admission(
                    session,
                    target_id=str(instance_id),
                    revision_id=str(instance.runtime_revision_id),
                    source_id=str(source_id),
                    container_id=None,
                )
                if selected is not None and selected.container_id is None:
                    raise DevelopmentSessionConflict(
                        "development_session_requires_review"
                    )
                operation = replace(
                    operation,
                    expected_container_id=None
                    if selected is None
                    else selected.container_id,
                )
                operations = DevelopmentSourceSmokeTestOperationRepository(session)
                if operations.active_for_instance(instance_id) is not None:
                    raise DevelopmentSourceSmokeTestConflictError(
                        "A smoke test is already active for this instance."
                    )
                operations.create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_SMOKE_TEST,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise DevelopmentSourceSmokeTestConflictError(
                "Another mutation is active for this managed instance."
            ) from error
        return self.to_status(operation)

    def acquire(self, operation_id: UUID) -> DevelopmentSourceSmokeTestLease | None:
        with self._session_factory() as session:
            operation = DevelopmentSourceSmokeTestOperationRepository(session).get(
                operation_id
            )
        if operation is None or operation.managed_instance_id is None:
            return None
        if operation.state is not DevelopmentSourceSmokeTestOperationState.QUEUED:
            raise DevelopmentSourceSmokeTestConflictError(
                "Smoke test is already claimed or terminal."
            )
        lease = DevelopmentSourceSmokeTestLease(
            operation.id,
            operation.managed_instance_id,
            operation.development_source_id,
            secrets.token_urlsafe(32),
        )
        now = self._clock()
        expires = now + SMOKE_OPERATION_BOUND
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            operations = DevelopmentSourceSmokeTestOperationRepository(session)
            fresh = operations.get(operation_id)
            leases = ManagedInstanceMutationLeaseRepository(session)
            mutation = leases.current(lease.instance_id)
            intent = (
                "original_managed_instance_id",
                "managed_instance_id",
                "development_source_id",
                "runtime_revision_id",
                "expected_container_id",
                "addon_id",
                "addon_version",
                "source_logical_name",
                "requested_at",
            )
            if (
                fresh is None
                or fresh.state is not DevelopmentSourceSmokeTestOperationState.QUEUED
                or tuple(getattr(fresh, name) for name in intent)
                != tuple(getattr(operation, name) for name in intent)
                or fresh.phase is not DevelopmentSourceSmokeTestOperationPhase.ACQUIRE
                or fresh.invocation_dispatched
                or fresh.reconciliation_required
                or fresh.lease_token_digest is not None
                or fresh.lease_expires_at is not None
                or mutation is None
                or mutation.operation_kind
                is not ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_SMOKE_TEST
                or mutation.operation_id != str(operation_id)
                or mutation.lease_token_digest is not None
                or mutation.lease_expires_at is not None
                or mutation.acquired_at is not None
                or mutation.heartbeat_at is not None
            ):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Exact unclaimed smoke authority is unavailable."
                )
            selected = require_source_session_admission(
                session,
                target_id=str(lease.instance_id),
                revision_id=str(fresh.runtime_revision_id),
                source_id=str(lease.source_id),
                container_id=fresh.expected_container_id,
            )
            if selected is not None and selected.container_id is None:
                raise DevelopmentSessionConflict("development_session_requires_review")
            lease = replace(
                lease,
                selected_generation=None if selected is None else selected.generation,
                expected_container_id=fresh.expected_container_id
                or (None if selected is None else selected.container_id),
            )
            mutation_claimed = leases.claim(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            acquired = operations.acquire(
                lease.operation_id,
                token_digest=lease.token_digest,
                expected_container_id=fresh.expected_container_id,
                now=now,
                lease_expires_at=expires,
            )
            if not (mutation_claimed and acquired):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test authority could not be acquired."
                )
        return lease

    def _require_owned(
        self,
        session: Session,
        lease: DevelopmentSourceSmokeTestLease,
    ) -> DevelopmentSourceSmokeTestOperation:
        operation = DevelopmentSourceSmokeTestOperationRepository(session).get(
            lease.operation_id
        )
        leases = ManagedInstanceMutationLeaseRepository(session)
        mutation = leases.current(lease.instance_id)
        now = self._clock()
        if (
            operation is None
            or operation.state is not DevelopmentSourceSmokeTestOperationState.RUNNING
            or operation.managed_instance_id != lease.instance_id
            or operation.development_source_id != lease.source_id
            or operation.lease_token_digest != lease.token_digest
            or operation.lease_expires_at is None
            or operation.lease_expires_at <= now
            or mutation is None
            or mutation.operation_kind
            is not ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_SMOKE_TEST
            or not leases.renew(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=operation.lease_expires_at,
            )
        ):
            raise DevelopmentSourceSmokeTestConflictError(
                "Exact live smoke authority is unavailable."
            )
        return operation

    @staticmethod
    def _require_scope(
        session: Session,
        lease: DevelopmentSourceSmokeTestLease,
        operation: DevelopmentSourceSmokeTestOperation,
        container_id: str,
    ) -> None:
        selected = require_source_session_admission(
            session,
            target_id=str(lease.instance_id),
            revision_id=str(operation.runtime_revision_id),
            source_id=str(lease.source_id),
            container_id=container_id,
        )
        generation = None if selected is None else selected.generation
        if generation != lease.selected_generation or (
            lease.expected_container_id is not None
            and lease.expected_container_id != container_id
        ):
            raise DevelopmentSessionConflict("development_session_identity_changed")

    def set_expected_container(
        self, lease: DevelopmentSourceSmokeTestLease, container_id: str
    ) -> None:
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            operation = self._require_owned(session, lease)
            self._require_scope(session, lease, operation, container_id)
            if not DevelopmentSourceSmokeTestOperationRepository(
                session
            ).set_expected_container(
                lease.operation_id,
                token_digest=lease.token_digest,
                expected_container_id=container_id,
                now=self._clock(),
            ):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test runtime precondition changed."
                )

    def update_phase(
        self,
        lease: DevelopmentSourceSmokeTestLease,
        phase: DevelopmentSourceSmokeTestOperationPhase,
    ) -> None:
        with self._session_factory.begin() as session:
            if not DevelopmentSourceSmokeTestOperationRepository(session).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=self._clock(),
            ):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test authority expired or changed."
                )

    def mark_invocation_dispatched(
        self, lease: DevelopmentSourceSmokeTestLease
    ) -> None:
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            operation = self._require_owned(session, lease)
            if operation.expected_container_id is None:
                raise DevelopmentSourceSmokeTestConflictError(
                    "Observed smoke profile binding is required."
                )
            self._require_scope(
                session, lease, operation, operation.expected_container_id
            )
            if not DevelopmentSourceSmokeTestOperationRepository(
                session
            ).mark_invocation_dispatched(
                lease.operation_id,
                token_digest=lease.token_digest,
                now=self._clock(),
            ):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test dispatch marker could not be recorded."
                )

    def finish(
        self,
        lease: DevelopmentSourceSmokeTestLease,
        *,
        state: DevelopmentSourceSmokeTestOperationState,
        overall_result: DevelopmentSourceSmokeTestOverallResult | None,
        execution_result: DevelopmentSourceSmokeTestExecutionResult,
        post_provenance: DevelopmentSourceSmokeTestPostProvenance,
        duration_ms: int | None = None,
        item_count: int | None = None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
        failure_reason: str | None = None,
        confidence: DevelopmentSourceDeploymentConfidence | None = None,
    ) -> DevelopmentSourceSmokeTestOperation:
        now = self._clock()
        reconciliation = (
            state is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
        )
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._require_owned(session, lease)
            if confidence is not None:
                DevelopmentSourceDeploymentRepository(session).mark_confidence(
                    lease.instance_id, lease.source_id, confidence
                )
            operations = DevelopmentSourceSmokeTestOperationRepository(session)
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
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test operation ownership was lost."
                )
            if not ManagedInstanceMutationLeaseRepository(session).release(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
            ):
                raise DevelopmentSourceSmokeTestConflictError(
                    "Smoke-test mutation lease cleanup could not be proven."
                )
            operations.prune_terminal(
                lease.instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
            )
            operation = operations.get(lease.operation_id)
            assert operation is not None
            return operation

    def get(self, operation_id: UUID) -> DevelopmentSourceSmokeTestOperation | None:
        with self._session_factory() as session:
            return DevelopmentSourceSmokeTestOperationRepository(session).get(
                operation_id
            )

    def queued(self) -> list[DevelopmentSourceSmokeTestOperation]:
        with self._session_factory() as session:
            return DevelopmentSourceSmokeTestOperationRepository(session).list_queued()

    @staticmethod
    def to_status(
        operation: DevelopmentSourceSmokeTestOperation,
    ) -> DevelopmentSourceSmokeTestStatus:
        return DevelopmentSourceSmokeTestStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            managed_instance_id=operation.managed_instance_id,
            development_source_id=operation.development_source_id,
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


class DevelopmentSourceSmokeTestService:
    """Run fixed preflight, invocation, and exact post-readback once."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _SmokeBroker,
        validator: DevelopmentSourceValidator,
        coordinator: DevelopmentSourceSmokeTestOperationCoordinator,
        lease: DevelopmentSourceSmokeTestLease,
        *,
        inventory_adapter_factory: InventoryAdapterFactory | None = None,
        details_adapter_factory: DetailsAdapterFactory | None = None,
        smoke_adapter_factory: SmokeAdapterFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._validator = validator
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

    def run(self) -> DevelopmentSourceSmokeTestStatus:
        dispatched = False
        execution = DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED
        duration: int | None = None
        count: int | None = None
        try:
            context = self._load_context()
            if isinstance(context, str):
                return self._failed(context)
            instance, source, profile = context
            self._coordinator.update_phase(
                self._lease, DevelopmentSourceSmokeTestOperationPhase.VALIDATE
            )
            try:
                descriptor = self._validator.validate(source.relative_locator)
            except DevelopmentSourceValidationError as error:
                return self._failed(error.code.value)
            if (
                descriptor.addon_id != source.addon_id
                or descriptor.observed_version != self._operation().addon_version
            ):
                return self._failed("deployment_evidence_stale")
            if copied_video_plugin_library(descriptor) is None:
                return self._failed(
                    "unsupported_addon_type",
                    overall=DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            if profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS:
                return self._failed("runtime_bytecode_policy_incompatible")
            runtime = self._resolve_runtime(instance.runtime_revision_id)
            if isinstance(runtime, str):
                return self._failed(runtime)
            observation, container_id = runtime
            self._coordinator.set_expected_container(self._lease, container_id)
            evidence_error = self._validate_evidence(
                source.addon_id, instance.runtime_revision_id, container_id
            )
            if evidence_error is not None:
                return self._failed(evidence_error)
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
            recognized = inventory_by_id.get(source.addon_id)
            if recognized is None:
                return self._failed("addon_not_recognized")
            if recognized.version != self._operation().addon_version:
                return self._failed("addon_version_mismatch")
            if not recognized.enabled:
                return self._failed("addon_disabled")
            if recognized.kodi_type != "xbmc.python.pluginsource":
                return self._failed(
                    "unsupported_addon_type",
                    overall=DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            details_adapter = self._details_factory(observation, profile)
            try:
                details = details_adapter.get_addon_details(source.addon_id)
            except JsonRpcUnavailableError:
                return self._failed("jsonrpc_unavailable")
            except (
                KodiAddonDetailsUnsupportedResponseError,
                KodiAddonInventoryMalformedDataError,
                ValueError,
            ):
                return self._failed("unsupported_response")
            if (
                details.addon_id != source.addon_id
                or details.version != self._operation().addon_version
            ):
                return self._failed("addon_version_mismatch")
            if not details.enabled:
                return self._failed("addon_disabled")
            if details.kodi_type != "xbmc.python.pluginsource":
                return self._failed(
                    "unsupported_addon_type",
                    overall=DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            mismatch = DevelopmentSourceEnablementService._declarations_match(
                descriptor.dependencies, details.dependencies
            )
            if mismatch is not None:
                return self._dependency_blocked("dependency_state_unknown", mismatch)
            dependencies = DevelopmentSourceEnablementService._preflight_dependencies(
                descriptor.dependencies,
                inventory_by_id,
                kodi_release=profile.kodi_release.canonical,
            )
            blocked = first_blocking_dependency(dependencies)
            if blocked is not None:
                return self._dependency_blocked(
                    dependency_failure_code(blocked.status), blocked
                )
            self._coordinator.update_phase(
                self._lease,
                DevelopmentSourceSmokeTestOperationPhase.VERIFY_PRE_EXECUTION,
            )
            verified = self._verify_destination(container_id)
            if isinstance(verified, str):
                if verified == "development_destination_mismatch":
                    return self._failed(
                        "source_tree_changed",
                        overall=(
                            DevelopmentSourceSmokeTestOverallResult.SOURCE_TREE_CHANGED
                        ),
                        confidence=DevelopmentSourceDeploymentConfidence.STALE,
                    )
                return self._failed(
                    "deployment_verification_failed",
                    overall=(
                        DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED
                    ),
                )
            if not verified.video_plugin_descriptor_eligible:
                return self._failed(
                    "unsupported_addon_type",
                    overall=DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE,
                )
            self._coordinator.mark_invocation_dispatched(self._lease)
            dispatched = True
            adapter = self._smoke_factory(observation, profile)
            try:
                invocation = adapter.invoke(source.addon_id)
                execution = DevelopmentSourceSmokeTestExecutionResult.DIRECTORY_RESULT
                duration = invocation.duration_ms
                count = invocation.item_count
            except KodiAddonSmokeJsonRpcError as error:
                execution = DevelopmentSourceSmokeTestExecutionResult.JSONRPC_ERROR
                duration = error.duration_ms
            except KodiAddonSmokeTimeoutError as error:
                return self._reconciliation(
                    DevelopmentSourceSmokeTestOverallResult.TIMEOUT,
                    DevelopmentSourceSmokeTestExecutionResult.TIMEOUT,
                    duration_ms=error.duration_ms,
                    reason="smoke_invocation_timeout",
                )
            except KodiAddonSmokeAmbiguousError:
                return self._reconciliation(
                    DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS,
                    reason="smoke_invocation_ambiguous",
                )
            self._coordinator.update_phase(
                self._lease,
                DevelopmentSourceSmokeTestOperationPhase.VERIFY_POST_EXECUTION,
            )
            post = self._verify_destination(container_id)
            if isinstance(post, str):
                if post == "development_destination_mismatch":
                    return self._terminal(
                        state=DevelopmentSourceSmokeTestOperationState.FAILED,
                        overall=(
                            DevelopmentSourceSmokeTestOverallResult.SOURCE_TREE_CHANGED
                        ),
                        execution=execution,
                        post=DevelopmentSourceSmokeTestPostProvenance.CHANGED,
                        duration_ms=duration,
                        item_count=count,
                        reason="source_tree_changed",
                        confidence=DevelopmentSourceDeploymentConfidence.STALE,
                    )
                return self._reconciliation(
                    DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    execution,
                    duration_ms=duration,
                    item_count=count,
                    reason="post_execution_verification_unknown",
                )
            if execution is DevelopmentSourceSmokeTestExecutionResult.JSONRPC_ERROR:
                return self._terminal(
                    state=DevelopmentSourceSmokeTestOperationState.FAILED,
                    overall=DevelopmentSourceSmokeTestOverallResult.EXECUTION_FAILED,
                    execution=execution,
                    post=DevelopmentSourceSmokeTestPostProvenance.MATCHED,
                    duration_ms=duration,
                    reason="kodi_plugin_execution_failed",
                )
            return self._terminal(
                state=DevelopmentSourceSmokeTestOperationState.SUCCEEDED,
                overall=DevelopmentSourceSmokeTestOverallResult.PASSED,
                execution=execution,
                post=DevelopmentSourceSmokeTestPostProvenance.MATCHED,
                duration_ms=duration,
                item_count=count,
            )
        except DevelopmentSessionConflict as error:
            if dispatched:
                return self._reconciliation(
                    DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS,
                    duration_ms=duration,
                    item_count=count,
                    reason="smoke_invocation_ambiguous",
                )
            return self._failed(error.code)
        except Exception:
            if dispatched:
                return self._reconciliation(
                    DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED,
                    DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS,
                    duration_ms=duration,
                    item_count=count,
                    reason="smoke_invocation_ambiguous",
                )
            return self._failed("development_source_smoke_test_failed")

    def _load_context(
        self,
    ) -> tuple[ManagedInstance, DevelopmentSource, RuntimeExecutionProfile] | str:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(self._lease.instance_id)
            source = DevelopmentSourceRepository(session).get(self._lease.source_id)
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                self._lease.instance_id, self._lease.source_id
            )
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if instance is None or source is None or attachment is None:
            return "source_not_attached"
        if instance.instance_type is not InstanceType.DISPOSABLE or profile is None:
            return "smoke_test_unsupported"
        if instance.runtime_revision_id != self._operation().runtime_revision_id:
            return "runtime_revision_mismatch"
        return instance, source, profile

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
        self, addon_id: str, revision_id: UUID, container_id: str
    ) -> str | None:
        with self._session_factory() as session:
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                self._lease.instance_id, self._lease.source_id
            )
            operation = (
                None
                if evidence is None
                else DevelopmentSourceDeploymentOperationRepository(session).get(
                    evidence.last_successful_operation_id
                )
            )
        if evidence is None:
            return "source_not_deployed"
        if evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE:
            return "deployment_evidence_stale"
        if evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN:
            return "deployment_evidence_unknown"
        if (
            evidence.addon_id != addon_id
            or evidence.runtime_revision_id != revision_id
            or evidence.trusted_container_id != container_id
            or evidence.observed_source_version != self._operation().addon_version
            or operation is None
            or operation.state
            is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
            or operation.original_managed_instance_id != self._lease.instance_id
            or operation.managed_instance_id != self._lease.instance_id
            or operation.development_source_id != self._lease.source_id
            or operation.runtime_revision_id != revision_id
            or operation.expected_container_id != container_id
        ):
            return "deployment_evidence_stale"
        return None

    def _verify_destination(
        self, container_id: str
    ) -> VerifyAttachedDevelopmentSourceDestinationResult | str:
        try:
            result = self._broker.verify_attached_development_source_destination(
                managed_instance_id=str(self._lease.instance_id),
                development_source_id=str(self._lease.source_id),
                expected_container_id=container_id,
            )
        except ClientError as error:
            return getattr(error, "code", "development_destination_verification_failed")
        if (
            result.managed_instance_id != str(self._lease.instance_id)
            or result.development_source_id != str(self._lease.source_id)
            or result.runtime_revision_id != str(self._operation().runtime_revision_id)
            or result.container_id != container_id
            or result.addon_id != self._operation().addon_id
            or result.addon_version != self._operation().addon_version
            or not result.matched
        ):
            return "development_destination_verification_failed"
        return result

    def _operation(self) -> DevelopmentSourceSmokeTestOperation:
        operation = self._coordinator.get(self._lease.operation_id)
        if operation is None:
            raise DevelopmentSourceSmokeTestConflictError(
                "Smoke-test history no longer exists."
            )
        return operation

    def _dependency_blocked(
        self, code: str, blocked: DevelopmentSourceDependencyState
    ) -> DevelopmentSourceSmokeTestStatus:
        return self._terminal(
            state=DevelopmentSourceSmokeTestOperationState.FAILED,
            overall=DevelopmentSourceSmokeTestOverallResult.DEPENDENCY_BLOCKED,
            execution=DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED,
            post=DevelopmentSourceSmokeTestPostProvenance.NOT_CHECKED,
            blocked_dependency_id=blocked.dependency_id,
            blocked_dependency_reason=code,
            reason=code,
        )

    def _failed(
        self,
        reason: str,
        *,
        overall: DevelopmentSourceSmokeTestOverallResult | None = None,
        confidence: DevelopmentSourceDeploymentConfidence | None = None,
    ) -> DevelopmentSourceSmokeTestStatus:
        return self._terminal(
            state=DevelopmentSourceSmokeTestOperationState.FAILED,
            overall=overall,
            execution=DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED,
            post=DevelopmentSourceSmokeTestPostProvenance.NOT_CHECKED,
            reason=reason,
            confidence=confidence,
        )

    def _reconciliation(
        self,
        overall: DevelopmentSourceSmokeTestOverallResult,
        execution: DevelopmentSourceSmokeTestExecutionResult,
        *,
        duration_ms: int | None = None,
        item_count: int | None = None,
        reason: str,
    ) -> DevelopmentSourceSmokeTestStatus:
        return self._terminal(
            state=DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED,
            overall=overall,
            execution=execution,
            post=DevelopmentSourceSmokeTestPostProvenance.UNKNOWN,
            duration_ms=duration_ms,
            item_count=item_count,
            reason=reason,
            confidence=DevelopmentSourceDeploymentConfidence.UNKNOWN,
        )

    def _terminal(
        self,
        *,
        state: DevelopmentSourceSmokeTestOperationState,
        overall: DevelopmentSourceSmokeTestOverallResult | None,
        execution: DevelopmentSourceSmokeTestExecutionResult,
        post: DevelopmentSourceSmokeTestPostProvenance,
        duration_ms: int | None = None,
        item_count: int | None = None,
        blocked_dependency_id: str | None = None,
        blocked_dependency_reason: str | None = None,
        reason: str | None = None,
        confidence: DevelopmentSourceDeploymentConfidence | None = None,
    ) -> DevelopmentSourceSmokeTestStatus:
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


type DevelopmentSourceSmokeTestServiceFactory = Callable[
    [DevelopmentSourceSmokeTestLease], DevelopmentSourceSmokeTestService
]


class DevelopmentSourceSmokeTestExecutor:
    """Run durable smoke operations outside normal Web request lifetimes."""

    def __init__(
        self,
        coordinator: DevelopmentSourceSmokeTestOperationCoordinator,
        service_factory: DevelopmentSourceSmokeTestServiceFactory,
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

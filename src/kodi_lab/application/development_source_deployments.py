"""Durable stopped-only DevelopmentSource copy/sync coordination."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import ClassVar, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import DevelopmentSourceDeploymentClientError
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)
from kodi_lab.domain import (
    AddonZipInstallationConfidence,
    AddonZipInstallationResult,
    AddonZipOrigin,
    DevelopmentSourceDeployment,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentMode,
    DevelopmentSourceDeploymentOperation,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentRequestKind,
    DevelopmentSourceDeploymentResult,
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstanceMutationKind,
    RepositoryReplacementOperationState,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AgentWorkspacePromotionOperationRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RepositoryReplacementOperationRepository,
)

DEPLOYMENT_LEASE_DURATION = timedelta(minutes=10)
TERMINAL_RETENTION_PER_INSTANCE = 20


class DevelopmentSourceDeploymentError(RuntimeError):
    code = "development_source_deployment_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DevelopmentSourceDeploymentConflictError(DevelopmentSourceDeploymentError):
    code = "development_source_deployment_conflict"


class DevelopmentSourceDeploymentUnsupportedError(DevelopmentSourceDeploymentError):
    code = "development_source_deployment_unsupported"


class DevelopmentSourceDeploymentLeaseLostError(DevelopmentSourceDeploymentError):
    code = "development_source_deployment_lease_lost"


class DevelopmentSourceDeploymentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    managed_instance_id: UUID | None
    original_managed_instance_id: UUID
    development_source_id: UUID
    state: DevelopmentSourceDeploymentOperationState
    phase: DevelopmentSourceDeploymentOperationPhase
    result_classification: DevelopmentSourceDeploymentResult | None
    new_count: int | None = Field(default=None, ge=0)
    changed_count: int | None = Field(default=None, ge=0)
    unchanged_count: int | None = Field(default=None, ge=0)
    destination_only_count: int | None = Field(default=None, ge=0)
    transferred_bytes: int | None = Field(default=None, ge=0)
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool


class _DeploymentBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult: ...


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDeploymentLease:
    operation_id: UUID
    managed_instance_id: UUID
    development_source_id: UUID
    expected_container_id: str
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class DevelopmentSourceDeploymentOperationCoordinator:
    """Reserve both typed authorities and own all short operation writes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _DeploymentBroker,
        validator: DevelopmentSourceValidator,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_dispatcher: StaleOperationReconciliationDispatcher | None = None,
        reconciliation: ManagedInstanceReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._validator = validator
        self._clock = clock
        self._stale = stale_dispatcher or StaleOperationReconciliationDispatcher(
            session_factory, clock=clock, broker=broker
        )
        self._reconciliation = reconciliation or ManagedInstanceReconciliationService(
            session_factory, broker=broker
        )

    def queue(
        self,
        instance_id: UUID,
        source_id: UUID,
        *,
        precondition: Callable[[Session], None] | None = None,
    ) -> DevelopmentSourceDeploymentStatus:
        self._stale.sweep_instance(instance_id)
        self._stale.sweep_global()
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.DEVELOPMENT_SOURCE_DEPLOYMENT
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            source = DevelopmentSourceRepository(session).get(source_id)
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                instance_id, source_id
            )
            promotion_reconciliation = AgentWorkspacePromotionOperationRepository(
                session
            ).reconciliation_for_source(source_id)
        if instance is None or source is None or attachment is None:
            raise DevelopmentSourceDeploymentUnsupportedError(
                "An attached development source is required."
            )
        if promotion_reconciliation:
            raise DevelopmentSourceDeploymentConflictError(
                "Development source promotion requires reconciliation.",
                code="promotion_source_reconciliation_required",
            )
        if instance.instance_type is not InstanceType.DISPOSABLE:
            raise DevelopmentSourceDeploymentUnsupportedError(
                "Copy/sync supports disposable instances only."
            )
        if self._repository_override_active(instance_id, source.addon_id):
            raise DevelopmentSourceDeploymentConflictError(
                "Attached DevelopmentSource is currently overridden.",
                code="repository_override_active",
            )
        try:
            observation = self._validator.validate(source.relative_locator)
        except DevelopmentSourceValidationError as error:
            raise DevelopmentSourceDeploymentUnsupportedError(
                error.code.value
            ) from error
        if observation.addon_id != source.addon_id:
            raise DevelopmentSourceDeploymentUnsupportedError(
                "development_source_identity_changed"
            )
        container_id = self._require_stopped(instance_id)
        operation = DevelopmentSourceDeploymentOperation(
            id=uuid4(),
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            development_source_id=source_id,
            source_logical_name=source.logical_name,
            addon_id=source.addon_id,
            runtime_revision_id=instance.runtime_revision_id,
            request_kind=DevelopmentSourceDeploymentRequestKind.COPY_SYNC,
            requested_at=self._clock(),
            state=DevelopmentSourceDeploymentOperationState.QUEUED,
            phase=DevelopmentSourceDeploymentOperationPhase.ACQUIRE,
            expected_container_id=container_id,
        )
        try:
            with self._session_factory() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if precondition is not None:
                    precondition(session)
                current = ManagedInstanceRepository(session).get(instance_id)
                attached = ManagedInstanceDevelopmentSourceRepository(session).get(
                    instance_id, source_id
                )
                if (
                    current is None
                    or current.instance_type is not InstanceType.DISPOSABLE
                    or current.runtime_revision_id != instance.runtime_revision_id
                    or attached is None
                ):
                    raise DevelopmentSourceDeploymentConflictError(
                        "Source deployment scope changed before queue claim."
                    )
                operations = DevelopmentSourceDeploymentOperationRepository(session)
                if operations.active_for_instance(instance_id) is not None:
                    raise DevelopmentSourceDeploymentConflictError(
                        "A development-source deployment is already active."
                    )
                operations.create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
                    operation_id=operation.id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
                    operation_id=operation.id,
                )
                session.commit()
        except IntegrityError as error:
            raise DevelopmentSourceDeploymentConflictError(
                "Another instance mutation or heavy operation is active."
            ) from error
        return self.to_status(operation)

    def _repository_override_active(self, instance_id: UUID, addon_id: str) -> bool:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            evidence = AddonZipInstallationEvidenceRepository(session).get(
                instance_id, addon_id
            )
            operation = (
                None
                if evidence is None or evidence.replacement_operation_id is None
                else RepositoryReplacementOperationRepository(session).get(
                    evidence.replacement_operation_id
                )
            )
        if (
            instance is None
            or evidence is None
            or operation is None
            or evidence.confidence
            is not AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
            or evidence.result
            is not AddonZipInstallationResult.REPOSITORY_REPLACEMENT_RECOGNIZED
            or evidence.artifact_origin is not AddonZipOrigin.REPOSITORY_DOWNLOAD
            or evidence.runtime_revision_id != instance.runtime_revision_id
            or operation.state is not RepositoryReplacementOperationState.SUCCEEDED
            or operation.managed_instance_id != instance_id
            or operation.addon_id != addon_id
            or not operation.stopped_readback_matched
            or not operation.final_readback_matched
        ):
            return False
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance_id)
            and item.container_id == evidence.trusted_container_id
        ]
        return len(matches) == 1

    def acquire(self, operation_id: UUID) -> DevelopmentSourceDeploymentLease | None:
        with self._session_factory() as session:
            operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                operation_id
            )
        if operation is None or operation.managed_instance_id is None:
            return None
        if operation.state is not DevelopmentSourceDeploymentOperationState.QUEUED:
            raise DevelopmentSourceDeploymentLeaseLostError(
                "Deployment is already claimed or terminal."
            )
        container_id = self._require_stopped(operation.managed_instance_id)
        token = secrets.token_urlsafe(32)
        lease = DevelopmentSourceDeploymentLease(
            operation.id,
            operation.managed_instance_id,
            operation.development_source_id,
            container_id,
            token,
        )
        now = self._clock()
        expires = now + DEPLOYMENT_LEASE_DURATION
        refusal: str | None = None
        with self._session_factory() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            operations = DevelopmentSourceDeploymentOperationRepository(session)
            fresh = operations.get(operation_id)
            mutations = ManagedInstanceMutationLeaseRepository(session)
            heavy_operations = GlobalHeavyOperationLeaseRepository(session)
            mutation = mutations.current(lease.managed_instance_id)
            heavy = heavy_operations.current()
            if (
                fresh is None
                or fresh.state is not DevelopmentSourceDeploymentOperationState.QUEUED
                or fresh.original_managed_instance_id
                != operation.original_managed_instance_id
                or fresh.managed_instance_id != operation.managed_instance_id
                or fresh.runtime_revision_id != operation.runtime_revision_id
                or fresh.development_source_id != operation.development_source_id
                or fresh.expected_container_id != operation.expected_container_id
                or fresh.lease_token_digest is not None
                or fresh.lease_expires_at is not None
                or mutation is None
                or mutation.operation_kind
                is not ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT
                or mutation.operation_id != str(operation_id)
                or mutation.lease_token_digest is not None
                or mutation.lease_expires_at is not None
                or heavy is None
                or heavy.operation_kind
                is not GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT
                or heavy.operation_id != str(operation_id)
                or heavy.lease_token_digest is not None
                or heavy.lease_expires_at is not None
            ):
                raise DevelopmentSourceDeploymentLeaseLostError(
                    "Exact unclaimed deployment authority is unavailable."
                )
            instance = ManagedInstanceRepository(session).get(lease.managed_instance_id)
            attached = ManagedInstanceDevelopmentSourceRepository(session).get(
                lease.managed_instance_id, lease.development_source_id
            )
            if (
                fresh.expected_container_id is not None
                and fresh.expected_container_id != container_id
            ):
                refusal = "queued_profile_changed"
            elif (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id != fresh.runtime_revision_id
                or attached is None
            ):
                refusal = "queued_scope_changed"
            if refusal is not None:
                # QUEUED plus both exact unclaimed reservations proves that no
                # legal copy dispatch began. Never settle claimed/unknown work.
                if not operations.fail_queued(operation_id, reason=refusal, now=now):
                    raise DevelopmentSourceDeploymentLeaseLostError(
                        "Queued deployment refusal could not be retained."
                    )
                released_mutation = mutations.release_for_operation(
                    lease.managed_instance_id, operation_id
                )
                released_heavy = heavy_operations.release_for_operation(operation_id)
                if not (released_mutation and released_heavy):
                    raise DevelopmentSourceDeploymentLeaseLostError(
                        "Exact queued reservation release was not confirmed."
                    )
            else:
                claimed_mutation = mutations.claim(
                    lease.managed_instance_id,
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                    now=now,
                    expires_at=expires,
                )
                claimed_heavy = heavy_operations.claim(
                    operation_id=lease.operation_id,
                    token_digest=lease.token_digest,
                    now=now,
                    expires_at=expires,
                )
                acquired = operations.acquire(
                    operation_id,
                    token_digest=lease.token_digest,
                    expected_container_id=container_id,
                    now=now,
                    lease_expires_at=expires,
                )
                if not (claimed_mutation and claimed_heavy and acquired):
                    raise DevelopmentSourceDeploymentLeaseLostError(
                        "Deployment authority could not be acquired."
                    )
            session.commit()
        if refusal is not None:
            raise DevelopmentSourceDeploymentConflictError(
                "Queued source profile changed."
                if refusal == "queued_profile_changed"
                else "Queued source scope changed.",
                code=f"development_source_{refusal}",
            )
        return lease

    def update_phase(
        self,
        lease: DevelopmentSourceDeploymentLease,
        phase: DevelopmentSourceDeploymentOperationPhase,
    ) -> None:
        now = self._clock()
        expires = now + DEPLOYMENT_LEASE_DURATION
        with self._session_factory.begin() as session:
            operation = DevelopmentSourceDeploymentOperationRepository(
                session
            ).update_phase(
                lease.operation_id,
                token_digest=lease.token_digest,
                phase=phase,
                now=now,
                lease_expires_at=expires,
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.managed_instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).renew(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not (operation and mutation and heavy):
                raise DevelopmentSourceDeploymentLeaseLostError(
                    "Deployment authority was lost."
                )

    def finish_success(
        self,
        lease: DevelopmentSourceDeploymentLease,
        result: CopySyncAttachedDevelopmentSourceResult,
    ) -> None:
        now = self._clock()
        evidence = DevelopmentSourceDeployment(
            managed_instance_id=lease.managed_instance_id,
            development_source_id=lease.development_source_id,
            addon_id=self._operation(lease.operation_id).addon_id,
            mode=DevelopmentSourceDeploymentMode.COPY,
            last_successful_operation_id=lease.operation_id,
            source_manifest_digest=result.source_manifest_digest,
            verified_destination_digest=result.verified_destination_digest,
            runtime_revision_id=UUID(result.runtime_revision_id),
            trusted_container_id=result.container_id,
            observed_source_name=result.observed_source_name,
            observed_source_version=result.observed_source_version,
            verified_at=now,
            confidence=(
                DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
            ),
        )
        with self._session_factory.begin() as session:
            DevelopmentSourceDeploymentRepository(session).upsert_verified(evidence)
            self._finish_row(
                session,
                lease,
                state=DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                result=result,
                reason=None,
                reconciliation_required=False,
                now=now,
            )

    def finish_failure(
        self,
        lease: DevelopmentSourceDeploymentLease,
        *,
        reason: str,
        reconciliation_required: bool,
        replacement_conflict: bool = False,
    ) -> None:
        with self._session_factory.begin() as session:
            if reconciliation_required or replacement_conflict:
                DevelopmentSourceDeploymentRepository(session).mark_confidence(
                    lease.managed_instance_id,
                    lease.development_source_id,
                    (
                        DevelopmentSourceDeploymentConfidence.UNKNOWN
                        if reconciliation_required
                        else DevelopmentSourceDeploymentConfidence.STALE
                    ),
                )
            self._finish_row(
                session,
                lease,
                state=(
                    DevelopmentSourceDeploymentOperationState.RECONCILIATION_REQUIRED
                    if reconciliation_required
                    else DevelopmentSourceDeploymentOperationState.FAILED
                ),
                result=None,
                reason=reason[:256],
                reconciliation_required=reconciliation_required,
                now=self._clock(),
            )

    def _finish_row(
        self,
        session: Session,
        lease: DevelopmentSourceDeploymentLease,
        *,
        state: DevelopmentSourceDeploymentOperationState,
        result: CopySyncAttachedDevelopmentSourceResult | None,
        reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> None:
        operations = DevelopmentSourceDeploymentOperationRepository(session)
        if not operations.finish(
            lease.operation_id,
            token_digest=lease.token_digest,
            state=state,
            result_classification=(
                None
                if result is None
                else DevelopmentSourceDeploymentResult(result.result_classification)
            ),
            new_count=None if result is None else result.new_count,
            changed_count=None if result is None else result.changed_count,
            unchanged_count=None if result is None else result.unchanged_count,
            destination_only_count=(
                None if result is None else result.destination_only_count
            ),
            transferred_bytes=None if result is None else result.transferred_bytes,
            failure_reason=reason,
            reconciliation_required=reconciliation_required,
            now=now,
        ):
            raise DevelopmentSourceDeploymentLeaseLostError(
                "Deployment operation ownership was lost."
            )
        if not ManagedInstanceMutationLeaseRepository(session).release(
            lease.managed_instance_id,
            operation_id=lease.operation_id,
            token_digest=lease.token_digest,
        ) or not GlobalHeavyOperationLeaseRepository(session).release(
            operation_id=lease.operation_id, token_digest=lease.token_digest
        ):
            raise DevelopmentSourceDeploymentLeaseLostError(
                "Deployment lease cleanup could not be proven."
            )
        operations.prune_terminal(
            lease.managed_instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
        )

    def get(self, operation_id: UUID) -> DevelopmentSourceDeploymentOperation | None:
        with self._session_factory() as session:
            return DevelopmentSourceDeploymentOperationRepository(session).get(
                operation_id
            )

    def queued(self) -> list[DevelopmentSourceDeploymentOperation]:
        with self._session_factory() as session:
            return DevelopmentSourceDeploymentOperationRepository(session).list_queued()

    def _operation(self, operation_id: UUID) -> DevelopmentSourceDeploymentOperation:
        operation = self.get(operation_id)
        if operation is None:
            raise DevelopmentSourceDeploymentLeaseLostError(
                "Deployment operation no longer exists."
            )
        return operation

    def _require_stopped(self, instance_id: UUID) -> str:
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance_id)
        ]
        if (
            len(matches) != 1
            or matches[0].runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise DevelopmentSourceDeploymentUnsupportedError(
                "Copy/sync requires one exact provisioned stopped container."
            )
        return matches[0].container_id

    @staticmethod
    def to_status(
        operation: DevelopmentSourceDeploymentOperation,
    ) -> DevelopmentSourceDeploymentStatus:
        return DevelopmentSourceDeploymentStatus(
            operation_id=operation.id,
            managed_instance_id=operation.managed_instance_id,
            original_managed_instance_id=operation.original_managed_instance_id,
            development_source_id=operation.development_source_id,
            state=operation.state,
            phase=operation.phase,
            result_classification=operation.result_classification,
            new_count=operation.new_count,
            changed_count=operation.changed_count,
            unchanged_count=operation.unchanged_count,
            destination_only_count=operation.destination_only_count,
            transferred_bytes=operation.transferred_bytes,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
        )


class DevelopmentSourceDeploymentService:
    """Invoke exactly one broker semantic operation under both durable leases."""

    _RECONCILIATION_CODES: ClassVar[set[str]] = {
        "development_source_reconciliation_required",
        "development_source_apply_unverified",
        "development_destination_verification_failed",
    }

    def __init__(
        self,
        broker: _DeploymentBroker,
        coordinator: DevelopmentSourceDeploymentOperationCoordinator,
        lease: DevelopmentSourceDeploymentLease,
    ) -> None:
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease

    def deploy(self) -> CopySyncAttachedDevelopmentSourceResult:
        self._coordinator.update_phase(
            self._lease, DevelopmentSourceDeploymentOperationPhase.RESOLVE
        )
        self._coordinator.update_phase(
            self._lease, DevelopmentSourceDeploymentOperationPhase.STAGE
        )
        try:
            result = self._broker.copy_sync_attached_development_source(
                managed_instance_id=str(self._lease.managed_instance_id),
                development_source_id=str(self._lease.development_source_id),
                expected_container_id=self._lease.expected_container_id,
                deployment_operation_id=str(self._lease.operation_id),
            )
        except DevelopmentSourceDeploymentClientError as error:
            reconciliation = error.code in self._RECONCILIATION_CODES
            self._coordinator.finish_failure(
                self._lease,
                reason=error.code,
                reconciliation_required=reconciliation,
                replacement_conflict=(
                    error.code == "development_source_replacement_conflict"
                ),
            )
            raise DevelopmentSourceDeploymentError(error.code) from error
        self._coordinator.update_phase(
            self._lease, DevelopmentSourceDeploymentOperationPhase.VERIFY
        )
        self._coordinator.finish_success(self._lease, result)
        return result


type DevelopmentSourceDeploymentServiceFactory = Callable[
    [DevelopmentSourceDeploymentLease], DevelopmentSourceDeploymentService
]


class DevelopmentSourceDeploymentExecutor:
    def __init__(
        self,
        coordinator: DevelopmentSourceDeploymentOperationCoordinator,
        service_factory: DevelopmentSourceDeploymentServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="development-source-deploy"
        )
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise RuntimeError("deployment executor is closed")
            self._pool.submit(self._run, operation_id)

    def _run(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
            if lease is None:
                return
            self._service_factory(lease).deploy()
        except Exception:
            # Typed coordinator state is authoritative; the worker must survive.
            return

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        with suppress(Exception):
            self._pool.shutdown(wait=True, cancel_futures=False)


def invalidate_development_source_deployments(
    session: Session,
    instance_id: UUID,
    *,
    confidence: DevelopmentSourceDeploymentConfidence = (
        DevelopmentSourceDeploymentConfidence.STALE
    ),
) -> int:
    """Invalidate provenance inside the transaction preceding runtime mutation."""

    return DevelopmentSourceDeploymentRepository(session).mark_instance_confidence(
        instance_id, confidence
    )

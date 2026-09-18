"""Exact short-lived approval and one-shot repository replacement execution."""

from __future__ import annotations

import hashlib
import json
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

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.broker.operations import (
    ApplyRepositoryReplacementResult,
    InspectRepositoryReplacementResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationResult,
    AddonZipOrigin,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstance,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RepositoryReplacementIncumbentClass,
    RepositoryReplacementOperation,
    RepositoryReplacementOperationPhase,
    RepositoryReplacementOperationState,
    RepositoryReplacementPlan,
    RepositoryReplacementPlanState,
    RepositoryReplacementTerminalOutcome,
    RuntimeExecutionProfile,
    SnapshotState,
)
from kodi_lab.kodi.addons import (
    KodiAddonInventoryAdapter,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
)
from kodi_lab.kodi.readiness import (
    KodiReadinessCoordinator,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.repositories import (
    AddonRepositoryMetadataRepository,
    AddonRepositorySourceRepository,
    AddonZipArtifactRepository,
    AddonZipInstallationEvidenceRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceAddonRepositorySourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RepositoryReplacementOperationRepository,
    RepositoryReplacementPlanRepository,
    RuntimeExecutionProfileRepository,
    SnapshotRepository,
)

APPROVAL_TTL = timedelta(minutes=15)
QUALIFYING_SNAPSHOT_MAX_AGE = timedelta(minutes=15)
REPLACEMENT_LEASE = timedelta(minutes=15)
EXPECTED_KODI_TYPE = "xbmc.python.pluginsource"


class RepositoryReplacementError(RuntimeError):
    code = "repository_replacement_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class RepositoryReplacementConflictError(RepositoryReplacementError):
    code = "repository_replacement_conflict"


class RepositoryReplacementUnsupportedError(RepositoryReplacementError):
    code = "repository_replacement_unsupported"


class RepositoryReplacementLeaseLostError(RepositoryReplacementError):
    code = "repository_replacement_lease_lost"


class RepositoryReplacementPlanRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID
    state: RepositoryReplacementPlanState
    repository_logical_name: str = Field(max_length=120)
    development_source_logical_name: str = Field(max_length=120)
    addon_id: str = Field(max_length=128)
    addon_name: str = Field(max_length=256)
    incumbent_version: str | None = Field(default=None, max_length=128)
    incoming_version: str = Field(max_length=128)
    incumbent_class: RepositoryReplacementIncumbentClass
    enabled: bool | None
    snapshot_ready: bool
    expires_at: datetime
    confirmation: str = Field(max_length=640)
    retained_attachment_notice: str = Field(max_length=256)
    rollback_notice: str = Field(max_length=512)


class RepositoryReplacementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    plan_id: UUID
    managed_instance_id: UUID | None
    addon_id: str = Field(max_length=128)
    incoming_version: str = Field(max_length=128)
    incumbent_class: RepositoryReplacementIncumbentClass
    state: RepositoryReplacementOperationState
    phase: RepositoryReplacementOperationPhase
    application_started: bool
    stopped_readback_matched: bool
    final_readback_matched: bool
    recognition_enabled: bool | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool


class _ReplacementBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult: ...

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult: ...

    def inspect_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        expected_container_id: str,
    ) -> InspectRepositoryReplacementResult: ...

    def apply_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        replacement_operation_id: str,
        expected_container_id: str,
    ) -> ApplyRepositoryReplacementResult: ...


class _InventoryAdapter(Protocol):
    def get_inventory(self) -> KodiAddonInventoryResult: ...


class _Readiness(Protocol):
    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult: ...


type InventoryFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _InventoryAdapter
]
type ReadinessFactory = Callable[
    [_ReplacementBroker, RuntimeExecutionProfile], _Readiness
]


@dataclass(frozen=True, slots=True)
class RepositoryReplacementLease:
    operation_id: UUID
    plan_id: UUID
    instance_id: UUID
    token: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class RepositoryReplacementCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _ReplacementBroker,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        inventory_factory: InventoryFactory | None = None,
        reconciliation: ManagedInstanceReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._clock = clock
        self._inventory_factory = inventory_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._reconciliation = reconciliation or ManagedInstanceReconciliationService(
            session_factory, broker=broker
        )

    def prepare(
        self, instance_id: UUID, artifact_id: UUID
    ) -> RepositoryReplacementPlanRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            RepositoryReplacementPlanRepository(session).expire_due(
                instance_id, now=now
            )
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.REPOSITORY_REPLACEMENT
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            artifact = AddonZipArtifactRepository(session).get(artifact_id)
            active = RepositoryReplacementPlanRepository(session).active_for_instance(
                instance_id
            )
            active_operation = RepositoryReplacementOperationRepository(
                session
            ).active_for_instance(instance_id)
            mutation = ManagedInstanceMutationLeaseRepository(session).current(
                instance_id
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).current()
        if (
            active is not None
            or active_operation is not None
            or mutation is not None
            or heavy is not None
        ):
            raise RepositoryReplacementConflictError(
                "Repository replacement planning conflicts with active work."
            )
        if instance is None or artifact is None:
            raise RepositoryReplacementUnsupportedError(
                "Managed instance or repository artifact is unavailable."
            )
        if (
            instance.instance_type is not InstanceType.DISPOSABLE
            or artifact.origin is not AddonZipOrigin.REPOSITORY_DOWNLOAD
            or artifact.repository_source_id is None
            or artifact.repository_snapshot_id is None
            or artifact.repository_candidate_id is None
        ):
            raise RepositoryReplacementUnsupportedError(
                "Replacement requires a repository-download artifact and "
                "disposable instance."
            )
        _profile, _observation, container_id, inventory = self._running_state(instance)
        with self._session_factory() as session:
            repository = AddonRepositorySourceRepository(session).get(
                artifact.repository_source_id
            )
            repository_attachment = ManagedInstanceAddonRepositorySourceRepository(
                session
            ).get(instance_id, artifact.repository_source_id)
            candidate = AddonRepositoryMetadataRepository(session).get_candidate(
                artifact.repository_candidate_id
            )
            source_rows = ManagedInstanceDevelopmentSourceRepository(
                session
            ).list_for_instance(instance_id)
            sources = DevelopmentSourceRepository(session)
            matching = [
                (attachment, source)
                for attachment in source_rows
                if (source := sources.get(attachment.development_source_id)) is not None
                and source.addon_id == artifact.addon_id
            ]
            prior_install = AddonZipInstallationEvidenceRepository(session).get(
                instance_id, artifact.addon_id
            )
        if (
            repository is None
            or repository_attachment is None
            or candidate is None
            or candidate.repository_source_id != artifact.repository_source_id
            or candidate.snapshot_id != artifact.repository_snapshot_id
            or candidate.addon_id != artifact.addon_id
            or candidate.version != artifact.addon_version
        ):
            raise RepositoryReplacementUnsupportedError(
                "Repository artifact lineage or attachment is not exact."
            )
        if len(matching) != 1:
            raise RepositoryReplacementUnsupportedError(
                "Exactly one attached same-ID DevelopmentSource is required.",
                code="attached_development_source_required",
            )
        if prior_install is not None:
            raise RepositoryReplacementUnsupportedError(
                "A prior ZIP or repository installation is not eligible.",
                code="prior_installation_ineligible",
            )
        attachment, source = matching[0]
        live = next(
            (item for item in inventory.items if item.addon_id == artifact.addon_id),
            None,
        )
        with self._session_factory() as session:
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                instance_id, source.id
            )
            evidence_operation = (
                None
                if evidence is None
                else DevelopmentSourceDeploymentOperationRepository(session).get(
                    evidence.last_successful_operation_id
                )
            )
            snapshots = SnapshotRepository(session).list_for_instance(instance_id)
        if evidence is None:
            if live is not None:
                raise RepositoryReplacementUnsupportedError(
                    "Inventory-only incumbents are not eligible.",
                    code="inventory_only_incumbent",
                )
            incumbent_class = (
                RepositoryReplacementIncumbentClass.ATTACHED_UNMATERIALIZED
            )
            qualifying_snapshot = None
        else:
            if (
                evidence.confidence
                is not DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                or evidence.runtime_revision_id != instance.runtime_revision_id
                or evidence.trusted_container_id != container_id
                or evidence.addon_id != source.addon_id
                or evidence_operation is None
                or evidence_operation.state
                is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
                or live is None
                or live.version != evidence.observed_source_version
                or live.kodi_type != EXPECTED_KODI_TYPE
            ):
                raise RepositoryReplacementUnsupportedError(
                    "Copied incumbent evidence or live identity is not exact.",
                    code="copied_incumbent_not_verified",
                )
            qualifying_snapshot = next(
                (
                    item
                    for item in snapshots
                    if item.state is SnapshotState.AVAILABLE
                    and item.source_managed_instance_id == instance_id
                    and item.runtime_revision_id == instance.runtime_revision_id
                    and now - QUALIFYING_SNAPSHOT_MAX_AGE <= item.captured_at <= now
                ),
                None,
            )
            if qualifying_snapshot is None:
                raise RepositoryReplacementUnsupportedError(
                    "A recent available Snapshot is required.",
                    code="qualifying_snapshot_required",
                )
            incumbent_class = RepositoryReplacementIncumbentClass.VERIFIED_COPIED
        plan = RepositoryReplacementPlan(
            id=uuid4(),
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            repository_source_id=repository.id,
            repository_attachment_at=repository_attachment.attached_at,
            repository_snapshot_id=artifact.repository_snapshot_id,
            repository_candidate_id=artifact.repository_candidate_id,
            incoming_artifact_id=artifact.id,
            addon_id=artifact.addon_id,
            incumbent_class=incumbent_class,
            development_source_id=source.id,
            development_source_attachment_at=attachment.attached_at,
            development_deployment_operation_id=(
                None if evidence is None else evidence.last_successful_operation_id
            ),
            development_source_manifest_digest=(
                None if evidence is None else evidence.source_manifest_digest
            ),
            development_destination_digest=(
                None if evidence is None else evidence.verified_destination_digest
            ),
            incumbent_version=None if live is None else live.version,
            incumbent_type=None if live is None else live.kodi_type,
            incumbent_enabled=None if live is None else live.enabled,
            runtime_revision_id=instance.runtime_revision_id,
            expected_container_id=container_id,
            qualifying_snapshot_id=(
                None if qualifying_snapshot is None else qualifying_snapshot.id
            ),
            incoming_version=artifact.addon_version,
            incoming_payload_manifest_digest=artifact.payload_manifest_digest,
            created_at=now,
            expires_at=now + APPROVAL_TTL,
            state=RepositoryReplacementPlanState.PENDING,
        )
        try:
            with self._session_factory.begin() as session:
                RepositoryReplacementPlanRepository(session).create(plan)
        except IntegrityError as error:
            raise RepositoryReplacementConflictError(
                "A repository replacement plan is already active."
            ) from error
        try:
            inspection = self.inspect(plan)
            expected = (
                "absent_eligible"
                if incumbent_class
                is RepositoryReplacementIncumbentClass.ATTACHED_UNMATERIALIZED
                else "overwrite_add_eligible"
            )
            if inspection.topology != expected:
                code = {
                    "exact_payload_noop": "exact_payload_noop",
                    "deletion_required": "unsupported_replacement_topology",
                    "type_transition": "unsupported_replacement_topology",
                    "incumbent_drift": "incumbent_state_drift",
                }.get(inspection.topology, "replacement_topology_invalid")
                raise RepositoryReplacementUnsupportedError(
                    "Repository replacement topology is not eligible.", code=code
                )
        except Exception as error:
            reason = (
                error.code
                if isinstance(error, RepositoryReplacementError)
                else "replacement_plan_inspection_failed"
            )
            with self._session_factory.begin() as session:
                RepositoryReplacementPlanRepository(session).invalidate(
                    plan.id, reason=reason
                )
            raise
        return self._plan_read(
            plan, repository.logical_name, source.logical_name, artifact.addon_name
        )

    def approve_and_queue(
        self, instance_id: UUID, plan_id: UUID
    ) -> RepositoryReplacementStatus:
        now = self._clock()
        with self._session_factory.begin() as session:
            plans = RepositoryReplacementPlanRepository(session)
            plans.expire_due(instance_id, now=now)
            plan = plans.get(plan_id)
        if plan is None or plan.managed_instance_id != instance_id:
            raise RepositoryReplacementUnsupportedError(
                "Replacement plan is unavailable."
            )
        if (
            plan.state is not RepositoryReplacementPlanState.PENDING
            or plan.expires_at <= now
        ):
            raise RepositoryReplacementConflictError(
                "Replacement plan is expired, consumed, or invalidated."
            )
        operation = RepositoryReplacementOperation(
            id=uuid4(),
            plan_id=plan.id,
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            incoming_artifact_id=plan.incoming_artifact_id,
            development_source_id=plan.development_source_id,
            addon_id=plan.addon_id,
            incoming_version=plan.incoming_version,
            incumbent_class=plan.incumbent_class,
            runtime_revision_id=plan.runtime_revision_id,
            expected_container_id=plan.expected_container_id,
            requested_at=now,
            state=RepositoryReplacementOperationState.QUEUED,
            phase=RepositoryReplacementOperationPhase.QUEUED,
        )
        try:
            with self._session_factory.begin() as session:
                plans = RepositoryReplacementPlanRepository(session)
                if not plans.approve_and_bind(plan.id, operation.id, now=now):
                    raise RepositoryReplacementConflictError(
                        "Replacement approval could not be bound."
                    )
                RepositoryReplacementOperationRepository(session).create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.REPOSITORY_REPLACEMENT,
                    operation_id=operation.id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.REPOSITORY_REPLACEMENT,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise RepositoryReplacementConflictError(
                "Another managed mutation or global-heavy operation is active."
            ) from error
        return self.to_status(operation)

    def acquire(self, operation_id: UUID) -> RepositoryReplacementLease | None:
        with self._session_factory() as session:
            operation = RepositoryReplacementOperationRepository(session).get(
                operation_id
            )
        if (
            operation is None
            or operation.managed_instance_id is None
            or operation.state is not RepositoryReplacementOperationState.QUEUED
        ):
            return None
        lease = RepositoryReplacementLease(
            operation.id,
            operation.plan_id,
            operation.managed_instance_id,
            secrets.token_urlsafe(32),
        )
        now = self._clock()
        expires = now + REPLACEMENT_LEASE
        with self._session_factory.begin() as session:
            claimed = RepositoryReplacementOperationRepository(session).acquire(
                operation.id, token_digest=lease.digest, now=now, expires_at=expires
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                lease.instance_id,
                operation_id=operation.id,
                token_digest=lease.digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=lease.digest,
                now=now,
                expires_at=expires,
            )
            if not (claimed and mutation and heavy):
                raise RepositoryReplacementLeaseLostError(
                    "Repository replacement authority could not be acquired."
                )
        return lease

    def update(self, lease: RepositoryReplacementLease, **values: object) -> None:
        now = self._clock()
        expires = now + REPLACEMENT_LEASE
        values["heartbeat_at"] = now
        values["lease_expires_at"] = expires
        with self._session_factory.begin() as session:
            changed = RepositoryReplacementOperationRepository(session).update_fields(
                lease.operation_id, token_digest=lease.digest, **values
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).renew(
                operation_id=lease.operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=expires,
            )
            if not (changed and mutation and heavy):
                raise RepositoryReplacementLeaseLostError(
                    "Repository replacement authority was lost."
                )

    def consume_application(self, lease: RepositoryReplacementLease) -> None:
        now = self._clock()
        operation = self._operation(lease.operation_id)
        with self._session_factory.begin() as session:
            plans = RepositoryReplacementPlanRepository(session)
            operations = RepositoryReplacementOperationRepository(session)
            if not plans.consume_for_application(lease.plan_id, lease.operation_id):
                raise RepositoryReplacementLeaseLostError(
                    "Repository replacement approval could not be consumed."
                )
            if not operations.update_fields(
                lease.operation_id,
                token_digest=lease.digest,
                application_started=True,
                phase=RepositoryReplacementOperationPhase.APPLYING,
                heartbeat_at=now,
                lease_expires_at=now + REPLACEMENT_LEASE,
            ):
                raise RepositoryReplacementLeaseLostError(
                    "Repository replacement application marker was not durable."
                )
            DevelopmentSourceDeploymentRepository(session).mark_confidence(
                lease.instance_id,
                operation.development_source_id,
                DevelopmentSourceDeploymentConfidence.UNKNOWN,
            )

    def now(self) -> datetime:
        return self._clock()

    def finish_success(
        self,
        lease: RepositoryReplacementLease,
        *,
        plan: RepositoryReplacementPlan,
        artifact: AddonZipArtifact,
    ) -> None:
        now = self._clock()
        evidence = AddonZipInstallationEvidence(
            managed_instance_id=lease.instance_id,
            original_managed_instance_id=lease.instance_id,
            artifact_id=artifact.id,
            installation_operation_id=None,
            replacement_operation_id=lease.operation_id,
            runtime_revision_id=plan.runtime_revision_id,
            trusted_container_id=plan.expected_container_id,
            addon_id=artifact.addon_id,
            addon_version=artifact.addon_version,
            payload_manifest_digest=artifact.payload_manifest_digest,
            verified_at=now,
            confidence=AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION,
            result=AddonZipInstallationResult.REPOSITORY_REPLACEMENT_RECOGNIZED,
            artifact_origin=AddonZipOrigin.REPOSITORY_DOWNLOAD,
            repository_source_id=artifact.repository_source_id,
            repository_snapshot_id=artifact.repository_snapshot_id,
            repository_candidate_id=artifact.repository_candidate_id,
        )
        with self._session_factory.begin() as session:
            AddonZipInstallationEvidenceRepository(session).upsert(evidence)
            DevelopmentSourceDeploymentRepository(session).mark_confidence(
                lease.instance_id,
                plan.development_source_id,
                DevelopmentSourceDeploymentConfidence.STALE,
            )
            self._terminalize(
                session,
                lease,
                RepositoryReplacementOperationState.SUCCEEDED,
                RepositoryReplacementTerminalOutcome.SUCCEEDED,
                None,
                now,
            )

    def finish_failure(
        self,
        lease: RepositoryReplacementLease,
        *,
        reason: str,
        reconciliation_required: bool,
        plan: RepositoryReplacementPlan | None,
        artifact: AddonZipArtifact | None,
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            if reconciliation_required and plan is not None and artifact is not None:
                AddonZipInstallationEvidenceRepository(session).upsert(
                    AddonZipInstallationEvidence(
                        managed_instance_id=lease.instance_id,
                        original_managed_instance_id=lease.instance_id,
                        artifact_id=artifact.id,
                        installation_operation_id=None,
                        replacement_operation_id=lease.operation_id,
                        runtime_revision_id=plan.runtime_revision_id,
                        trusted_container_id=plan.expected_container_id,
                        addon_id=artifact.addon_id,
                        addon_version=artifact.addon_version,
                        payload_manifest_digest=artifact.payload_manifest_digest,
                        verified_at=now,
                        confidence=AddonZipInstallationConfidence.UNKNOWN,
                        result=AddonZipInstallationResult.REPOSITORY_REPLACEMENT_RECOGNIZED,
                        artifact_origin=AddonZipOrigin.REPOSITORY_DOWNLOAD,
                        repository_source_id=artifact.repository_source_id,
                        repository_snapshot_id=artifact.repository_snapshot_id,
                        repository_candidate_id=artifact.repository_candidate_id,
                    )
                )
                DevelopmentSourceDeploymentRepository(session).mark_confidence(
                    lease.instance_id,
                    plan.development_source_id,
                    DevelopmentSourceDeploymentConfidence.UNKNOWN,
                )
            elif plan is not None:
                RepositoryReplacementPlanRepository(session).invalidate(
                    plan.id, reason=reason
                )
            self._terminalize(
                session,
                lease,
                RepositoryReplacementOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else RepositoryReplacementOperationState.FAILED,
                RepositoryReplacementTerminalOutcome.RECONCILIATION_REQUIRED
                if reconciliation_required
                else RepositoryReplacementTerminalOutcome.FAILED,
                reason[:256],
                now,
            )

    def _terminalize(
        self,
        session: Session,
        lease: RepositoryReplacementLease,
        state: RepositoryReplacementOperationState,
        outcome: RepositoryReplacementTerminalOutcome,
        reason: str | None,
        now: datetime,
    ) -> None:
        if not RepositoryReplacementOperationRepository(session).finish(
            lease.operation_id,
            token_digest=lease.digest,
            state=state,
            now=now,
            reason=reason,
        ):
            raise RepositoryReplacementLeaseLostError(
                "Repository replacement finalization could not be proven."
            )
        RepositoryReplacementPlanRepository(session).finish(
            lease.plan_id, outcome=outcome, reason=reason
        )
        if not ManagedInstanceMutationLeaseRepository(session).release(
            lease.instance_id,
            operation_id=lease.operation_id,
            token_digest=lease.digest,
        ) or not GlobalHeavyOperationLeaseRepository(session).release(
            operation_id=lease.operation_id, token_digest=lease.digest
        ):
            raise RepositoryReplacementLeaseLostError(
                "Repository replacement lease release could not be proven."
            )

    def get_plan(self, plan_id: UUID) -> RepositoryReplacementPlanRead:
        with self._session_factory() as session:
            plan = RepositoryReplacementPlanRepository(session).get(plan_id)
            if plan is None:
                raise RepositoryReplacementUnsupportedError(
                    "Replacement plan is unavailable."
                )
            repository = AddonRepositorySourceRepository(session).get(
                plan.repository_source_id
            )
            source = DevelopmentSourceRepository(session).get(
                plan.development_source_id
            )
            artifact = AddonZipArtifactRepository(session).get(
                plan.incoming_artifact_id
            )
        if repository is None or source is None or artifact is None:
            raise RepositoryReplacementUnsupportedError(
                "Replacement plan display state is unavailable."
            )
        return self._plan_read(
            plan, repository.logical_name, source.logical_name, artifact.addon_name
        )

    def get_plan_for(
        self, instance_id: UUID, plan_id: UUID
    ) -> RepositoryReplacementPlanRead:
        with self._session_factory() as session:
            plan = RepositoryReplacementPlanRepository(session).get(plan_id)
        if plan is None or plan.original_managed_instance_id != instance_id:
            raise RepositoryReplacementUnsupportedError(
                "Replacement plan is unavailable."
            )
        return self.get_plan(plan_id)

    def get(self, operation_id: UUID) -> RepositoryReplacementStatus:
        operation = self._operation(operation_id)
        return self.to_status(operation)

    def get_for_instance(
        self, instance_id: UUID, operation_id: UUID
    ) -> RepositoryReplacementStatus:
        operation = self._operation(operation_id)
        if operation.original_managed_instance_id != instance_id:
            raise RepositoryReplacementUnsupportedError(
                "Replacement operation is unavailable."
            )
        return self.to_status(operation)

    def queued(self) -> list[RepositoryReplacementOperation]:
        with self._session_factory() as session:
            return RepositoryReplacementOperationRepository(session).list_queued()

    def _operation(self, operation_id: UUID) -> RepositoryReplacementOperation:
        with self._session_factory() as session:
            operation = RepositoryReplacementOperationRepository(session).get(
                operation_id
            )
        if operation is None:
            raise RepositoryReplacementUnsupportedError(
                "Replacement operation is unavailable."
            )
        return operation

    def _running_state(
        self, instance: ManagedInstance
    ) -> tuple[
        RuntimeExecutionProfile,
        ObserveManagedKodiRuntimeResult,
        str,
        KodiAddonInventoryResult,
    ]:
        with self._session_factory() as session:
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
        if (
            profile is None
            or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
        ):
            raise RepositoryReplacementUnsupportedError(
                "Replacement runtime is unsupported."
            )
        containers = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance.target_id)
        ]
        if (
            len(containers) != 1
            or containers[0].runtime_state is not ContainerRuntimeState.RUNNING
            or containers[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise RepositoryReplacementUnsupportedError(
                "Replacement requires one healthy running container."
            )
        container = containers[0]
        observation = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=str(instance.target_id),
            expected_container_id=container.container_id,
        )
        if (
            observation.runtime_revision_id != str(instance.runtime_revision_id)
            or observation.container_id != container.container_id
            or observation.runtime_state is not ContainerRuntimeState.RUNNING
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            raise RepositoryReplacementUnsupportedError(
                "Running replacement identity is not exact."
            )
        inventory = self._inventory_factory(observation, profile).get_inventory()
        if inventory.identity.kodi_release != profile.kodi_release:
            raise RepositoryReplacementUnsupportedError(
                "Kodi inventory identity is not exact."
            )
        return profile, observation, container.container_id, inventory

    def inspect(
        self, plan: RepositoryReplacementPlan
    ) -> InspectRepositoryReplacementResult:
        return self._broker.inspect_repository_replacement(
            managed_instance_id=str(plan.original_managed_instance_id),
            approval_id=str(plan.id),
            expected_container_id=plan.expected_container_id,
        )

    @staticmethod
    def _plan_read(
        plan: RepositoryReplacementPlan,
        repository_name: str,
        source_name: str,
        addon_name: str,
    ) -> RepositoryReplacementPlanRead:
        return RepositoryReplacementPlanRead(
            plan_id=plan.id,
            state=plan.state,
            repository_logical_name=repository_name,
            development_source_logical_name=source_name,
            addon_id=plan.addon_id,
            addon_name=addon_name,
            incumbent_version=plan.incumbent_version,
            incoming_version=plan.incoming_version,
            incumbent_class=plan.incumbent_class,
            enabled=plan.incumbent_enabled,
            snapshot_ready=plan.qualifying_snapshot_id is not None,
            expires_at=plan.expires_at,
            confirmation=(
                f"Repository package {plan.incoming_version} will replace the current "
                f"DevelopmentSource installation for {addon_name} on this "
                "disposable instance."
            ),
            retained_attachment_notice=(
                "The DevelopmentSource remains attached. Copy/sync is blocked while "
                "the repository replacement is active."
            ),
            rollback_notice=(
                "Replacement does not automatically roll back. Restore the bound "
                "Snapshot to return to prior instance state; restored "
                "DevelopmentSource bytes do not "
                "automatically regain verified provenance."
                if plan.incumbent_class
                is RepositoryReplacementIncumbentClass.VERIFIED_COPIED
                else (
                    "Replacement does not automatically roll back. Reset the "
                    "disposable instance to exit."
                )
            ),
        )

    @staticmethod
    def to_status(
        operation: RepositoryReplacementOperation,
    ) -> RepositoryReplacementStatus:
        return RepositoryReplacementStatus(
            operation_id=operation.id,
            plan_id=operation.plan_id,
            managed_instance_id=operation.managed_instance_id,
            addon_id=operation.addon_id,
            incoming_version=operation.incoming_version,
            incumbent_class=operation.incumbent_class,
            state=operation.state,
            phase=operation.phase,
            application_started=operation.application_started,
            stopped_readback_matched=operation.stopped_readback_matched,
            final_readback_matched=operation.final_readback_matched,
            recognition_enabled=operation.recognition_enabled,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
            reconciliation_required=operation.reconciliation_required,
        )


class RepositoryReplacementService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _ReplacementBroker,
        coordinator: RepositoryReplacementCoordinator,
        lease: RepositoryReplacementLease,
        *,
        inventory_factory: InventoryFactory | None = None,
        readiness_factory: ReadinessFactory = KodiReadinessCoordinator,
        after_application_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease
        self._inventory_factory = inventory_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._readiness_factory = readiness_factory
        self._after_application_hook = after_application_hook

    def execute(self) -> None:
        stopped = False
        application_started = False
        plan: RepositoryReplacementPlan | None = None
        artifact: AddonZipArtifact | None = None
        profile: RuntimeExecutionProfile | None = None
        try:
            plan, artifact, instance, profile = self._load_exact()
            observation = self._running_observation(instance)
            before = self._inventory(observation, profile)
            self._require_incumbent(plan, before.items)
            inspection = self._coordinator.inspect(plan)
            self._require_topology(plan, inspection.topology)
            baseline = self._dependency_state(artifact, before.items)
            self._coordinator.update(
                self._lease,
                dependency_baseline_json=self._dependency_json(baseline),
                phase=RepositoryReplacementOperationPhase.STOPPING,
            )
            result = self._broker.stop_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=plan.expected_container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
            if (
                result.container_id != plan.expected_container_id
                or result.runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise RepositoryReplacementError("Exact container did not stop.")
            stopped = True
            self._coordinator.update(
                self._lease,
                phase=RepositoryReplacementOperationPhase.REVALIDATE_STOPPED,
            )
            stopped_inspection = self._coordinator.inspect(plan)
            self._require_topology(plan, stopped_inspection.topology)
            self._coordinator.consume_application(self._lease)
            application_started = True
            applied = self._broker.apply_repository_replacement(
                managed_instance_id=str(self._lease.instance_id),
                approval_id=str(plan.id),
                replacement_operation_id=str(self._lease.operation_id),
                expected_container_id=plan.expected_container_id,
            )
            if not applied.matched:
                raise RepositoryReplacementError("Stopped replacement readback failed.")
            if self._after_application_hook is not None:
                self._after_application_hook()
            self._coordinator.update(
                self._lease,
                stopped_readback_matched=True,
                phase=RepositoryReplacementOperationPhase.RESTARTING,
            )
            started = self._broker.start_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=plan.expected_container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if (
                started.container_id != plan.expected_container_id
                or started.runtime_state is not ContainerRuntimeState.RUNNING
            ):
                raise RepositoryReplacementError("Exact container did not restart.")
            stopped = False
            self._coordinator.update(
                self._lease, phase=RepositoryReplacementOperationPhase.READINESS
            )
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(self._lease.instance_id))
            if readiness.usability_state is not UsabilityState.USABLE:
                raise RepositoryReplacementError(
                    "Restarted Kodi did not become usable."
                )
            self._coordinator.update(
                self._lease, phase=RepositoryReplacementOperationPhase.RECOGNITION
            )
            observation = self._running_observation(instance)
            after = self._inventory(observation, profile)
            recognized = next(
                (item for item in after.items if item.addon_id == artifact.addon_id),
                None,
            )
            expected_enabled = (
                False if plan.incumbent_enabled is None else plan.incumbent_enabled
            )
            if (
                recognized is None
                or recognized.version != artifact.addon_version
                or recognized.kodi_type != EXPECTED_KODI_TYPE
                or recognized.enabled is not expected_enabled
            ):
                raise RepositoryReplacementError(
                    "Kodi replacement recognition did not match."
                )
            self._coordinator.update(
                self._lease, recognition_enabled=recognized.enabled
            )
            post = self._dependency_state(artifact, after.items)
            self._coordinator.update(
                self._lease, dependency_post_json=self._dependency_json(post)
            )
            if post != baseline:
                raise RepositoryReplacementError("Declared dependency state changed.")
            self._coordinator.update(
                self._lease, phase=RepositoryReplacementOperationPhase.VERIFY_FINAL
            )
            final = self._coordinator.inspect(plan)
            if final.topology != "exact_payload_noop":
                raise RepositoryReplacementError(
                    "Final replacement readback did not match."
                )
            self._coordinator.update(self._lease, final_readback_matched=True)
            self._coordinator.finish_success(self._lease, plan=plan, artifact=artifact)
        except Exception as error:
            if plan is None:
                with self._session_factory() as session:
                    plan = RepositoryReplacementPlanRepository(session).get(
                        self._lease.plan_id
                    )
                    artifact = (
                        None
                        if plan is None
                        else AddonZipArtifactRepository(session).get(
                            plan.incoming_artifact_id
                        )
                    )
            if application_started:
                self._leave_stopped(plan)
            elif stopped and plan is not None and profile is not None:
                self._restore_running(plan, profile)
            reason = (
                error.code
                if isinstance(error, (RepositoryReplacementError, RemoteBrokerError))
                else "repository_replacement_failed"
            )
            with suppress(Exception):
                self._coordinator.finish_failure(
                    self._lease,
                    reason=reason,
                    reconciliation_required=application_started,
                    plan=plan,
                    artifact=artifact,
                )
            raise

    def _load_exact(
        self,
    ) -> tuple[
        RepositoryReplacementPlan,
        AddonZipArtifact,
        ManagedInstance,
        RuntimeExecutionProfile,
    ]:
        now = self._coordinator.now()
        with self._session_factory() as session:
            plan = RepositoryReplacementPlanRepository(session).get(self._lease.plan_id)
            operation = RepositoryReplacementOperationRepository(session).get(
                self._lease.operation_id
            )
            instance = ManagedInstanceRepository(session).get(self._lease.instance_id)
            artifact = (
                None
                if plan is None
                else AddonZipArtifactRepository(session).get(plan.incoming_artifact_id)
            )
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
            source_attachment = (
                None
                if plan is None
                else ManagedInstanceDevelopmentSourceRepository(session).get(
                    self._lease.instance_id, plan.development_source_id
                )
            )
            repository_attachment = (
                None
                if plan is None
                else ManagedInstanceAddonRepositorySourceRepository(session).get(
                    self._lease.instance_id, plan.repository_source_id
                )
            )
            snapshot = (
                None
                if plan is None or plan.qualifying_snapshot_id is None
                else SnapshotRepository(session).get(plan.qualifying_snapshot_id)
            )
        if (
            plan is None
            or operation is None
            or instance is None
            or artifact is None
            or profile is None
            or source_attachment is None
            or repository_attachment is None
        ):
            raise RepositoryReplacementUnsupportedError(
                "Exact replacement state is unavailable."
            )
        if (
            plan.state is not RepositoryReplacementPlanState.APPROVED
            or plan.expires_at <= now
            or plan.consumed_execution_operation_id != operation.id
            or operation.plan_id != plan.id
            or operation.expected_container_id != plan.expected_container_id
            or instance.runtime_revision_id != plan.runtime_revision_id
            or source_attachment.attached_at != plan.development_source_attachment_at
            or repository_attachment.attached_at != plan.repository_attachment_at
        ):
            raise RepositoryReplacementConflictError(
                "Replacement plan state changed.", code="replacement_plan_invalidated"
            )
        if (
            plan.incumbent_class is RepositoryReplacementIncumbentClass.VERIFIED_COPIED
            and (
                snapshot is None
                or snapshot.state is not SnapshotState.AVAILABLE
                or snapshot.source_managed_instance_id != instance.target_id
                or snapshot.runtime_revision_id != instance.runtime_revision_id
                or snapshot.captured_at > plan.created_at
                or plan.created_at - snapshot.captured_at > QUALIFYING_SNAPSHOT_MAX_AGE
            )
        ):
            raise RepositoryReplacementConflictError(
                "Qualifying Snapshot changed.", code="qualifying_snapshot_invalidated"
            )
        return plan, artifact, instance, profile

    def _running_observation(
        self, instance: ManagedInstance
    ) -> ObserveManagedKodiRuntimeResult:
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance.target_id)
        ]
        if (
            len(matches) != 1
            or matches[0].container_id != self._operation_container()
            or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            or matches[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise RepositoryReplacementConflictError(
                "Managed container state changed.", code="replacement_container_changed"
            )
        observation = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=str(instance.target_id),
            expected_container_id=matches[0].container_id,
        )
        if (
            observation.container_id != matches[0].container_id
            or observation.runtime_revision_id != str(instance.runtime_revision_id)
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            raise RepositoryReplacementConflictError(
                "Managed runtime state changed.", code="replacement_runtime_changed"
            )
        return observation

    def _operation_container(self) -> str:
        with self._session_factory() as session:
            operation = RepositoryReplacementOperationRepository(session).get(
                self._lease.operation_id
            )
        if operation is None:
            raise RepositoryReplacementLeaseLostError(
                "Replacement operation is unavailable."
            )
        return operation.expected_container_id

    def _inventory(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
    ) -> KodiAddonInventoryResult:
        result = self._inventory_factory(observation, profile).get_inventory()
        if result.identity.kodi_release != profile.kodi_release:
            raise RepositoryReplacementConflictError("Kodi inventory identity changed.")
        return result

    @staticmethod
    def _require_incumbent(
        plan: RepositoryReplacementPlan, items: tuple[KodiAddonInventoryRecord, ...]
    ) -> None:
        current = next((item for item in items if item.addon_id == plan.addon_id), None)
        if (
            plan.incumbent_class
            is RepositoryReplacementIncumbentClass.ATTACHED_UNMATERIALIZED
        ):
            if current is not None:
                raise RepositoryReplacementConflictError(
                    "Unmaterialized incumbent appeared.",
                    code="replacement_inventory_changed",
                )
        elif (
            current is None
            or current.version != plan.incumbent_version
            or current.kodi_type != plan.incumbent_type
            or current.enabled is not plan.incumbent_enabled
        ):
            raise RepositoryReplacementConflictError(
                "Copied incumbent identity changed.",
                code="replacement_inventory_changed",
            )

    @staticmethod
    def _require_topology(plan: RepositoryReplacementPlan, topology: str) -> None:
        expected = (
            "absent_eligible"
            if plan.incumbent_class
            is RepositoryReplacementIncumbentClass.ATTACHED_UNMATERIALIZED
            else "overwrite_add_eligible"
        )
        if topology != expected:
            raise RepositoryReplacementConflictError(
                "Replacement topology changed.", code="replacement_topology_changed"
            )

    @staticmethod
    def _dependency_state(
        artifact: AddonZipArtifact, inventory: tuple[KodiAddonInventoryRecord, ...]
    ) -> tuple[tuple[str, bool, bool | None, str | None], ...]:
        by_id = {item.addon_id: item for item in inventory}
        return tuple(
            (
                dependency.dependency_id,
                dependency.dependency_id in by_id,
                None
                if dependency.dependency_id not in by_id
                else by_id[dependency.dependency_id].enabled,
                None
                if dependency.dependency_id not in by_id
                else by_id[dependency.dependency_id].version,
            )
            for dependency in artifact.dependencies
        )

    @staticmethod
    def _dependency_json(
        state: tuple[tuple[str, bool, bool | None, str | None], ...],
    ) -> str:
        value = json.dumps(
            [
                {
                    "id": item[0],
                    "present": item[1],
                    "enabled": item[2],
                    "version": item[3],
                }
                for item in state
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(value.encode()) > 16 * 1024:
            raise RepositoryReplacementUnsupportedError(
                "Dependency evidence exceeds bound."
            )
        return value

    def _restore_running(
        self, plan: RepositoryReplacementPlan, profile: RuntimeExecutionProfile
    ) -> None:
        with suppress(Exception):
            result = self._broker.start_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=plan.expected_container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if result.runtime_state is ContainerRuntimeState.RUNNING:
                self._readiness_factory(self._broker, profile).wait_until_usable(
                    str(self._lease.instance_id)
                )

    def _leave_stopped(self, plan: RepositoryReplacementPlan | None) -> None:
        if plan is None:
            return
        with suppress(Exception):
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(self._lease.instance_id)
                and item.container_id == plan.expected_container_id
            ]
            if (
                len(matches) == 1
                and matches[0].runtime_state is ContainerRuntimeState.RUNNING
            ):
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.instance_id),
                    expected_container_id=plan.expected_container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )


type RepositoryReplacementServiceFactory = Callable[
    [RepositoryReplacementLease], RepositoryReplacementService
]


class RepositoryReplacementExecutor:
    def __init__(
        self,
        coordinator: RepositoryReplacementCoordinator,
        service_factory: RepositoryReplacementServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="repository-replacement"
        )
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise RuntimeError("repository replacement executor is closed")
            self._pool.submit(self._run, operation_id)

    def _run(self, operation_id: UUID) -> None:
        with suppress(Exception):
            lease = self._coordinator.acquire(operation_id)
            if lease is not None:
                self._service_factory(lease).execute()

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=True, cancel_futures=False)

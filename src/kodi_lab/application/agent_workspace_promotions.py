"""Web-reviewed three-way workspace promotion into a DevelopmentSource."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspacePromotionChangeKind,
    AgentWorkspacePromotionHandoff,
    AgentWorkspacePromotionJournalState,
    AgentWorkspacePromotionOperation,
    AgentWorkspacePromotionOperationState,
    AgentWorkspacePromotionPhase,
    AgentWorkspacePromotionPlan,
    AgentWorkspacePromotionPlanState,
    AgentWorkspaceState,
    ConnectionState,
    DevelopmentSource,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceEnablementOperationState,
    GlobalHeavyOperationKind,
    HealthStatus,
    InstanceType,
    ManagedInstanceMutationKind,
    TargetAvailability,
)
from kodi_lab.storage.models import AgentWorkspacePromotionPlanPathRow
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionHandoffRepository,
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourcePromotionLeaseRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    TargetRepository,
)
from kodi_lab.workspace import (
    AgentWorkspacePromotionStorageError,
    PromotionPathMaterial,
    execute_promotion,
    prepare_promotion_review,
    prove_promotion_preimages,
)

PROMOTION_PLAN_TTL = timedelta(minutes=15)
PROMOTION_LEASE_TTL = timedelta(minutes=15)


class AgentWorkspacePromotionError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class AgentWorkspacePromotionConflictError(AgentWorkspacePromotionError):
    pass


class AgentWorkspacePromotionChangeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=512)
    change_kind: AgentWorkspacePromotionChangeKind
    before_text: str | None
    after_text: str
    before_bytes: int = Field(ge=0)
    after_bytes: int = Field(ge=0)
    conflict_reason: str | None = Field(default=None, max_length=64)


class AgentWorkspacePromotionPlanRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID
    workspace_id: UUID
    state: AgentWorkspacePromotionPlanState
    workspace_logical_name: str = Field(max_length=120)
    source_logical_name: str = Field(max_length=120)
    verification_target_id: UUID
    verification_target_name: str = Field(max_length=256)
    verification_target_available: bool = True
    addon_id: str = Field(max_length=128)
    current_version: str = Field(max_length=128)
    promoted_version: str = Field(max_length=128)
    source_write_permission: str
    changed_file_count: int = Field(ge=1, le=100)
    review_text_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    review_file_limit: int = 100
    review_text_limit_bytes: int = 4 * 1024 * 1024
    expires_at: datetime | None
    changes: list[AgentWorkspacePromotionChangeRead] = Field(max_length=100)
    rollback_notice: str = Field(max_length=512)
    kodi_verification_notice: str = Field(max_length=512)


class AgentWorkspacePromotionOperationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    plan_id: UUID
    workspace_id: UUID
    verification_target_id: UUID
    verification_target_available: bool = True
    state: AgentWorkspacePromotionOperationState
    phase: AgentWorkspacePromotionPhase
    application_started: bool
    backup_verified: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    reconciliation_required: bool
    handoff_published: bool


class AgentWorkspacePromotionTargetRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str = Field(max_length=256)


@dataclass(frozen=True, slots=True)
class _TargetBinding:
    name: str
    runtime_revision_id: UUID
    container_id: str
    deployment_operation_id: UUID
    source_digest: str
    destination_digest: str
    expected_enabled: bool


@dataclass(frozen=True, slots=True)
class _Lease:
    operation_id: UUID
    plan_id: UUID
    workspace_id: UUID
    source_id: UUID
    target_id: UUID
    token: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AgentWorkspacePromotionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        development_root: Path | None,
        verification_publisher: Callable[[Session, UUID], UUID],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        reconciliation_gate: Callable[[UUID], None] | None = None,
        failure_injector: Callable[[str], None] = lambda _phase: None,
        verification_starter: Callable[[UUID], object] | None = None,
        enabled_state_reader: Callable[[UUID, str], bool] | None = None,
    ) -> None:
        if verification_publisher is None:
            raise ValueError("verification_publisher is required")
        self._session_factory = session_factory
        self._data_root = data_root
        self._development_root = development_root
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._reconciliation_gate = reconciliation_gate
        self._failure_injector = failure_injector
        self._verification_publisher = verification_publisher
        self._verification_starter = verification_starter
        self._enabled_state_reader = enabled_state_reader

    def _workspace_and_source(
        self, workspace_id: UUID
    ) -> tuple[AgentWorkspace, DevelopmentSource]:
        with self._session_factory() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
        if workspace is None:
            raise AgentWorkspacePromotionError(
                "agent_workspace_not_found", "Agent workspace was not found."
            )
        if workspace.bytes_state is AgentWorkspaceBytesState.CLEANED:
            raise AgentWorkspacePromotionConflictError(
                "workspace_bytes_cleaned",
                "Workspace bytes have been permanently cleaned.",
            )
        if workspace.state is not AgentWorkspaceState.READY:
            raise AgentWorkspacePromotionConflictError(
                "promotion_workspace_not_ready",
                "Only a ready workspace may be promoted.",
            )
        if source is None:
            raise AgentWorkspacePromotionConflictError(
                "promotion_source_missing", "Development source is unavailable."
            )
        with self._session_factory() as session:
            unresolved = AgentWorkspacePromotionOperationRepository(
                session
            ).reconciliation_for_source(source.id)
        if unresolved:
            raise AgentWorkspacePromotionConflictError(
                "promotion_source_reconciliation_required",
                "Development source has an unresolved promotion.",
            )
        return workspace, source

    def _target_binding(
        self,
        target_id: UUID,
        source_id: UUID,
        *,
        permit_reserved_operation: UUID | None = None,
    ) -> _TargetBinding:
        if self._reconciliation_gate is not None:
            self._reconciliation_gate(target_id)
        with self._session_factory() as session:
            target = TargetRepository(session).get(target_id)
            instance = ManagedInstanceRepository(session).get(target_id)
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                target_id, source_id
            )
            deployment = DevelopmentSourceDeploymentRepository(session).get(
                target_id, source_id
            )
            operation = (
                None
                if deployment is None
                else DevelopmentSourceDeploymentOperationRepository(session).get(
                    deployment.last_successful_operation_id
                )
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).current(
                target_id
            )
            enablements = DevelopmentSourceEnablementOperationRepository(
                session
            ).list_for_original_instance(target_id)
        if (
            target is None
            or instance is None
            or attachment is None
            or deployment is None
            or operation is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or target.availability is not TargetAvailability.AVAILABLE
            or target.health is not HealthStatus.HEALTHY
            or target.connection_state is not ConnectionState.CONNECTED
            or deployment.confidence
            is not DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
            or deployment.runtime_revision_id != instance.runtime_revision_id
            or operation.state
            is not DevelopmentSourceDeploymentOperationState.SUCCEEDED
            or (
                mutation is not None
                and mutation.operation_id
                != (
                    None
                    if permit_reserved_operation is None
                    else str(permit_reserved_operation)
                )
            )
        ):
            raise AgentWorkspacePromotionConflictError(
                "promotion_target_ineligible",
                "Selected verification target no longer has exact eligible "
                "deployment state.",
            )
        enabled = next(
            (
                item.result_enabled
                for item in enablements
                if item.development_source_id == source_id
                and item.state is DevelopmentSourceEnablementOperationState.SUCCEEDED
                and item.result_enabled is not None
            ),
            None,
        )
        if self._enabled_state_reader is not None:
            try:
                enabled = self._enabled_state_reader(target_id, deployment.addon_id)
            except Exception as error:
                raise AgentWorkspacePromotionConflictError(
                    "promotion_target_inventory_unavailable",
                    "Selected target enabled state could not be proven.",
                ) from error
        if enabled is None:
            raise AgentWorkspacePromotionConflictError(
                "promotion_target_enabled_unknown",
                "Selected target enabled state is unknown.",
            )
        return _TargetBinding(
            target.name,
            instance.runtime_revision_id,
            deployment.trusted_container_id,
            deployment.last_successful_operation_id,
            deployment.source_manifest_digest,
            deployment.verified_destination_digest,
            enabled,
        )

    def eligible_targets(
        self, workspace_id: UUID
    ) -> list[AgentWorkspacePromotionTargetRead]:
        workspace, _source = self._workspace_and_source(workspace_id)
        with self._session_factory() as session:
            instances = ManagedInstanceRepository(session).list()
        result: list[AgentWorkspacePromotionTargetRead] = []
        for instance in instances:
            try:
                binding = self._target_binding(
                    instance.target_id, workspace.development_source_id
                )
            except AgentWorkspacePromotionError:
                continue
            result.append(
                AgentWorkspacePromotionTargetRead(
                    id=instance.target_id, name=binding.name
                )
            )
        return sorted(result, key=lambda item: (item.name, str(item.id)))

    def sweep_startup(self) -> int:
        """Close prior-process attempts without adopting an apparent source tree."""

        now = self._clock()
        with self._session_factory() as session:
            preparing = AgentWorkspacePromotionPlanRepository(session).list_preparing()
        recovered = 0
        for preparation in preparing:
            with self._session_factory.begin() as session:
                global_lease = GlobalHeavyOperationLeaseRepository(session).current()
                if (
                    global_lease is None
                    or global_lease.operation_kind
                    is not GlobalHeavyOperationKind.AGENT_WORKSPACE_PROMOTION
                    or global_lease.operation_id != str(preparation.id)
                    or global_lease.lease_token_digest is None
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_recovery_coordination_corruption",
                        "Promotion preparation coordination could not be proven.",
                    )
                preparation_token = global_lease.lease_token_digest
                AgentWorkspacePromotionPlanRepository(session).invalidate(
                    preparation.id, "promotion_preparation_interrupted"
                )
                if not GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=preparation.id, token_digest=preparation_token
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_recovery_coordination_corruption",
                        "Promotion preparation ownership changed.",
                    )
            recovered += 1
        with self._session_factory() as session:
            active = AgentWorkspacePromotionOperationRepository(session).list_active()
        for operation in active:
            token_digest = operation.lease_token_digest
            reconciliation = operation.application_started
            code = (
                "promotion_restart_reconciliation_required"
                if reconciliation
                else "promotion_interrupted_before_application"
            )
            with self._session_factory() as session:
                plan_repository = AgentWorkspacePromotionPlanRepository(session)
                recovery_plan = plan_repository.get(operation.plan_id)
                recovery_paths = plan_repository.paths(operation.plan_id)
                source = DevelopmentSourceRepository(session).get(
                    operation.development_source_id
                )
                journal = AgentWorkspacePromotionOperationRepository(
                    session
                ).journal_rows(operation.id)
            if recovery_plan is None or source is None:
                raise AgentWorkspacePromotionConflictError(
                    "promotion_recovery_coordination_corruption",
                    "Promotion recovery context could not be proven.",
                )
            if (
                reconciliation
                and journal
                and all(
                    item.state is AgentWorkspacePromotionJournalState.NOT_ATTEMPTED
                    for item in journal
                )
                and self._development_root is not None
            ):
                try:
                    prove_promotion_preimages(
                        self._development_root,
                        source.relative_locator,
                        expected_addon_id=recovery_plan.addon_id,
                        paths=self._materials(recovery_paths),
                    )
                except AgentWorkspacePromotionStorageError:
                    pass
                else:
                    reconciliation = False
                    code = "promotion_interrupted_after_marker_before_application"
            state = (
                AgentWorkspacePromotionOperationState.RECONCILIATION_REQUIRED
                if reconciliation
                else AgentWorkspacePromotionOperationState.FAILED
            )
            with self._session_factory.begin() as session:
                operations = AgentWorkspacePromotionOperationRepository(session)
                active_plan = AgentWorkspacePromotionPlanRepository(session).get(
                    operation.plan_id
                )
                if active_plan is None:
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_recovery_coordination_corruption",
                        "Promotion recovery plan could not be proven.",
                    )
                if not operations.recover_startup(
                    operation.id,
                    token_digest=token_digest,
                    state=state,
                    now=now,
                    failure_code=code,
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_recovery_conflict",
                        "Promotion recovery ownership could not be proven.",
                    )
                if reconciliation:
                    if not AgentWorkspaceRepository(session).require_reconciliation(
                        operation.workspace_id,
                        expected_generation=active_plan.workspace_generation,
                        now=now,
                    ):
                        raise AgentWorkspacePromotionConflictError(
                            "promotion_recovery_workspace_conflict",
                            "Promotion recovery workspace state could not be proven.",
                        )
                    DevelopmentSourceDeploymentRepository(
                        session
                    ).mark_source_confidence(
                        operation.development_source_id,
                        DevelopmentSourceDeploymentConfidence.UNKNOWN,
                    )
                else:
                    AgentWorkspacePromotionPlanRepository(session).invalidate(
                        operation.plan_id, code
                    )
                if token_digest is None:
                    target_released = ManagedInstanceMutationLeaseRepository(
                        session
                    ).release_for_operation(operation.target_instance_id, operation.id)
                    heavy_released = GlobalHeavyOperationLeaseRepository(
                        session
                    ).release_for_operation(operation.id)
                else:
                    target_released = ManagedInstanceMutationLeaseRepository(
                        session
                    ).release(
                        operation.target_instance_id,
                        operation_id=operation.id,
                        token_digest=token_digest,
                    )
                    heavy_released = GlobalHeavyOperationLeaseRepository(
                        session
                    ).release(operation_id=operation.id, token_digest=token_digest)
                source_released = DevelopmentSourcePromotionLeaseRepository(
                    session
                ).release_for_operation(operation.id)
                if not (target_released and heavy_released and source_released):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_recovery_coordination_corruption",
                        "Promotion recovery coordination could not be proven.",
                    )
            recovered += 1
        return recovered

    def prepare(
        self, target_id: UUID, workspace_id: UUID
    ) -> AgentWorkspacePromotionPlanRead:
        if self._development_root is None:
            raise AgentWorkspacePromotionConflictError(
                "promotion_source_unavailable", "Development sources are unavailable."
            )
        now = self._clock()
        workspace, source = self._workspace_and_source(workspace_id)
        with self._session_factory.begin() as session:
            plans = AgentWorkspacePromotionPlanRepository(session)
            active = plans.active_for_workspace(workspace_id, now=now)
            if active is not None:
                raise AgentWorkspacePromotionConflictError(
                    "promotion_plan_active",
                    "Workspace already has an active promotion plan.",
                )
        binding = self._target_binding(target_id, source.id)
        if (
            workspace.baseline_manifest_digest is None
            or workspace.current_tree_digest is None
        ):
            raise AgentWorkspacePromotionConflictError(
                "promotion_workspace_not_ready", "Workspace provenance is incomplete."
            )
        plan_id = self._uuid_factory()
        capture_token = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        preparing = AgentWorkspacePromotionPlan(
            id=plan_id,
            workspace_id=workspace.id,
            development_source_id=source.id,
            target_instance_id=target_id,
            workspace_generation=workspace.generation,
            workspace_digest=workspace.current_tree_digest,
            baseline_artifact_id=workspace.baseline_artifact_id,
            baseline_digest=workspace.baseline_manifest_digest,
            source_capture_digest="sha256:" + "0" * 64,
            addon_id=workspace.source_addon_id,
            current_version=source.observed_version,
            promoted_name=source.observed_name,
            promoted_version=source.observed_version,
            target_runtime_revision_id=binding.runtime_revision_id,
            target_container_id=binding.container_id,
            target_deployment_operation_id=binding.deployment_operation_id,
            target_source_digest=binding.source_digest,
            target_destination_digest=binding.destination_digest,
            expected_enabled=binding.expected_enabled,
            changed_file_count=1,
            review_text_bytes=0,
            created_at=now,
            reviewable_at=None,
            expires_at=None,
            state=AgentWorkspacePromotionPlanState.PREPARING,
            live_target_instance_id=target_id,
            original_target_name=binding.name,
        )
        try:
            with self._session_factory.begin() as session:
                current_workspace = AgentWorkspaceRepository(session).get(workspace_id)
                if (
                    current_workspace is None
                    or current_workspace.state is not AgentWorkspaceState.READY
                    or current_workspace.generation != workspace.generation
                    or current_workspace.current_tree_digest
                    != workspace.current_tree_digest
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_workspace_not_ready",
                        "Only a stable ready workspace may be promoted.",
                    )
                AgentWorkspacePromotionPlanRepository(session).create(preparing, [])
                heavy = GlobalHeavyOperationLeaseRepository(session)
                heavy.reserve(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_PROMOTION,
                    operation_id=plan_id,
                )
                if not heavy.claim(
                    operation_id=plan_id,
                    token_digest=capture_token,
                    now=now,
                    expires_at=now + PROMOTION_LEASE_TTL,
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_capture_authority_lost",
                        "Promotion capture authority could not be acquired.",
                    )
        except IntegrityError as error:
            raise AgentWorkspacePromotionConflictError(
                "promotion_coordination_conflict",
                "Promotion review conflicts with active global-heavy work.",
            ) from error

        def abandon_preparation(reason: str) -> None:
            with self._session_factory.begin() as session:
                AgentWorkspacePromotionPlanRepository(session).invalidate(
                    plan_id, reason
                )
                if not GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=plan_id, token_digest=capture_token
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_capture_authority_lost",
                        "Promotion capture authority could not be released.",
                    )

        try:
            review = prepare_promotion_review(
                self._data_root,
                workspace.id,
                self._development_root,
                source.relative_locator,
                expected_baseline_digest=workspace.baseline_manifest_digest,
                expected_workspace_digest=workspace.current_tree_digest,
                expected_addon_id=workspace.source_addon_id,
            )
        except AgentWorkspacePromotionStorageError as error:
            abandon_preparation(error.code)
            raise AgentWorkspacePromotionConflictError(
                error.code, error.safe_message
            ) from error
        except Exception as error:
            abandon_preparation("promotion_preparation_failed")
            raise AgentWorkspacePromotionConflictError(
                "promotion_preparation_failed",
                "Promotion review preparation failed safely.",
            ) from error
        if review.permission_classification != "promotion_enabled":
            abandon_preparation("promotion_source_not_writable")
            raise AgentWorkspacePromotionConflictError(
                "promotion_source_not_writable",
                "Development source is readable but not promotion-writable.",
            )
        state = (
            AgentWorkspacePromotionPlanState.CONFLICTED
            if review.conflicted
            else AgentWorkspacePromotionPlanState.REVIEWABLE
        )
        expires = None if review.conflicted else now + PROMOTION_PLAN_TTL
        plan = AgentWorkspacePromotionPlan(
            id=plan_id,
            workspace_id=workspace.id,
            development_source_id=source.id,
            target_instance_id=target_id,
            workspace_generation=workspace.generation,
            workspace_digest=workspace.current_tree_digest,
            baseline_artifact_id=workspace.baseline_artifact_id,
            baseline_digest=workspace.baseline_manifest_digest,
            source_capture_digest=review.source_capture_digest,
            addon_id=workspace.source_addon_id,
            current_version=review.current_version,
            promoted_name=review.promoted_name,
            promoted_version=review.promoted_version,
            target_runtime_revision_id=binding.runtime_revision_id,
            target_container_id=binding.container_id,
            target_deployment_operation_id=binding.deployment_operation_id,
            target_source_digest=binding.source_digest,
            target_destination_digest=binding.destination_digest,
            expected_enabled=binding.expected_enabled,
            changed_file_count=len(review.paths),
            review_text_bytes=review.review_text_bytes,
            created_at=now,
            reviewable_at=None if review.conflicted else now,
            expires_at=expires,
            state=state,
            live_target_instance_id=target_id,
            original_target_name=binding.name,
        )
        paths: list[dict[str, object]] = [
            {
                "ordinal": item.ordinal,
                "relative_path": item.relative_path,
                "change_kind": AgentWorkspacePromotionChangeKind(item.change_kind),
                "pre_sha256": item.pre_sha256,
                "pre_size": item.pre_size,
                "pre_mode": item.pre_mode,
                "post_sha256": item.post_sha256,
                "post_size": item.post_size,
                "post_mode": item.post_mode,
                "before_text": item.before_text,
                "after_text": item.after_text,
                "conflict_reason": item.conflict_reason,
            }
            for item in review.paths
        ]
        try:
            with self._session_factory.begin() as session:
                if not AgentWorkspacePromotionPlanRepository(session).publish_review(
                    plan, paths
                ) or not GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=plan_id, token_digest=capture_token
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_capture_authority_lost",
                        "Promotion review could not be published exactly.",
                    )
        except IntegrityError as error:
            abandon_preparation("promotion_plan_conflict")
            raise AgentWorkspacePromotionConflictError(
                "promotion_plan_conflict",
                "Promotion planning conflicted with current work.",
            ) from error
        except AgentWorkspacePromotionConflictError:
            abandon_preparation("promotion_plan_conflict")
            raise
        result = self.get_plan(target_id, workspace_id, plan.id)
        if len(result.model_dump_json().encode("utf-8")) > 8 * 1024 * 1024:
            with self._session_factory.begin() as session:
                AgentWorkspacePromotionPlanRepository(session).invalidate(
                    plan.id, "promotion_review_too_large"
                )
            raise AgentWorkspacePromotionConflictError(
                "promotion_review_too_large", "Promotion review response is too large."
            )
        return result

    def get_plan(
        self, target_id: UUID, workspace_id: UUID, plan_id: UUID
    ) -> AgentWorkspacePromotionPlanRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            repository = AgentWorkspacePromotionPlanRepository(session)
            repository.expire_due(workspace_id, now=now)
            plan = repository.get(plan_id)
            paths = [] if plan is None else repository.paths(plan_id)
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            source = (
                None
                if plan is None
                else DevelopmentSourceRepository(session).get(
                    plan.development_source_id
                )
            )
            target = TargetRepository(session).get(target_id)
        if (
            plan is None
            or plan.workspace_id != workspace_id
            or plan.target_instance_id != target_id
            or workspace is None
            or source is None
        ):
            raise AgentWorkspacePromotionError(
                "promotion_plan_not_found", "Promotion plan was not found."
            )
        return AgentWorkspacePromotionPlanRead(
            plan_id=plan.id,
            workspace_id=workspace.id,
            state=plan.state,
            workspace_logical_name=workspace.source_logical_name,
            source_logical_name=source.logical_name,
            verification_target_id=plan.target_instance_id,
            verification_target_name=(
                target.name
                if target is not None
                else plan.original_target_name or "Deleted managed instance"
            ),
            verification_target_available=target is not None,
            addon_id=plan.addon_id,
            current_version=plan.current_version,
            promoted_version=plan.promoted_version,
            source_write_permission="promotion_enabled",
            changed_file_count=plan.changed_file_count,
            review_text_bytes=plan.review_text_bytes,
            expires_at=plan.expires_at,
            changes=[
                AgentWorkspacePromotionChangeRead(
                    path=item.relative_path,
                    change_kind=item.change_kind,
                    before_text=item.before_text,
                    after_text=item.after_text,
                    before_bytes=0
                    if item.before_text is None
                    else len(item.before_text.encode("utf-8")),
                    after_bytes=len(item.after_text.encode("utf-8")),
                    conflict_reason=item.conflict_reason,
                )
                for item in paths
            ],
            rollback_notice=(
                "Before source mutation, Kodi Lab publishes a private exact backup. "
                "Rollback is attempted only while exact operation postimages remain "
                "provable; otherwise reconciliation is required."
            ),
            kodi_verification_notice=(
                "Kodi verification occurs in the next bounded stage and a later Kodi "
                "validation failure will not automatically undo a successful source "
                "promotion."
            ),
        )

    def approve_and_execute(
        self, target_id: UUID, workspace_id: UUID, plan_id: UUID
    ) -> AgentWorkspacePromotionOperationStatus:
        now = self._clock()
        with self._session_factory.begin() as session:
            plans = AgentWorkspacePromotionPlanRepository(session)
            plans.expire_due(workspace_id, now=now)
            plan = plans.get(plan_id)
            paths = [] if plan is None else plans.paths(plan_id)
        if (
            plan is None
            or plan.workspace_id != workspace_id
            or plan.target_instance_id != target_id
            or plan.state is not AgentWorkspacePromotionPlanState.REVIEWABLE
            or plan.expires_at is None
            or plan.expires_at <= now
        ):
            raise AgentWorkspacePromotionConflictError(
                "promotion_plan_not_executable",
                "Promotion plan is expired, conflicted, invalidated, or already used.",
            )
        binding = self._target_binding(target_id, plan.development_source_id)
        if (
            binding.runtime_revision_id != plan.target_runtime_revision_id
            or binding.container_id != plan.target_container_id
            or binding.deployment_operation_id != plan.target_deployment_operation_id
            or binding.source_digest != plan.target_source_digest
            or binding.destination_digest != plan.target_destination_digest
        ):
            self._invalidate(plan.id, "promotion_target_drift")
            raise AgentWorkspacePromotionConflictError(
                "promotion_target_drift",
                "Selected verification target changed after review.",
            )
        operation = AgentWorkspacePromotionOperation(
            id=self._uuid_factory(),
            plan_id=plan.id,
            workspace_id=workspace_id,
            development_source_id=plan.development_source_id,
            target_instance_id=target_id,
            state=AgentWorkspacePromotionOperationState.QUEUED,
            phase=AgentWorkspacePromotionPhase.QUEUED,
            requested_at=now,
            live_target_instance_id=target_id,
        )
        try:
            with self._session_factory.begin() as session:
                plan_repository = AgentWorkspacePromotionPlanRepository(session)
                if not plan_repository.approve_and_bind(plan.id, operation.id):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_plan_already_used",
                        "Promotion plan was already used.",
                    )
                AgentWorkspacePromotionOperationRepository(session).create(
                    operation, paths
                )
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    target_id,
                    kind=ManagedInstanceMutationKind.AGENT_WORKSPACE_PROMOTION,
                    operation_id=operation.id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_PROMOTION,
                    operation_id=operation.id,
                )
                DevelopmentSourcePromotionLeaseRepository(session).reserve(
                    plan.development_source_id, workspace_id, operation.id
                )
        except IntegrityError as error:
            raise AgentWorkspacePromotionConflictError(
                "promotion_coordination_conflict",
                "Promotion conflicts with active source, target, or global-heavy work.",
            ) from error
        lease = self._acquire(operation)
        self._run(lease, plan, paths)
        with self._session_factory() as session:
            completed = AgentWorkspacePromotionOperationRepository(session).get(
                operation.id
            )
        if (
            completed is not None
            and completed.state is AgentWorkspacePromotionOperationState.SUCCEEDED
            and self._verification_starter is not None
        ):
            # The child was durably published with source success. Execution cannot
            # rewrite the already committed source result.
            self._verification_starter(operation.id)
        return self.get_operation(target_id, workspace_id, operation.id)

    def _acquire(self, operation: AgentWorkspacePromotionOperation) -> _Lease:
        lease = _Lease(
            operation.id,
            operation.plan_id,
            operation.workspace_id,
            operation.development_source_id,
            operation.target_instance_id,
            secrets.token_urlsafe(32),
        )
        now = self._clock()
        expires = now + PROMOTION_LEASE_TTL
        with self._session_factory.begin() as session:
            ok = AgentWorkspacePromotionOperationRepository(session).acquire(
                operation.id, token_digest=lease.digest, now=now, expires_at=expires
            )
            target = ManagedInstanceMutationLeaseRepository(session).claim(
                lease.target_id,
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
            source = DevelopmentSourcePromotionLeaseRepository(session).claim(
                lease.source_id,
                operation.id,
                token_digest=lease.digest,
                now=now,
                expires_at=expires,
            )
            if not (ok and target and heavy and source):
                raise AgentWorkspacePromotionConflictError(
                    "promotion_authority_lost",
                    "Promotion authority could not be acquired.",
                )
        return lease

    @staticmethod
    def _materials(
        paths: list[AgentWorkspacePromotionPlanPathRow],
    ) -> tuple[PromotionPathMaterial, ...]:
        return tuple(
            PromotionPathMaterial(
                ordinal=item.ordinal,
                relative_path=item.relative_path,
                change_kind=item.change_kind.value,
                pre_sha256=item.pre_sha256,
                pre_size=item.pre_size,
                pre_mode=item.pre_mode,
                post_sha256=item.post_sha256,
                post_size=item.post_size,
                post_mode=item.post_mode,
                before_text=item.before_text,
                after_text=item.after_text,
                conflict_reason=item.conflict_reason,
            )
            for item in paths
        )

    def _run(
        self,
        lease: _Lease,
        plan: AgentWorkspacePromotionPlan,
        paths: list[AgentWorkspacePromotionPlanPathRow],
    ) -> None:
        with self._session_factory() as session:
            source = DevelopmentSourceRepository(session).get(lease.source_id)
            workspace = AgentWorkspaceRepository(session).get(lease.workspace_id)
        if source is None or self._development_root is None:
            self._finish_failure(lease, plan, "promotion_source_missing", False)
            return
        if (
            workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.generation != plan.workspace_generation
            or workspace.baseline_manifest_digest != plan.baseline_digest
            or workspace.current_tree_digest != plan.workspace_digest
        ):
            self._finish_failure(lease, plan, "promotion_workspace_drift", False)
            return
        backup_id = self._uuid_factory()

        def update_phase(phase: AgentWorkspacePromotionPhase, **values: object) -> None:
            with self._session_factory.begin() as session:
                if not AgentWorkspacePromotionOperationRepository(session).update(
                    lease.operation_id, token_digest=lease.digest, phase=phase, **values
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_authority_lost", "Promotion authority was lost."
                    )

        def backup_published(value: UUID) -> None:
            update_phase(
                AgentWorkspacePromotionPhase.APPLICATION_MARKER,
                backup_artifact_id=str(value),
                backup_verified=True,
            )

        def marker() -> None:
            with self._session_factory.begin() as session:
                plans = AgentWorkspacePromotionPlanRepository(session)
                operations = AgentWorkspacePromotionOperationRepository(session)
                if not operations.update(
                    lease.operation_id,
                    token_digest=lease.digest,
                    application_started=True,
                    phase=AgentWorkspacePromotionPhase.APPLY,
                ) or not plans.consume(lease.plan_id, lease.operation_id):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_marker_failed",
                        "Promotion marker could not be published.",
                    )

        def journal(
            ordinal: int, state: str, device: int | None, inode: int | None
        ) -> None:
            with self._session_factory.begin() as session:
                if not AgentWorkspacePromotionOperationRepository(session).journal(
                    lease.operation_id,
                    ordinal,
                    AgentWorkspacePromotionJournalState(state),
                    device=device,
                    inode=inode,
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_journal_failed",
                        "Promotion journal could not be published.",
                    )

        def success() -> None:
            now = self._clock()
            binding = self._target_binding(
                lease.target_id,
                lease.source_id,
                permit_reserved_operation=lease.operation_id,
            )
            if (
                binding.runtime_revision_id != plan.target_runtime_revision_id
                or binding.container_id != plan.target_container_id
                or binding.deployment_operation_id
                != plan.target_deployment_operation_id
                or binding.source_digest != plan.target_source_digest
                or binding.destination_digest != plan.target_destination_digest
            ):
                raise AgentWorkspacePromotionConflictError(
                    "promotion_target_drift", "Selected verification target changed."
                )
            with self._session_factory.begin() as session:
                operations = AgentWorkspacePromotionOperationRepository(session)
                if not operations.update(
                    lease.operation_id,
                    token_digest=lease.digest,
                    phase=AgentWorkspacePromotionPhase.EVIDENCE_TRANSITION,
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_authority_lost", "Promotion authority was lost."
                    )
                AgentWorkspacePromotionHandoffRepository(session).create(
                    AgentWorkspacePromotionHandoff(
                        id=self._uuid_factory(),
                        operation_id=lease.operation_id,
                        workspace_id=lease.workspace_id,
                        development_source_id=lease.source_id,
                        target_instance_id=lease.target_id,
                        runtime_revision_id=plan.target_runtime_revision_id,
                        trusted_container_id=plan.target_container_id,
                        prior_deployment_operation_id=plan.target_deployment_operation_id,
                        prior_source_digest=plan.target_source_digest,
                        prior_destination_digest=plan.target_destination_digest,
                        created_at=now,
                        expected_addon_id=plan.addon_id,
                        expected_name=plan.promoted_name,
                        expected_version=plan.promoted_version,
                        expected_enabled=plan.expected_enabled,
                        live_target_instance_id=lease.target_id,
                    )
                )
                DevelopmentSourceDeploymentRepository(session).mark_source_confidence(
                    lease.source_id, DevelopmentSourceDeploymentConfidence.STALE
                )
                if not AgentWorkspaceRepository(session).mark_promoted(
                    lease.workspace_id,
                    expected_generation=plan.workspace_generation,
                    expected_tree_digest=plan.workspace_digest,
                    now=now,
                ) or not operations.finish(
                    lease.operation_id,
                    token_digest=lease.digest,
                    state=AgentWorkspacePromotionOperationState.SUCCEEDED,
                    now=now,
                    failure_code=None,
                ):
                    raise AgentWorkspacePromotionConflictError(
                        "promotion_success_publication_failed",
                        "Promotion success could not be published.",
                    )
                self._release(session, lease)
                self._verification_publisher(session, lease.operation_id)

        try:
            update_phase(AgentWorkspacePromotionPhase.BACKUP)
            execute_promotion(
                self._data_root,
                lease.workspace_id,
                self._development_root,
                source.relative_locator,
                operation_id=lease.operation_id,
                backup_id=backup_id,
                expected_baseline_digest=plan.baseline_digest,
                expected_workspace_digest=plan.workspace_digest,
                expected_addon_id=plan.addon_id,
                paths=self._materials(paths),
                backup_published=backup_published,
                application_marker=marker,
                journal_update=journal,
                final_capture_started=lambda: update_phase(
                    AgentWorkspacePromotionPhase.FINAL_CAPTURE
                ),
                publish_success=success,
                failure_injector=self._failure_injector,
            )
        except AgentWorkspacePromotionStorageError as error:
            self._finish_failure(lease, plan, error.code, error.reconciliation_required)
        except Exception:
            with self._session_factory() as session:
                current = AgentWorkspacePromotionOperationRepository(session).get(
                    lease.operation_id
                )
            self._finish_failure(
                lease,
                plan,
                "promotion_internal_failure",
                current is None or current.application_started,
            )

    def _release(self, session: Session, lease: _Lease) -> None:
        if (
            not ManagedInstanceMutationLeaseRepository(session).release(
                lease.target_id,
                operation_id=lease.operation_id,
                token_digest=lease.digest,
            )
            or not GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id, token_digest=lease.digest
            )
            or not DevelopmentSourcePromotionLeaseRepository(session).release(
                lease.source_id, lease.operation_id, token_digest=lease.digest
            )
        ):
            raise AgentWorkspacePromotionConflictError(
                "promotion_lease_release_failed",
                "Promotion leases could not be released.",
            )

    def _finish_failure(
        self,
        lease: _Lease,
        plan: AgentWorkspacePromotionPlan,
        code: str,
        reconciliation_required: bool,
    ) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            operations = AgentWorkspacePromotionOperationRepository(session)
            if reconciliation_required:
                AgentWorkspaceRepository(session).require_reconciliation(
                    lease.workspace_id,
                    expected_generation=plan.workspace_generation,
                    now=now,
                )
                DevelopmentSourceDeploymentRepository(session).mark_source_confidence(
                    lease.source_id, DevelopmentSourceDeploymentConfidence.UNKNOWN
                )
            else:
                AgentWorkspacePromotionPlanRepository(session).invalidate(
                    lease.plan_id, code
                )
            state = (
                AgentWorkspacePromotionOperationState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else AgentWorkspacePromotionOperationState.FAILED
            )
            if not operations.finish(
                lease.operation_id,
                token_digest=lease.digest,
                state=state,
                now=now,
                failure_code=code[:128],
            ):
                raise AgentWorkspacePromotionConflictError(
                    "promotion_failure_publication_failed",
                    "Promotion failure could not be published.",
                )
            self._release(session, lease)

    def _invalidate(self, plan_id: UUID, reason: str) -> None:
        with self._session_factory.begin() as session:
            AgentWorkspacePromotionPlanRepository(session).invalidate(plan_id, reason)

    def get_operation(
        self, target_id: UUID, workspace_id: UUID, operation_id: UUID
    ) -> AgentWorkspacePromotionOperationStatus:
        with self._session_factory() as session:
            operation = AgentWorkspacePromotionOperationRepository(session).get(
                operation_id
            )
            handoff = AgentWorkspacePromotionHandoffRepository(
                session
            ).get_for_operation(operation_id)
        if (
            operation is None
            or operation.workspace_id != workspace_id
            or operation.target_instance_id != target_id
        ):
            raise AgentWorkspacePromotionError(
                "promotion_operation_not_found", "Promotion operation was not found."
            )
        return AgentWorkspacePromotionOperationStatus(
            operation_id=operation.id,
            plan_id=operation.plan_id,
            workspace_id=operation.workspace_id,
            verification_target_id=operation.target_instance_id,
            verification_target_available=operation.live_target_instance_id is not None,
            state=operation.state,
            phase=operation.phase,
            application_started=operation.application_started,
            backup_verified=operation.backup_verified,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            failure_code=operation.failure_code,
            reconciliation_required=operation.reconciliation_required,
            handoff_published=handoff is not None,
        )

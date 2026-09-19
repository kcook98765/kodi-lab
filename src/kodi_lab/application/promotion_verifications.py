"""Promotion-bound stopped sync, same-container reload, and bounded verification."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import (
    AgentWorkspacePromotionOperationState,
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
    PromotionLogResult,
    PromotionVerificationOperation,
    PromotionVerificationPhase,
    PromotionVerificationStageResult,
    PromotionVerificationState,
)
from kodi_lab.kodi.readiness import KodiReadinessCoordinator, UsabilityState
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionHandoffRepository,
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    PromotionVerificationOperationRepository,
    RuntimeExecutionProfileRepository,
    TargetRepository,
)

VERIFICATION_LEASE_TTL = timedelta(minutes=15)
LOG_WINDOW_DELAY_SECONDS = 10.0
SUPPORTED_ADDON_TYPES = frozenset(
    {
        "xbmc.python.pluginsource",
        "xbmc.python.script",
        "xbmc.python.module",
        "xbmc.python.weather",
        "xbmc.python.lyrics",
        "xbmc.python.library",
        "xbmc.service",
    }
)


class PromotionVerificationError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class PromotionVerificationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification_id: UUID
    source_promotion_id: UUID
    workspace_id: UUID
    target_instance_id: UUID
    target_name: str = Field(max_length=256)
    addon_id: str = Field(max_length=128)
    state: PromotionVerificationState
    phase: PromotionVerificationPhase
    sync_result: PromotionVerificationStageResult
    reload_result: PromotionVerificationStageResult
    inventory_result: PromotionVerificationStageResult
    log_result: PromotionLogResult
    expected_version: str
    current_version: str | None
    expected_enabled: bool
    current_enabled: bool | None
    target_error_count: int
    unrelated_warning_count: int
    target_error_snippets: list[str] = Field(max_length=20)
    failure_code: str | None
    requested_at: datetime
    completed_at: datetime | None
    source_result_notice: str = (
        "Kodi verification does not change the already-completed source "
        "promotion result."
    )


class _Broker(Protocol):
    def managed_container_inventory(self) -> Any: ...
    def stop_managed_container(self, **kwargs: Any) -> Any: ...
    def start_managed_container(self, **kwargs: Any) -> Any: ...
    def copy_sync_promoted_development_source(self, **kwargs: Any) -> Any: ...
    def capture_promotion_kodi_log_cursor(self, **kwargs: Any) -> Any: ...
    def read_promotion_kodi_log_window(self, **kwargs: Any) -> Any: ...


class _Inventory(Protocol):
    def get(self, instance_id: UUID) -> Any: ...


class PromotionVerificationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _Broker,
        inventory: _Inventory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        sleep: Callable[[float], None] = time.sleep,
        readiness_factory: Callable[[Any, Any], Any] = KodiReadinessCoordinator,
        failure_injector: Callable[[str], None] = lambda _phase: None,
        reconciliation_gate: Callable[[UUID], None] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._broker = broker
        self._inventory = inventory
        self._clock = clock
        self._uuid = uuid_factory
        self._sleep = sleep
        self._readiness_factory = readiness_factory
        self._failure_injector = failure_injector
        self._reconciliation_gate = reconciliation_gate

    def queue_and_run(self, source_promotion_id: UUID) -> PromotionVerificationStatus:
        existing = self._get_for_promotion(source_promotion_id)
        if existing is not None:
            return self.get(existing.id)
        with self._sessions() as session:
            promotion = AgentWorkspacePromotionOperationRepository(session).get(
                source_promotion_id
            )
            plan = (
                None
                if promotion is None
                else AgentWorkspacePromotionPlanRepository(session).get(
                    promotion.plan_id
                )
            )
            handoff = AgentWorkspacePromotionHandoffRepository(
                session
            ).get_for_operation(source_promotion_id)
            source = (
                None
                if handoff is None
                else DevelopmentSourceRepository(session).get(
                    handoff.development_source_id
                )
            )
            instance = (
                None
                if handoff is None
                else ManagedInstanceRepository(session).get(handoff.target_instance_id)
            )
            attachment = (
                None
                if handoff is None
                else ManagedInstanceDevelopmentSourceRepository(session).get(
                    handoff.target_instance_id, handoff.development_source_id
                )
            )
            evidence = (
                None
                if handoff is None
                else DevelopmentSourceDeploymentRepository(session).get(
                    handoff.target_instance_id, handoff.development_source_id
                )
            )
        if (
            promotion is None
            or plan is None
            or promotion.state is not AgentWorkspacePromotionOperationState.SUCCEEDED
            or handoff is None
            or handoff.consumed_at is not None
            or promotion.plan_id != plan.id
            or promotion.workspace_id != handoff.workspace_id
            or promotion.development_source_id != handoff.development_source_id
            or promotion.target_instance_id != handoff.target_instance_id
            or plan.workspace_id != handoff.workspace_id
            or plan.development_source_id != handoff.development_source_id
            or plan.target_instance_id != handoff.target_instance_id
            or plan.target_runtime_revision_id != handoff.runtime_revision_id
            or plan.target_container_id != handoff.trusted_container_id
            or plan.target_deployment_operation_id
            != handoff.prior_deployment_operation_id
            or plan.target_source_digest != handoff.prior_source_digest
            or plan.target_destination_digest != handoff.prior_destination_digest
            or handoff.operation_id != promotion.id
        ):
            raise PromotionVerificationError(
                "promotion_verification_handoff_invalid",
                "Successful source-promotion handoff is unavailable or consumed.",
            )
        if (
            source is None
            or instance is None
            or attachment is None
            or evidence is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or instance.runtime_revision_id != handoff.runtime_revision_id
            or evidence.runtime_revision_id != handoff.runtime_revision_id
            or evidence.trusted_container_id != handoff.trusted_container_id
            or evidence.last_successful_operation_id
            != handoff.prior_deployment_operation_id
            or evidence.source_manifest_digest != handoff.prior_source_digest
            or evidence.verified_destination_digest != handoff.prior_destination_digest
        ):
            raise PromotionVerificationError(
                "promotion_verification_preflight_drift",
                "Selected verification target drifted before mutation.",
            )
        if self._reconciliation_gate is not None:
            self._reconciliation_gate(instance.target_id)
        if (
            handoff.expected_addon_id != source.addon_id
            or handoff.expected_name is None
            or handoff.expected_version is None
            or handoff.expected_enabled is None
        ):
            raise PromotionVerificationError(
                "promotion_verification_handoff_incomplete",
                "Promotion verification handoff lacks bound expected state.",
            )
        verification_id = self._uuid()
        deployment_id = self._uuid()
        now = self._clock()
        operation = PromotionVerificationOperation(
            id=verification_id,
            source_promotion_id=source_promotion_id,
            handoff_id=handoff.id,
            workspace_id=handoff.workspace_id,
            development_source_id=handoff.development_source_id,
            target_instance_id=handoff.target_instance_id,
            runtime_revision_id=handoff.runtime_revision_id,
            deployment_operation_id=deployment_id,
            state=PromotionVerificationState.QUEUED,
            phase=PromotionVerificationPhase.QUEUED,
            addon_id=source.addon_id,
            expected_name=handoff.expected_name,
            expected_version=handoff.expected_version,
            expected_enabled=handoff.expected_enabled,
            requested_at=now,
        )
        deployment = DevelopmentSourceDeploymentOperation(
            id=deployment_id,
            original_managed_instance_id=instance.target_id,
            managed_instance_id=instance.target_id,
            development_source_id=source.id,
            source_logical_name=source.logical_name,
            addon_id=source.addon_id,
            runtime_revision_id=instance.runtime_revision_id,
            request_kind=DevelopmentSourceDeploymentRequestKind.COPY_SYNC,
            requested_at=now,
            state=DevelopmentSourceDeploymentOperationState.QUEUED,
            phase=DevelopmentSourceDeploymentOperationPhase.ACQUIRE,
        )
        try:
            with self._sessions.begin() as session:
                DevelopmentSourceDeploymentOperationRepository(session).create(
                    deployment
                )
                PromotionVerificationOperationRepository(session).create(operation)
                if not AgentWorkspacePromotionHandoffRepository(
                    session
                ).reserve_for_verification(
                    handoff.id,
                    verification_operation_id=verification_id,
                    addon_id=source.addon_id,
                    expected_name=plan.promoted_name,
                    expected_version=plan.promoted_version,
                    expected_enabled=handoff.expected_enabled,
                ):
                    raise PromotionVerificationError(
                        "promotion_verification_handoff_reserved",
                        "Promotion verification handoff was already reserved "
                        "or consumed.",
                    )
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance.target_id,
                    kind=ManagedInstanceMutationKind.PROMOTION_VERIFICATION,
                    operation_id=verification_id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.PROMOTION_VERIFICATION,
                    operation_id=verification_id,
                )
        except IntegrityError as error:
            raise PromotionVerificationError(
                "promotion_verification_coordination_conflict",
                "Target mutation coordination is unavailable.",
            ) from error
        self._run(verification_id, handoff.trusted_container_id)
        return self.get(verification_id)

    def _get_for_promotion(
        self, promotion_id: UUID
    ) -> PromotionVerificationOperation | None:
        with self._sessions() as session:
            return PromotionVerificationOperationRepository(session).get_for_promotion(
                promotion_id
            )

    def get_for_promotion(self, promotion_id: UUID) -> PromotionVerificationStatus:
        operation = self._get_for_promotion(promotion_id)
        if operation is None:
            raise PromotionVerificationError(
                "promotion_verification_not_found", "Kodi verification was not found."
            )
        return self.get(operation.id)

    def _run(self, operation_id: UUID, container_id: str) -> None:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        expires = now + VERIFICATION_LEASE_TTL
        with self._sessions.begin() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
            if operation is None:
                raise PromotionVerificationError(
                    "promotion_verification_missing",
                    "Verification operation is unavailable.",
                )
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                operation.target_instance_id,
                operation_id=operation.id,
                token_digest=digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=digest,
                now=now,
                expires_at=expires,
            )
            acquired = PromotionVerificationOperationRepository(session).update(
                operation.id,
                state=PromotionVerificationState.RUNNING,
                phase=PromotionVerificationPhase.PREFLIGHT,
                started_at=now,
                lease_token_digest=digest,
                lease_expires_at=expires,
            )
            deployment = DevelopmentSourceDeploymentOperationRepository(
                session
            ).acquire(
                operation.deployment_operation_id,
                token_digest=digest,
                expected_container_id=container_id,
                now=now,
                lease_expires_at=expires,
            )
            if not (mutation and heavy and acquired and deployment):
                raise PromotionVerificationError(
                    "promotion_verification_authority_lost",
                    "Verification authority could not be acquired.",
                )
        cursor: str | None = None
        stopped = sync_started = sync_completed = start_dispatched = False
        observation_started = False
        try:
            self._require_bound_preflight(operation_id, container_id)
            self._require_exact_container(
                operation_id, container_id, ContainerRuntimeState.RUNNING
            )
            self._phase(operation_id, PromotionVerificationPhase.LOG_CURSOR)
            cursor = self._broker.capture_promotion_kodi_log_cursor(
                verification_id=str(operation_id),
                managed_instance_id=str(operation.target_instance_id),
                expected_container_id=container_id,
            ).cursor
            self._failure_injector("before_stop")
            self._phase(operation_id, PromotionVerificationPhase.STOP)
            self._broker.stop_managed_container(
                managed_instance_id=str(operation.target_instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
            stopped = True
            self._update(operation_id, stop_completed=True)
            self._failure_injector("after_stop_before_sync")
            self._phase(operation_id, PromotionVerificationPhase.SYNC)
            sync_started = True
            self._update(operation_id, sync_started=True)
            result = self._broker.copy_sync_promoted_development_source(
                verification_operation_id=str(operation_id),
                managed_instance_id=str(operation.target_instance_id),
                expected_container_id=container_id,
            )
            self._publish_sync(operation_id, digest, result)
            sync_completed = True
            self._failure_injector("after_sync_before_start")
            self._phase(operation_id, PromotionVerificationPhase.START)
            start_dispatched = True
            self._update(operation_id, start_dispatched=True)
            self._broker.start_managed_container(
                managed_instance_id=str(operation.target_instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            self._phase(operation_id, PromotionVerificationPhase.READINESS)
            with self._sessions() as session:
                profile = RuntimeExecutionProfileRepository(session).get(
                    operation.runtime_revision_id
                )
            if profile is None:
                raise PromotionVerificationError(
                    "promotion_verification_readiness_unavailable",
                    "Runtime readiness profile is unavailable.",
                )
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(operation.target_instance_id))
            if readiness.usability_state is not UsabilityState.USABLE:
                self._finish(
                    operation_id,
                    digest,
                    PromotionVerificationState.FAILED,
                    reload_result=PromotionVerificationStageResult.FAILED,
                    failure_code="promotion_verification_readiness_failed",
                )
                return
            self._update(
                operation_id, reload_result=PromotionVerificationStageResult.SUCCEEDED
            )
            observation_started = True
            self._failure_injector("after_readiness_before_inventory")
            self._phase(operation_id, PromotionVerificationPhase.INVENTORY)
            inventory = self._inventory.get(operation.target_instance_id)
            live = next(
                (
                    item
                    for item in inventory.items
                    if item.addon_id == operation.addon_id
                ),
                None,
            )
            inventory_ok = (
                inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
                and live is not None
                and live.version == operation.expected_version
                and live.enabled is operation.expected_enabled
                and live.kodi_type in SUPPORTED_ADDON_TYPES
            )
            if live is not None:
                self._update(
                    operation_id,
                    current_version=live.version,
                    current_enabled=live.enabled,
                )
            self._update(
                operation_id,
                inventory_result=PromotionVerificationStageResult.SUCCEEDED
                if inventory_ok
                else PromotionVerificationStageResult.FAILED
                if inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
                else PromotionVerificationStageResult.INCOMPLETE,
            )
            self._phase(operation_id, PromotionVerificationPhase.LOG_WINDOW)
            self._sleep(LOG_WINDOW_DELAY_SECONDS)
            window = self._broker.read_promotion_kodi_log_window(
                verification_id=str(operation_id),
                managed_instance_id=str(operation.target_instance_id),
                expected_container_id=container_id,
                cursor=cursor,
            )
            log_result = PromotionLogResult(window.classification)
            self._update(
                operation_id,
                log_result=log_result,
                target_error_count=window.target_error_count,
                unrelated_warning_count=window.unrelated_warning_count,
                target_error_snippets=tuple(window.snippets),
            )
            if not inventory_ok:
                final = (
                    PromotionVerificationState.FAILED
                    if inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
                    else PromotionVerificationState.INCOMPLETE
                )
            elif log_result is PromotionLogResult.TARGET_ERRORS:
                final = PromotionVerificationState.FAILED
            elif log_result is PromotionLogResult.INCOMPLETE:
                final = PromotionVerificationState.INCOMPLETE
            else:
                final = PromotionVerificationState.SUCCEEDED
            self._finish(operation_id, digest, final, failure_code=None)
        except Exception as error:
            code = getattr(error, "code", "promotion_verification_internal_failure")
            if observation_started:
                with self._sessions() as session:
                    current = PromotionVerificationOperationRepository(session).get(
                        operation_id
                    )
                inventory_failed = (
                    current is not None
                    and current.inventory_result
                    is PromotionVerificationStageResult.FAILED
                )
                self._finish(
                    operation_id,
                    digest,
                    PromotionVerificationState.FAILED
                    if inventory_failed
                    else PromotionVerificationState.INCOMPLETE,
                    inventory_result=(
                        PromotionVerificationStageResult.FAILED
                        if inventory_failed
                        else PromotionVerificationStageResult.INCOMPLETE
                    ),
                    log_result=PromotionLogResult.INCOMPLETE,
                    failure_code=code,
                )
                return
            if sync_started and not sync_completed:
                if code in {
                    "development_source_reconciliation_required",
                    "development_source_apply_unverified",
                    "development_destination_verification_failed",
                    "promotion_verification_handoff_authority_lost",
                    "promotion_verification_internal_failure",
                }:
                    self._finish(
                        operation_id,
                        digest,
                        PromotionVerificationState.RECONCILIATION_REQUIRED,
                        sync_result=(
                            PromotionVerificationStageResult.RECONCILIATION_REQUIRED
                        ),
                        failure_code=code,
                    )
                else:
                    try:
                        self._broker.start_managed_container(
                            managed_instance_id=str(operation.target_instance_id),
                            expected_container_id=container_id,
                            expected_runtime_state=ContainerRuntimeState.STOPPED,
                        )
                        self._finish(
                            operation_id,
                            digest,
                            PromotionVerificationState.FAILED,
                            sync_result=PromotionVerificationStageResult.FAILED,
                            failure_code=code,
                        )
                    except Exception:
                        self._finish(
                            operation_id,
                            digest,
                            PromotionVerificationState.RECONCILIATION_REQUIRED,
                            sync_result=(
                                PromotionVerificationStageResult.RECONCILIATION_REQUIRED
                            ),
                            failure_code=code,
                        )
            elif start_dispatched:
                self._finish(
                    operation_id,
                    digest,
                    PromotionVerificationState.RECONCILIATION_REQUIRED,
                    reload_result=PromotionVerificationStageResult.RECONCILIATION_REQUIRED,
                    failure_code=code,
                )
            elif stopped:
                try:
                    self._broker.start_managed_container(
                        managed_instance_id=str(operation.target_instance_id),
                        expected_container_id=container_id,
                        expected_runtime_state=ContainerRuntimeState.STOPPED,
                    )
                    self._finish(
                        operation_id,
                        digest,
                        PromotionVerificationState.FAILED,
                        failure_code=code,
                    )
                except Exception:
                    self._finish(
                        operation_id,
                        digest,
                        PromotionVerificationState.RECONCILIATION_REQUIRED,
                        reload_result=(
                            PromotionVerificationStageResult.RECONCILIATION_REQUIRED
                        ),
                        failure_code=code,
                    )
            else:
                self._finish(
                    operation_id,
                    digest,
                    PromotionVerificationState.FAILED,
                    failure_code=code,
                )

    def _require_bound_preflight(self, operation_id: UUID, container_id: str) -> None:
        """Re-prove every persisted handoff binding after coordination is held."""

        with self._sessions() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
            if operation is None:
                raise PromotionVerificationError(
                    "promotion_verification_missing",
                    "Verification operation is unavailable.",
                )
            promotion = AgentWorkspacePromotionOperationRepository(session).get(
                operation.source_promotion_id
            )
            plan = AgentWorkspacePromotionPlanRepository(session).get(
                promotion.plan_id if promotion is not None else UUID(int=0)
            )
            handoff = AgentWorkspacePromotionHandoffRepository(session).get(
                operation.handoff_id
            )
            instance = ManagedInstanceRepository(session).get(
                operation.target_instance_id
            )
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                operation.target_instance_id, operation.development_source_id
            )
            evidence = DevelopmentSourceDeploymentRepository(session).get(
                operation.target_instance_id, operation.development_source_id
            )
        if (
            promotion is None
            or plan is None
            or handoff is None
            or instance is None
            or attachment is None
            or evidence is None
            or promotion.state is not AgentWorkspacePromotionOperationState.SUCCEEDED
            or promotion.plan_id != plan.id
            or promotion.workspace_id != operation.workspace_id
            or promotion.development_source_id != operation.development_source_id
            or promotion.target_instance_id != operation.target_instance_id
            or plan.workspace_id != operation.workspace_id
            or plan.development_source_id != operation.development_source_id
            or plan.target_instance_id != operation.target_instance_id
            or plan.target_runtime_revision_id != operation.runtime_revision_id
            or plan.target_container_id != container_id
            or plan.target_deployment_operation_id
            != handoff.prior_deployment_operation_id
            or plan.target_source_digest != handoff.prior_source_digest
            or plan.target_destination_digest != handoff.prior_destination_digest
            or handoff.operation_id != promotion.id
            or handoff.verification_operation_id != operation.id
            or handoff.consumed_at is not None
            or handoff.workspace_id != operation.workspace_id
            or handoff.development_source_id != operation.development_source_id
            or handoff.target_instance_id != operation.target_instance_id
            or handoff.runtime_revision_id != operation.runtime_revision_id
            or handoff.trusted_container_id != container_id
            or handoff.expected_addon_id != operation.addon_id
            or handoff.expected_name != operation.expected_name
            or handoff.expected_version != operation.expected_version
            or handoff.expected_enabled is not operation.expected_enabled
            or instance.instance_type is not InstanceType.DISPOSABLE
            or instance.runtime_revision_id != operation.runtime_revision_id
            or evidence.runtime_revision_id != operation.runtime_revision_id
            or evidence.trusted_container_id != container_id
            or evidence.last_successful_operation_id
            != handoff.prior_deployment_operation_id
            or evidence.source_manifest_digest != handoff.prior_source_digest
            or evidence.verified_destination_digest != handoff.prior_destination_digest
        ):
            raise PromotionVerificationError(
                "promotion_verification_preflight_drift",
                "Selected verification target drifted before mutation.",
            )

    def _publish_sync(self, operation_id: UUID, digest: str, result: Any) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
            assert operation is not None
            evidence = DevelopmentSourceDeployment(
                managed_instance_id=operation.target_instance_id,
                development_source_id=operation.development_source_id,
                addon_id=operation.addon_id,
                mode=DevelopmentSourceDeploymentMode.COPY,
                last_successful_operation_id=operation.deployment_operation_id,
                source_manifest_digest=result.source_manifest_digest,
                verified_destination_digest=result.verified_destination_digest,
                runtime_revision_id=operation.runtime_revision_id,
                trusted_container_id=result.container_id,
                observed_source_name=result.observed_source_name,
                observed_source_version=result.observed_source_version,
                verified_at=now,
                confidence=DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION,
            )
            DevelopmentSourceDeploymentRepository(session).upsert_verified(evidence)
            deployment_finished = DevelopmentSourceDeploymentOperationRepository(
                session
            ).finish(
                operation.deployment_operation_id,
                token_digest=digest,
                state=DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                result_classification=DevelopmentSourceDeploymentResult(
                    result.result_classification
                ),
                new_count=result.new_count,
                changed_count=result.changed_count,
                unchanged_count=result.unchanged_count,
                destination_only_count=result.destination_only_count,
                transferred_bytes=result.transferred_bytes,
                failure_reason=None,
                reconciliation_required=False,
                now=now,
            )
            handoff_consumed = AgentWorkspacePromotionHandoffRepository(
                session
            ).consume_reserved(
                operation.handoff_id,
                verification_operation_id=operation.id,
                now=now,
            )
            operation_updated = PromotionVerificationOperationRepository(
                session
            ).update(
                operation_id,
                sync_completed=True,
                sync_result=PromotionVerificationStageResult.SUCCEEDED,
                handoff_consumed=True,
            )
            if not (deployment_finished and handoff_consumed and operation_updated):
                raise PromotionVerificationError(
                    "promotion_verification_handoff_authority_lost",
                    "Reserved promotion verification handoff could not be consumed.",
                )

    def _require_exact_container(
        self, operation_id: UUID, container_id: str, state: ContainerRuntimeState
    ) -> None:
        with self._sessions() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
        assert operation is not None
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(operation.target_instance_id)
        ]
        if (
            len(matches) != 1
            or matches[0].container_id != container_id
            or matches[0].runtime_state is not state
        ):
            raise PromotionVerificationError(
                "promotion_verification_container_drift",
                "Exact managed container precondition changed.",
            )

    def _phase(self, operation_id: UUID, phase: PromotionVerificationPhase) -> None:
        self._update(operation_id, phase=phase)

    def _update(self, operation_id: UUID, **values: object) -> None:
        with self._sessions.begin() as session:
            if not PromotionVerificationOperationRepository(session).update(
                operation_id, **values
            ):
                raise PromotionVerificationError(
                    "promotion_verification_authority_lost",
                    "Verification operation could not be updated.",
                )

    def _finish(
        self,
        operation_id: UUID,
        digest: str,
        state: PromotionVerificationState,
        *,
        failure_code: str | None,
        **values: object,
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
            if operation is None:
                return
            deployment = DevelopmentSourceDeploymentOperationRepository(session).get(
                operation.deployment_operation_id
            )
            if (
                deployment is not None
                and deployment.state
                is DevelopmentSourceDeploymentOperationState.RUNNING
            ):
                reconciliation = (
                    state is PromotionVerificationState.RECONCILIATION_REQUIRED
                )
                DevelopmentSourceDeploymentOperationRepository(session).finish(
                    operation.deployment_operation_id,
                    token_digest=digest,
                    state=(
                        DevelopmentSourceDeploymentOperationState.RECONCILIATION_REQUIRED
                        if reconciliation
                        else DevelopmentSourceDeploymentOperationState.FAILED
                    ),
                    result_classification=None,
                    new_count=None,
                    changed_count=None,
                    unchanged_count=None,
                    destination_only_count=None,
                    transferred_bytes=None,
                    failure_reason=failure_code,
                    reconciliation_required=reconciliation,
                    now=now,
                )
            PromotionVerificationOperationRepository(session).update(
                operation_id,
                state=state,
                phase=PromotionVerificationPhase.FINALIZE,
                completed_at=now,
                failure_code=failure_code,
                lease_token_digest=None,
                lease_expires_at=None,
                **values,
            )
            ManagedInstanceMutationLeaseRepository(session).release(
                operation.target_instance_id,
                operation_id=operation_id,
                token_digest=digest,
            )
            GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=operation_id, token_digest=digest
            )

    def get(self, operation_id: UUID) -> PromotionVerificationStatus:
        with self._sessions() as session:
            operation = PromotionVerificationOperationRepository(session).get(
                operation_id
            )
            target = (
                None
                if operation is None
                else TargetRepository(session).get(operation.target_instance_id)
            )
        if operation is None or target is None:
            raise PromotionVerificationError(
                "promotion_verification_not_found", "Kodi verification was not found."
            )
        return PromotionVerificationStatus(
            verification_id=operation.id,
            source_promotion_id=operation.source_promotion_id,
            workspace_id=operation.workspace_id,
            target_instance_id=operation.target_instance_id,
            target_name=target.name,
            addon_id=operation.addon_id,
            state=operation.state,
            phase=operation.phase,
            sync_result=operation.sync_result,
            reload_result=operation.reload_result,
            inventory_result=operation.inventory_result,
            log_result=operation.log_result,
            expected_version=operation.expected_version,
            current_version=operation.current_version,
            expected_enabled=operation.expected_enabled,
            current_enabled=operation.current_enabled,
            target_error_count=operation.target_error_count,
            unrelated_warning_count=operation.unrelated_warning_count,
            target_error_snippets=list(operation.target_error_snippets),
            failure_code=operation.failure_code,
            requested_at=operation.requested_at,
            completed_at=operation.completed_at,
        )

    def sweep_startup(self) -> int:
        """Never resume mutation after a process boundary."""
        with self._sessions() as session:
            active = PromotionVerificationOperationRepository(session).list_active()
        recovered = 0
        for operation in active:
            uncertain = (
                operation.stop_completed
                or operation.sync_started
                or operation.start_dispatched
            )
            state = (
                PromotionVerificationState.RECONCILIATION_REQUIRED
                if uncertain
                else PromotionVerificationState.FAILED
            )
            with self._sessions.begin() as session:
                if not PromotionVerificationOperationRepository(
                    session
                ).recover_interrupted(operation.id, state=state, now=self._clock()):
                    raise PromotionVerificationError(
                        "promotion_verification_recovery_failed",
                        "Interrupted verification could not be closed safely.",
                    )
                ManagedInstanceMutationLeaseRepository(session).release_for_operation(
                    operation.target_instance_id, operation.id
                )
                GlobalHeavyOperationLeaseRepository(session).release_for_operation(
                    operation.id
                )
            recovered += 1
        return recovered

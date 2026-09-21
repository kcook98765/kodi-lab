"""Replay-safe MCP preparation of Phase 9 Web-reviewed promotion plans."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionConflictError,
    AgentWorkspacePromotionPlanRead,
    AgentWorkspacePromotionService,
    McpPromotionOrigin,
)
from kodi_lab.application.mcp_development_requests import (
    DEVELOPMENT_REQUEST_MAX_GLOBAL,
    DEVELOPMENT_REQUEST_MAX_PER_CLIENT,
    DEVELOPMENT_REQUEST_RETENTION,
    PROMOTION_REQUEST_OPERATION_KIND,
    prune_expired_safe_development_requests,
)
from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState
from kodi_lab.storage.models import (
    AgentWorkspacePromotionOperationRow,
    AgentWorkspacePromotionPlanRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceValidationRow,
    PromotionVerificationOperationRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)


def _to_text(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _fingerprint(workspace_id: UUID, generation: int, target_id: UUID) -> str:
    material = (
        f"{PROMOTION_REQUEST_OPERATION_KIND}\x00{workspace_id}\x00"
        f"{generation}\x00{target_id}"
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()


class McpPromotionRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PromotionRequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    promotion_plan_id: UUID
    workspace_id: UUID
    source_id: UUID
    target_id: UUID
    generation: int = Field(ge=0)
    review_state: str = Field(max_length=32)
    changed_file_count: int = Field(ge=1, le=100)
    added_file_count: int = Field(ge=0, le=100)
    modified_file_count: int = Field(ge=0, le=100)
    candidate_version: str = Field(max_length=128)
    expires_at: datetime | None
    web_review_required: bool = True
    readiness_classification: str = Field(max_length=64)
    conflict_classification: str = Field(max_length=64)


class PromotionStatusProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: UUID | None = None
    review_state: str | None = Field(default=None, max_length=32)
    promotion_operation_id: UUID | None = None
    promotion_operation_state: str | None = Field(default=None, max_length=32)
    verification_id: UUID | None = None
    verification_state: str | None = Field(default=None, max_length=32)
    final_classification: str | None = Field(default=None, max_length=64)


class BoundedPromotionPreparationLimiter:
    """At most two new preparations per client in one rolling ten-minute window."""

    def __init__(
        self,
        *,
        limit: int = 2,
        window_seconds: float = 600.0,
        maximum_keys: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._maximum = maximum_keys
        self._clock = clock
        self._entries: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def key_count(self) -> int:
        return len(self._entries)

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            cutoff = now - self._window
            for current in list(self._entries):
                retained = [value for value in self._entries[current] if value > cutoff]
                if retained:
                    self._entries[current] = retained
                else:
                    del self._entries[current]
            values = self._entries.get(key)
            if values is None:
                if len(self._entries) >= self._maximum:
                    return False
                values = []
                self._entries[key] = values
            if len(values) >= self._limit:
                self._entries.move_to_end(key)
                return False
            values.append(now)
            self._entries.move_to_end(key)
            return True


class McpPromotionRequestService:
    """Validate MCP evidence and invoke only the canonical preparation boundary."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        promotions: AgentWorkspacePromotionService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        limiter: BoundedPromotionPreparationLimiter | None = None,
        retention: timedelta = DEVELOPMENT_REQUEST_RETENTION,
        max_per_client: int = DEVELOPMENT_REQUEST_MAX_PER_CLIENT,
        max_global: int = DEVELOPMENT_REQUEST_MAX_GLOBAL,
    ) -> None:
        self._sessions = sessions
        self._promotions = promotions
        self._clock = clock
        self._limiter = limiter or BoundedPromotionPreparationLimiter()
        self._retention = retention
        self._max_per_client = max_per_client
        self._max_global = max_global

    def request(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        expected_generation: int,
        target_id: UUID,
    ) -> PromotionRequestResult:
        fingerprint = _fingerprint(workspace_id, expected_generation, target_id)
        existing = self._load_request(client_id, request_id)
        if existing is not None:
            return self._replay(existing, fingerprint)

        source_id, deployment_id, execution_id = self._preflight(
            client_id, workspace_id, expected_generation, target_id
        )
        with self._sessions.begin() as session:
            active = AgentWorkspacePromotionPlanRepository(
                session
            ).active_for_workspace(workspace_id, now=self._clock())
        if active is not None:
            if (
                active.originating_mcp_client_id == client_id
                and active.workspace_generation == expected_generation
                and active.target_instance_id == target_id
            ):
                raise McpPromotionRequestError("promotion_already_requested")
            raise McpPromotionRequestError("promotion_plan_active")
        if not self._limiter.allow(str(client_id)):
            raise McpPromotionRequestError("rate_limited")

        origin = McpPromotionOrigin(
            client_id=client_id,
            request_id=request_id,
            deployment_id=deployment_id,
            execution_id=execution_id,
        )

        def claim(session: Session, plan_id: UUID) -> None:
            self._prune_and_require_capacity(session, client_id)
            current = self._find_request(session, client_id, request_id)
            if current is not None:
                if current.parameter_fingerprint != fingerprint:
                    raise McpPromotionRequestError("request_id_conflict")
                raise McpPromotionRequestError("promotion_already_requested")
            now = _to_text(self._clock())
            session.add(
                McpDevelopmentRequestRow(
                    id=str(uuid4()),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    operation_kind=PROMOTION_REQUEST_OPERATION_KIND,
                    parameter_fingerprint=fingerprint,
                    development_source_id=str(source_id),
                    workspace_id=str(workspace_id),
                    creation_operation_id=None,
                    operation_id=str(plan_id),
                    state="allocated",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()

        try:
            plan = self._promotions.prepare(
                target_id,
                workspace_id,
                mcp_origin=origin,
                request_claim=claim,
            )
        except McpPromotionRequestError:
            replay = self._load_request(client_id, request_id)
            if replay is not None:
                return self._replay(replay, fingerprint)
            raise
        except AgentWorkspacePromotionConflictError as error:
            replay = self._load_request(client_id, request_id)
            if replay is not None:
                return self._replay(replay, fingerprint)
            raise McpPromotionRequestError(
                self._map_promotion_error(error.code)
            ) from error
        except (IntegrityError, SQLAlchemyError) as error:
            replay = self._load_request(client_id, request_id)
            if replay is not None:
                return self._replay(replay, fingerprint)
            with self._sessions.begin() as session:
                active = AgentWorkspacePromotionPlanRepository(
                    session
                ).active_for_workspace(workspace_id, now=self._clock())
            if active is not None:
                if (
                    active.originating_mcp_client_id == client_id
                    and active.workspace_generation == expected_generation
                    and active.target_instance_id == target_id
                ):
                    raise McpPromotionRequestError(
                        "promotion_already_requested"
                    ) from error
                raise McpPromotionRequestError("promotion_plan_active") from error
            raise McpPromotionRequestError("promotion_request_ambiguous") from error
        return self._result(request_id, plan)

    def projection(self, workspace_id: UUID) -> PromotionStatusProjection:
        with self._sessions() as session:
            plan = session.scalar(
                select(AgentWorkspacePromotionPlanRow)
                .where(AgentWorkspacePromotionPlanRow.workspace_id == str(workspace_id))
                .order_by(
                    AgentWorkspacePromotionPlanRow.created_at.desc(),
                    AgentWorkspacePromotionPlanRow.id.desc(),
                )
                .limit(1)
            )
            operation = session.scalar(
                select(AgentWorkspacePromotionOperationRow)
                .where(
                    AgentWorkspacePromotionOperationRow.workspace_id
                    == str(workspace_id)
                )
                .order_by(
                    AgentWorkspacePromotionOperationRow.requested_at.desc(),
                    AgentWorkspacePromotionOperationRow.id.desc(),
                )
                .limit(1)
            )
            verification = (
                None
                if operation is None
                else session.scalar(
                    select(PromotionVerificationOperationRow).where(
                        PromotionVerificationOperationRow.source_promotion_id
                        == operation.id
                    )
                )
            )
        return PromotionStatusProjection(
            plan_id=None if plan is None else UUID(plan.id),
            review_state=None if plan is None else plan.state.value,
            promotion_operation_id=(None if operation is None else UUID(operation.id)),
            promotion_operation_state=(
                None if operation is None else operation.state.value
            ),
            verification_id=(None if verification is None else UUID(verification.id)),
            verification_state=(
                None if verification is None else verification.state.value
            ),
            final_classification=self._final_classification(
                None if plan is None else plan.state.value,
                None if operation is None else operation.state.value,
                None if verification is None else verification.state.value,
            ),
        )

    @staticmethod
    def _final_classification(
        plan: str | None, operation: str | None, verification: str | None
    ) -> str | None:
        if verification is not None:
            return {
                "succeeded": "source_promoted_and_verified",
                "failed": "source_promoted_verification_failed",
                "incomplete": "source_promoted_verification_incomplete",
                "reconciliation_required": "promotion_reconciliation_required",
                "queued": "source_promoted_verification_queued",
                "running": "source_promoted_verification_running",
            }.get(verification, "promotion_state_unknown")
        if operation is not None:
            return {
                "queued": "promotion_queued",
                "running": "promotion_running",
                "succeeded": "source_promoted_verification_pending",
                "failed": "promotion_failed",
                "reconciliation_required": "promotion_reconciliation_required",
            }.get(operation, "promotion_state_unknown")
        if plan is None:
            return None
        return {
            "preparing": "promotion_preparing",
            "reviewable": "awaiting_web_review",
            "conflicted": "promotion_conflicted",
            "invalidated": "promotion_rejected_or_invalidated",
            "expired": "promotion_expired",
            "approved_and_queued": "promotion_queued",
            "consumed": "promotion_consumed",
        }.get(plan, "promotion_state_unknown")

    def _preflight(
        self, client_id: UUID, workspace_id: UUID, generation: int, target_id: UUID
    ) -> tuple[UUID, UUID, UUID]:
        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            validation = session.scalar(
                select(McpWorkspaceValidationRow).where(
                    McpWorkspaceValidationRow.workspace_id == str(workspace_id)
                )
            )
            deployment = session.scalar(
                select(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.client_id == str(client_id),
                    McpWorkspaceDeploymentOperationRow.workspace_id
                    == str(workspace_id),
                    McpWorkspaceDeploymentOperationRow.target_id == str(target_id),
                    McpWorkspaceDeploymentOperationRow.generation == generation,
                    McpWorkspaceDeploymentOperationRow.state == "succeeded",
                )
                .order_by(McpWorkspaceDeploymentOperationRow.requested_at.desc())
                .limit(1)
            )
            execution = (
                None
                if deployment is None
                else session.scalar(
                    select(McpWorkspaceExecutionOperationRow)
                    .where(
                        McpWorkspaceExecutionOperationRow.client_id == str(client_id),
                        McpWorkspaceExecutionOperationRow.workspace_id
                        == str(workspace_id),
                        McpWorkspaceExecutionOperationRow.target_id == str(target_id),
                        McpWorkspaceExecutionOperationRow.generation == generation,
                        McpWorkspaceExecutionOperationRow.deployment_id
                        == deployment.id,
                        McpWorkspaceExecutionOperationRow.state == "succeeded",
                    )
                    .order_by(McpWorkspaceExecutionOperationRow.requested_at.desc())
                    .limit(1)
                )
            )
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
        if (
            workspace is None
            or source is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
        ):
            raise McpPromotionRequestError("promotion_not_allowed")
        if workspace.generation != generation:
            raise McpPromotionRequestError("workspace_generation_conflict")
        if (
            validation is None
            or validation.generation != generation
            or validation.development_source_id != str(source.id)
            or validation.classification != "passed"
        ):
            raise McpPromotionRequestError("validation_required")
        if (
            validation.addon_id != workspace.source_addon_id
            or source.addon_id != workspace.source_addon_id
        ):
            raise McpPromotionRequestError("promotion_not_allowed")
        if deployment is None:
            raise McpPromotionRequestError("deployment_required")
        if (
            deployment.development_source_id != str(source.id)
            or deployment.addon_id != workspace.source_addon_id
            or deployment.addon_version != validation.addon_version
        ):
            raise McpPromotionRequestError("promotion_conflict")
        if execution is None:
            raise McpPromotionRequestError("execution_required")
        return source.id, UUID(deployment.id), UUID(execution.id)

    def _replay(
        self, request: McpDevelopmentRequestRow, fingerprint: str
    ) -> PromotionRequestResult:
        if request.parameter_fingerprint != fingerprint:
            raise McpPromotionRequestError("request_id_conflict")
        if request.operation_id is None:
            raise McpPromotionRequestError("promotion_request_ambiguous")
        with self._sessions() as session:
            stored = session.get(AgentWorkspacePromotionPlanRow, request.operation_id)
        if stored is None:
            raise McpPromotionRequestError("promotion_request_ambiguous")
        if getattr(stored.state, "value", stored.state) == "preparing":
            raise McpPromotionRequestError("promotion_request_ambiguous")
        plan = self._promotions.get_plan(
            UUID(stored.original_target_instance_id),
            UUID(request.workspace_id),
            UUID(request.operation_id),
        )
        return self._result(UUID(request.request_id), plan)

    @staticmethod
    def _result(
        request_id: UUID, plan: AgentWorkspacePromotionPlanRead
    ) -> PromotionRequestResult:
        return PromotionRequestResult(
            request_id=request_id,
            promotion_plan_id=plan.plan_id,
            workspace_id=plan.workspace_id,
            source_id=plan.source_id,
            target_id=plan.verification_target_id,
            generation=plan.workspace_generation,
            review_state=plan.state.value,
            changed_file_count=plan.changed_file_count,
            added_file_count=plan.added_file_count,
            modified_file_count=plan.modified_file_count,
            candidate_version=plan.promoted_version,
            expires_at=plan.expires_at,
            readiness_classification=(
                "ready_for_web_review"
                if plan.state.value == "reviewable"
                else "not_ready_for_web_review"
            ),
            conflict_classification=plan.source_conflict_classification,
        )

    def _load_request(
        self, client_id: UUID, request_id: UUID
    ) -> McpDevelopmentRequestRow | None:
        with self._sessions() as session:
            row = self._find_request(session, client_id, request_id)
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _find_request(
        session: Session, client_id: UUID, request_id: UUID
    ) -> McpDevelopmentRequestRow | None:
        return session.scalar(
            select(McpDevelopmentRequestRow).where(
                McpDevelopmentRequestRow.client_id == str(client_id),
                McpDevelopmentRequestRow.request_id == str(request_id),
                McpDevelopmentRequestRow.operation_kind
                == PROMOTION_REQUEST_OPERATION_KIND,
            )
        )

    def _prune_and_require_capacity(self, session: Session, client_id: UUID) -> None:
        prune_expired_safe_development_requests(
            session, now=self._clock(), retention=self._retention
        )
        per_client = session.scalar(
            select(func.count())
            .select_from(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.client_id == str(client_id))
        )
        total = session.scalar(
            select(func.count()).select_from(McpDevelopmentRequestRow)
        )
        if (
            int(per_client or 0) >= self._max_per_client
            or int(total or 0) >= self._max_global
        ):
            raise McpPromotionRequestError("promotion_request_ambiguous")

    @staticmethod
    def _map_promotion_error(code: str) -> str:
        if code == "promotion_plan_active":
            return "promotion_plan_active"
        if code in {
            "promotion_target_ineligible",
            "promotion_target_inventory_unavailable",
            "promotion_target_enabled_unknown",
            "promotion_workspace_not_ready",
            "promotion_source_missing",
            "promotion_source_not_writable",
        }:
            return "promotion_not_allowed"
        if code.startswith("promotion_"):
            return "promotion_conflict"
        return "promotion_request_ambiguous"

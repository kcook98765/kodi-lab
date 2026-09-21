"""Lifecycle-aware retention for shared Phase 12 mutation request identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    AgentWorkspaceCreationOperationRow,
    AgentWorkspacePromotionOperationRow,
    AgentWorkspacePromotionPlanRow,
    AgentWorkspaceRow,
    McpAgentWorkspaceBindingRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    PromotionVerificationOperationRow,
)

DEVELOPMENT_REQUEST_RETENTION = timedelta(days=30)
DEVELOPMENT_REQUEST_MAX_PER_CLIENT = 10_000
DEVELOPMENT_REQUEST_MAX_GLOBAL = 100_000
DEVELOPMENT_REQUEST_PRUNE_CANDIDATE_LIMIT = 256

WORKSPACE_CREATE_OPERATION_KIND = "workspace_create"
WORKSPACE_DEPLOY_OPERATION_KIND = "workspace_deploy"
WORKSPACE_EXECUTE_OPERATION_KIND = "workspace_execute"
PROMOTION_REQUEST_OPERATION_KIND = "promotion_request"

_ACTIVE_PROMOTION_PLAN_STATES = {
    "preparing",
    "reviewable",
    "approved_and_queued",
}
_TERMINAL_PROMOTION_PLAN_STATES = {
    "conflicted",
    "invalidated",
    "expired",
    "consumed",
}
_TERMINAL_VERIFICATION_STATES = {"succeeded", "failed", "incomplete"}


@dataclass(frozen=True, slots=True)
class DevelopmentRequestPruneResult:
    inspected: int
    deleted: int


def _state(value: object) -> str:
    return str(getattr(value, "value", value))


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _promotion_workflow_is_terminal(
    session: Session, plan: AgentWorkspacePromotionPlanRow
) -> bool:
    """Return true only for a fully closed review/apply/verification workflow."""

    plan_state = _state(plan.state)
    if plan_state in _ACTIVE_PROMOTION_PLAN_STATES:
        return False
    if plan_state not in _TERMINAL_PROMOTION_PLAN_STATES:
        return False

    if plan.source_operation_id is None:
        return plan_state in {"conflicted", "invalidated", "expired"}
    if plan_state not in {"invalidated", "consumed"}:
        return False

    operation = session.get(
        AgentWorkspacePromotionOperationRow, plan.source_operation_id
    )
    if (
        operation is None
        or operation.plan_id != plan.id
        or operation.workspace_id != plan.workspace_id
        or operation.development_source_id != plan.development_source_id
        or operation.completed_at is None
    ):
        return False
    operation_state = _state(operation.state)
    if operation_state == "failed":
        return plan_state == "invalidated" and not operation.reconciliation_required
    if operation_state != "succeeded" or plan_state != "consumed":
        return False

    verification = session.scalar(
        select(PromotionVerificationOperationRow).where(
            PromotionVerificationOperationRow.source_promotion_id == operation.id
        )
    )
    return bool(
        verification is not None
        and verification.workspace_id == plan.workspace_id
        and verification.development_source_id == plan.development_source_id
        and verification.completed_at is not None
        and _state(verification.state) in _TERMINAL_VERIFICATION_STATES
    )


def _referenced_promotion_workflows_are_terminal(
    session: Session, operation_id: str, *, deployment: bool
) -> bool:
    reference = (
        AgentWorkspacePromotionPlanRow.originating_mcp_deployment_id
        if deployment
        else AgentWorkspacePromotionPlanRow.originating_mcp_execution_id
    )
    plans = session.scalars(
        select(AgentWorkspacePromotionPlanRow)
        .where(reference == operation_id)
        .order_by(
            AgentWorkspacePromotionPlanRow.created_at,
            AgentWorkspacePromotionPlanRow.id,
        )
    ).all()
    return all(_promotion_workflow_is_terminal(session, plan) for plan in plans)


def _creation_is_replay_safe(
    session: Session, request: McpDevelopmentRequestRow
) -> bool:
    if request.creation_operation_id is None or request.operation_id is not None:
        return False
    operation = session.get(
        AgentWorkspaceCreationOperationRow, request.creation_operation_id
    )
    workspace = session.get(AgentWorkspaceRow, request.workspace_id)
    binding = session.get(McpAgentWorkspaceBindingRow, request.workspace_id)
    if (
        operation is None
        or workspace is None
        or binding is None
        or operation.workspace_id != request.workspace_id
        or operation.development_source_id != request.development_source_id
        or workspace.development_source_id != request.development_source_id
        or binding.client_id != request.client_id
        or binding.development_source_id != request.development_source_id
        or binding.binding_origin != "mcp_created"
        or operation.completed_at is None
    ):
        return False
    operation_state = _state(operation.state)
    if operation_state == "succeeded":
        return _state(workspace.state) != "creating"
    return operation_state == "failed" and _state(workspace.state) == "abandoned"


def _deployment_is_replay_safe(
    session: Session, request: McpDevelopmentRequestRow
) -> bool:
    if request.operation_id is None or request.creation_operation_id is not None:
        return False
    operation = session.get(McpWorkspaceDeploymentOperationRow, request.operation_id)
    if (
        operation is None
        or operation.client_id != request.client_id
        or operation.request_id != request.request_id
        or operation.workspace_id != request.workspace_id
        or operation.development_source_id != request.development_source_id
        or operation.completed_at is None
        or operation.phase != "finalize"
        or not _referenced_promotion_workflows_are_terminal(
            session, operation.id, deployment=True
        )
    ):
        return False
    if operation.state == "succeeded":
        return bool(
            operation.mutation_started
            and operation.sync_started
            and operation.sync_completed
            and operation.target_started
            and operation.inventory_classification == "matched"
            and operation.result_classification is not None
            and operation.failure_code is None
        )
    return bool(
        operation.state == "failed"
        and not operation.mutation_started
        and operation.failure_code is not None
    )


def _execution_is_replay_safe(
    session: Session, request: McpDevelopmentRequestRow
) -> bool:
    if request.operation_id is None or request.creation_operation_id is not None:
        return False
    operation = session.get(McpWorkspaceExecutionOperationRow, request.operation_id)
    if (
        operation is None
        or operation.client_id != request.client_id
        or operation.request_id != request.request_id
        or operation.workspace_id != request.workspace_id
        or operation.development_source_id != request.development_source_id
        or operation.completed_at is None
        or operation.phase != "finalize"
        or not _referenced_promotion_workflows_are_terminal(
            session, operation.id, deployment=False
        )
    ):
        return False
    if operation.state == "succeeded":
        return bool(
            operation.invocation_dispatched
            and operation.execution_classification == "directory_result"
            and operation.result_classification is not None
            and operation.failure_code is None
        )
    return bool(
        operation.state == "failed"
        and not operation.invocation_dispatched
        and operation.failure_code is not None
    )


def _promotion_request_is_replay_safe(
    session: Session, request: McpDevelopmentRequestRow
) -> bool:
    if request.operation_id is None or request.creation_operation_id is not None:
        return False
    plan = session.get(AgentWorkspacePromotionPlanRow, request.operation_id)
    return bool(
        plan is not None
        and plan.workspace_id == request.workspace_id
        and plan.development_source_id == request.development_source_id
        and plan.originating_mcp_client_id == request.client_id
        and plan.originating_mcp_request_id == request.request_id
        and _promotion_workflow_is_terminal(session, plan)
    )


def development_request_is_replay_safe_to_forget(
    session: Session, request: McpDevelopmentRequestRow
) -> bool:
    """Apply the closed per-kind lifecycle policy, retaining every uncertainty."""

    if request.state != "allocated":
        return False
    if request.operation_kind == WORKSPACE_CREATE_OPERATION_KIND:
        return _creation_is_replay_safe(session, request)
    if request.operation_kind == WORKSPACE_DEPLOY_OPERATION_KIND:
        return _deployment_is_replay_safe(session, request)
    if request.operation_kind == WORKSPACE_EXECUTE_OPERATION_KIND:
        return _execution_is_replay_safe(session, request)
    if request.operation_kind == PROMOTION_REQUEST_OPERATION_KIND:
        return _promotion_request_is_replay_safe(session, request)
    return False


def prune_expired_safe_development_requests(
    session: Session,
    *,
    now: datetime,
    retention: timedelta = DEVELOPMENT_REQUEST_RETENTION,
    candidate_limit: int = DEVELOPMENT_REQUEST_PRUNE_CANDIDATE_LIMIT,
) -> DevelopmentRequestPruneResult:
    """Delete one bounded batch of expired, provably replay-safe mappings.

    Callers invoke this inside the same write transaction used to allocate new
    work. SQLite therefore serializes the observation and deletion. All state
    transitions used to prove terminality are monotonic; terminal rows cannot
    become active again.
    """

    if retention <= timedelta(0) or candidate_limit < 1:
        raise ValueError("development request pruning bounds must be positive")
    cutoff = _to_text(now - retention)
    potentially_terminal = or_(
        and_(
            McpDevelopmentRequestRow.operation_kind == WORKSPACE_CREATE_OPERATION_KIND,
            exists().where(
                AgentWorkspaceCreationOperationRow.id
                == McpDevelopmentRequestRow.creation_operation_id,
                AgentWorkspaceCreationOperationRow.state.in_(("succeeded", "failed")),
                AgentWorkspaceCreationOperationRow.completed_at.is_not(None),
            ),
        ),
        and_(
            McpDevelopmentRequestRow.operation_kind == WORKSPACE_DEPLOY_OPERATION_KIND,
            exists().where(
                McpWorkspaceDeploymentOperationRow.id
                == McpDevelopmentRequestRow.operation_id,
                or_(
                    McpWorkspaceDeploymentOperationRow.state == "succeeded",
                    and_(
                        McpWorkspaceDeploymentOperationRow.state == "failed",
                        ~McpWorkspaceDeploymentOperationRow.mutation_started,
                    ),
                ),
                McpWorkspaceDeploymentOperationRow.completed_at.is_not(None),
            ),
        ),
        and_(
            McpDevelopmentRequestRow.operation_kind == WORKSPACE_EXECUTE_OPERATION_KIND,
            exists().where(
                McpWorkspaceExecutionOperationRow.id
                == McpDevelopmentRequestRow.operation_id,
                or_(
                    McpWorkspaceExecutionOperationRow.state == "succeeded",
                    and_(
                        McpWorkspaceExecutionOperationRow.state == "failed",
                        ~McpWorkspaceExecutionOperationRow.invocation_dispatched,
                    ),
                ),
                McpWorkspaceExecutionOperationRow.completed_at.is_not(None),
            ),
        ),
        and_(
            McpDevelopmentRequestRow.operation_kind == PROMOTION_REQUEST_OPERATION_KIND,
            exists().where(
                AgentWorkspacePromotionPlanRow.id
                == McpDevelopmentRequestRow.operation_id,
                or_(
                    and_(
                        AgentWorkspacePromotionPlanRow.state.in_(
                            ("conflicted", "invalidated", "expired")
                        ),
                        AgentWorkspacePromotionPlanRow.source_operation_id.is_(None),
                    ),
                    and_(
                        AgentWorkspacePromotionPlanRow.state == "invalidated",
                        exists().where(
                            AgentWorkspacePromotionOperationRow.id
                            == AgentWorkspacePromotionPlanRow.source_operation_id,
                            AgentWorkspacePromotionOperationRow.state == "failed",
                            ~AgentWorkspacePromotionOperationRow.reconciliation_required,
                        ),
                    ),
                    and_(
                        AgentWorkspacePromotionPlanRow.state == "consumed",
                        exists().where(
                            AgentWorkspacePromotionOperationRow.id
                            == AgentWorkspacePromotionPlanRow.source_operation_id,
                            AgentWorkspacePromotionOperationRow.state == "succeeded",
                            exists().where(
                                PromotionVerificationOperationRow.source_promotion_id
                                == AgentWorkspacePromotionOperationRow.id,
                                PromotionVerificationOperationRow.state.in_(
                                    tuple(sorted(_TERMINAL_VERIFICATION_STATES))
                                ),
                                PromotionVerificationOperationRow.completed_at.is_not(
                                    None
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    candidates = list(
        session.scalars(
            select(McpDevelopmentRequestRow)
            .where(
                McpDevelopmentRequestRow.created_at < cutoff,
                potentially_terminal,
            )
            .order_by(
                McpDevelopmentRequestRow.created_at,
                McpDevelopmentRequestRow.id,
            )
            .limit(candidate_limit)
        )
    )
    safe_ids = [
        row.id
        for row in candidates
        if development_request_is_replay_safe_to_forget(session, row)
    ]
    if safe_ids:
        session.execute(
            delete(McpDevelopmentRequestRow).where(
                McpDevelopmentRequestRow.id.in_(safe_ids)
            )
        )
    return DevelopmentRequestPruneResult(
        inspected=len(candidates), deleted=len(safe_ids)
    )

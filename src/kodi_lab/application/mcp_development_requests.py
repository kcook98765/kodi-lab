"""Lifecycle-aware retention for shared Phase 12 mutation request identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from kodi_lab.storage.models import (
    AgentWorkspaceCreationOperationRow,
    AgentWorkspaceRow,
    ExternalBridgeDeploymentReceiptRow,
    McpAgentWorkspaceBindingRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    RepositoryWorkspaceDeploymentRow,
)

DEVELOPMENT_REQUEST_RETENTION = timedelta(days=30)
DEVELOPMENT_REQUEST_MAX_PER_CLIENT = 10_000
DEVELOPMENT_REQUEST_MAX_GLOBAL = 100_000
DEVELOPMENT_REQUEST_PRUNE_CANDIDATE_LIMIT = 256

WORKSPACE_CREATE_OPERATION_KIND = "workspace_create"
WORKSPACE_DEPLOY_OPERATION_KIND = "workspace_deploy"
WORKSPACE_EXECUTE_OPERATION_KIND = "workspace_execute"

_TERMINAL_VERIFICATION_STATES = {"succeeded", "failed", "incomplete"}
_DEPLOYMENT_RESULT_CLASSIFICATIONS = {
    "initial_deployment",
    "synchronized",
    "unchanged",
}
_EXTERNAL_DEPLOYMENT_RESULT_CLASSIFICATIONS = {
    "deployed",
    "deployed_and_verified",
}


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


def _deployment_success_evidence_is_consistent(
    session: Session,
    operation: McpWorkspaceDeploymentOperationRow,
) -> bool:
    common = bool(
        operation.state == "succeeded"
        and operation.phase == "finalize"
        and operation.started_at is not None
        and operation.completed_at is not None
        and (
            operation.deployment_mode == "repository"
            or (
                operation.mutation_started
                and operation.sync_started
                and operation.sync_completed
                and operation.target_started
            )
        )
        and operation.inventory_classification == "matched"
        and operation.addon_enabled is not None
        and operation.source_manifest_digest is not None
        and operation.verified_destination_digest is not None
        and operation.failure_code is None
    )
    if not common:
        return False
    if operation.deployment_mode == "repository":
        linked = session.get(RepositoryWorkspaceDeploymentRow, operation.id)
        return bool(
            operation.target_class == "external_development"
            and operation.initiator_kind == "mcp"
            and operation.client_id is not None
            and operation.repository_origin == "planned"
            and operation.bridge_id is not None
            and operation.bridge_request_id is None
            and operation.bridge_receipt_id is None
            and operation.addon_enabled is True
            and operation.result_classification == "repository_verified"
            and operation.source_manifest_digest
            == operation.verified_destination_digest
            and linked is not None
            and linked.origin == "planned"
            and linked.state == "succeeded"
            and linked.bridge_id == operation.bridge_id
            and linked.expected_manifest_digest == operation.verified_destination_digest
            and linked.verified_at is not None
        )
    receipt = session.get(ExternalBridgeDeploymentReceiptRow, operation.id)
    return bool(
        operation.target_class == "external_development"
        and operation.bridge_id is not None
        and operation.bridge_request_id == operation.request_id
        and operation.bridge_receipt_id is not None
        and operation.file_count is not None
        and operation.total_bytes is not None
        and operation.result_classification
        in _EXTERNAL_DEPLOYMENT_RESULT_CLASSIFICATIONS
        and receipt is not None
        and receipt.target_id == operation.target_id
        and receipt.bridge_id == operation.bridge_id
        and receipt.bridge_request_id == operation.request_id
        and receipt.bridge_receipt_id == operation.bridge_receipt_id
        and receipt.expected_manifest_digest == operation.source_manifest_digest
        and receipt.expected_manifest_digest == operation.verified_destination_digest
        and receipt.file_count == operation.file_count
        and receipt.total_bytes == operation.total_bytes
        and receipt.state == "succeeded"
        and receipt.completed_at is not None
    )


def _deployment_pre_mutation_failure_evidence_is_consistent(
    operation: McpWorkspaceDeploymentOperationRow,
) -> bool:
    """Accept only a terminal failure with no durable mutation evidence at all."""

    return bool(
        operation.state == "failed"
        and operation.phase == "finalize"
        and operation.started_at is not None
        and operation.completed_at is not None
        and operation.failure_code is not None
        and not operation.mutation_started
        and not operation.sync_started
        and not operation.sync_completed
        and not operation.target_started
        and operation.inventory_classification == "not_checked"
        and operation.addon_enabled is None
        and operation.result_classification is None
        and operation.source_manifest_digest is None
        and operation.verified_destination_digest is None
    )


def _deployment_repository_failure_evidence_is_consistent(
    session: Session, operation: McpWorkspaceDeploymentOperationRow
) -> bool:
    linked = session.get(RepositoryWorkspaceDeploymentRow, operation.id)
    return bool(
        operation.deployment_mode == "repository"
        and operation.initiator_kind == "mcp"
        and operation.client_id is not None
        and operation.repository_origin == "planned"
        and operation.target_class == "external_development"
        and operation.state == "failed"
        and operation.phase == "finalize"
        and operation.started_at is not None
        and operation.completed_at is not None
        and operation.failure_code is not None
        and not operation.mutation_started
        and linked is not None
        and linked.origin == "planned"
        and linked.state == "failed"
        and linked.failure_code == operation.failure_code
        and linked.expected_manifest_digest == operation.source_manifest_digest
    )


def _execution_success_evidence_is_consistent(
    operation: McpWorkspaceExecutionOperationRow,
) -> bool:
    return bool(
        operation.state == "succeeded"
        and operation.phase == "finalize"
        and operation.started_at is not None
        and operation.completed_at is not None
        and operation.invocation_dispatched
        and operation.execution_classification == "directory_result"
        and operation.item_count is not None
        and operation.result_classification == "smoke_passed"
        and operation.failure_code is None
    )


def _execution_pre_dispatch_failure_evidence_is_consistent(
    operation: McpWorkspaceExecutionOperationRow,
) -> bool:
    """Accept only a terminal failure with no durable dispatch evidence at all."""

    return bool(
        operation.state == "failed"
        and operation.phase == "finalize"
        and operation.started_at is not None
        and operation.completed_at is not None
        and operation.failure_code is not None
        and not operation.invocation_dispatched
        and operation.execution_classification == "not_attempted"
        and operation.item_count is None
        and operation.result_classification is None
    )


def deployment_terminal_evidence_is_consistent_sql() -> ColumnElement[bool]:
    """SQL form used by bounded terminal-operation cleanup."""

    operation = McpWorkspaceDeploymentOperationRow
    receipt = ExternalBridgeDeploymentReceiptRow
    repository = RepositoryWorkspaceDeploymentRow
    external_receipt = (
        exists()
        .where(
            receipt.operation_id == operation.id,
            receipt.target_id == operation.target_id,
            receipt.bridge_id == operation.bridge_id,
            receipt.bridge_request_id == operation.request_id,
            receipt.bridge_receipt_id == operation.bridge_receipt_id,
            receipt.expected_manifest_digest == operation.source_manifest_digest,
            receipt.expected_manifest_digest == operation.verified_destination_digest,
            receipt.file_count == operation.file_count,
            receipt.total_bytes == operation.total_bytes,
            receipt.state == "succeeded",
            receipt.completed_at.is_not(None),
        )
        .correlate(operation)
    )
    repository_receipt = (
        exists()
        .where(
            repository.operation_id == operation.id,
            repository.state == "succeeded",
            repository.bridge_id == operation.bridge_id,
            repository.expected_manifest_digest
            == operation.verified_destination_digest,
            repository.verified_at.is_not(None),
        )
        .correlate(operation)
    )
    return or_(
        and_(
            operation.state == "succeeded",
            operation.phase == "finalize",
            operation.started_at.is_not(None),
            operation.completed_at.is_not(None),
            or_(
                operation.deployment_mode == "repository",
                and_(
                    operation.mutation_started.is_(True),
                    operation.sync_started.is_(True),
                    operation.sync_completed.is_(True),
                    operation.target_started.is_(True),
                ),
            ),
            operation.inventory_classification == "matched",
            operation.addon_enabled.is_not(None),
            or_(
                and_(
                    operation.target_class == "external_development",
                    operation.deployment_mode == "direct",
                    operation.bridge_id.is_not(None),
                    operation.bridge_request_id == operation.request_id,
                    operation.bridge_receipt_id.is_not(None),
                    operation.file_count.is_not(None),
                    operation.total_bytes.is_not(None),
                    operation.result_classification.in_(
                        tuple(sorted(_EXTERNAL_DEPLOYMENT_RESULT_CLASSIFICATIONS))
                    ),
                    external_receipt,
                ),
                and_(
                    operation.target_class == "external_development",
                    operation.deployment_mode == "repository",
                    operation.bridge_id.is_not(None),
                    operation.bridge_request_id.is_(None),
                    operation.bridge_receipt_id.is_(None),
                    operation.addon_enabled.is_(True),
                    operation.result_classification == "repository_verified",
                    operation.source_manifest_digest
                    == operation.verified_destination_digest,
                    repository_receipt,
                ),
            ),
            operation.source_manifest_digest.is_not(None),
            operation.verified_destination_digest.is_not(None),
            operation.failure_code.is_(None),
        ),
        and_(
            operation.state == "failed",
            operation.phase == "finalize",
            operation.started_at.is_not(None),
            operation.completed_at.is_not(None),
            operation.failure_code.is_not(None),
            operation.mutation_started.is_(False),
            operation.sync_started.is_(False),
            operation.sync_completed.is_(False),
            operation.target_started.is_(False),
            operation.inventory_classification == "not_checked",
            operation.addon_enabled.is_(None),
            operation.result_classification.is_(None),
            operation.source_manifest_digest.is_(None),
            operation.verified_destination_digest.is_(None),
        ),
        and_(
            operation.deployment_mode == "repository",
            operation.target_class == "external_development",
            operation.state == "failed",
            operation.phase == "finalize",
            operation.started_at.is_not(None),
            operation.completed_at.is_not(None),
            operation.failure_code.is_not(None),
            operation.mutation_started.is_(False),
            exists().where(
                repository.operation_id == operation.id,
                repository.state == "failed",
                repository.failure_code == operation.failure_code,
                repository.expected_manifest_digest == operation.source_manifest_digest,
            ),
        ),
    )


def execution_terminal_evidence_is_consistent_sql() -> ColumnElement[bool]:
    """SQL form used by bounded terminal-operation cleanup."""

    operation = McpWorkspaceExecutionOperationRow
    return or_(
        and_(
            operation.state == "succeeded",
            operation.phase == "finalize",
            operation.started_at.is_not(None),
            operation.completed_at.is_not(None),
            operation.invocation_dispatched.is_(True),
            operation.execution_classification == "directory_result",
            operation.item_count.is_not(None),
            operation.result_classification == "smoke_passed",
            operation.failure_code.is_(None),
        ),
        and_(
            operation.state == "failed",
            operation.phase == "finalize",
            operation.started_at.is_not(None),
            operation.completed_at.is_not(None),
            operation.failure_code.is_not(None),
            operation.invocation_dispatched.is_(False),
            operation.execution_classification == "not_attempted",
            operation.item_count.is_(None),
            operation.result_classification.is_(None),
        ),
    )


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
        or _state(operation.phase) != "finalize"
        or operation.lease_token_digest is not None
        or operation.lease_expires_at is not None
    ):
        return False
    operation_state = _state(operation.state)
    if operation_state == "succeeded":
        return bool(
            operation.started_at is not None
            and operation.failure_code is None
            and _state(workspace.state) != "creating"
            and workspace.baseline_manifest_digest is not None
            and workspace.current_tree_digest is not None
            and workspace.captured_at is not None
        )
    return bool(
        operation_state == "failed"
        and operation.started_at is not None
        and operation.failure_code is not None
        and _state(workspace.state) == "abandoned"
        and workspace.abandoned_at is not None
        and workspace.baseline_manifest_digest is None
        and workspace.current_tree_digest is None
        and workspace.captured_at is None
    )


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
    ):
        return False
    return bool(
        _deployment_success_evidence_is_consistent(session, operation)
        or _deployment_pre_mutation_failure_evidence_is_consistent(operation)
        or _deployment_repository_failure_evidence_is_consistent(session, operation)
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
    ):
        return False
    return bool(
        _execution_success_evidence_is_consistent(operation)
        or _execution_pre_dispatch_failure_evidence_is_consistent(operation)
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

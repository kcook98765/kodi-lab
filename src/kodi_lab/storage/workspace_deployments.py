"""Retained workspace copy evidence scoped to a historical deployment."""

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, defer
from sqlalchemy.sql.elements import ColumnElement

from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    McpWorkspaceDeploymentOperationRow,
)


def selected_profile_lineage_sql() -> ColumnElement[bool]:
    """Fresh selection only; historical replay/recovery keeps original scope.

    Active managed sessions require positive original-profile identity. Legacy
    inactive policy and external development keep their existing semantics.
    This narrows receipt selection; it supplies no authority or live proof.
    """
    row = McpWorkspaceDeploymentOperationRow
    policy = DevelopmentSessionPolicyRow
    current = DevelopmentSessionRow
    active = (
        select(policy.id)
        .where(policy.id == 1, policy.active_generation.is_not(None))
        .exists()
    )
    matching = (
        select(current.generation)
        .select_from(current)
        .join(policy, policy.active_generation == current.generation)
        .where(
            policy.id == 1,
            current.phase == "bound",
            current.container_id.is_not(None),
            row.target_id == current.target_id,
            row.runtime_revision_id == current.runtime_revision_id,
            row.workspace_id == current.workspace_id,
            row.development_source_id == current.source_id,
            row.original_container_id == current.container_id,
        )
        .correlate(row)
        .exists()
    )
    return or_(row.target_class == "external_development", ~active, matching)


def failed_verified_workspace_copy(row: McpWorkspaceDeploymentOperationRow) -> bool:
    """An explicit recovery candidate, never trusted prior-copy evidence."""
    return (
        row.state == "failed"
        and row.phase == "finalize"
        and row.target_class == "internal_lab"
        and row.deployment_mode == "direct"
        and row.failure_phase == "inventory"
        and row.failure_code == "deployment_verification_failed"
        and row.recovery_state is None
        and row.mutation_started
        and row.target_stopped
        and row.sync_started
        and row.sync_completed
        and row.target_started
        and row.inventory_classification in {"failed", "unknown"}
        and row.original_container_id is not None
        and row.source_manifest_digest is not None
        and row.verified_destination_digest is not None
        and row.result_classification
        in {"initial_deployment", "synchronized", "unchanged"}
        and row.completed_at is not None
    )


def failed_verified_workspace_copy_sql() -> ColumnElement[bool]:
    """SQL counterpart for diagnostic ordering; never confers recovery admission."""
    row = McpWorkspaceDeploymentOperationRow
    return and_(
        row.state == "failed",
        row.phase == "finalize",
        row.target_class == "internal_lab",
        row.deployment_mode == "direct",
        row.failure_phase == "inventory",
        row.failure_code == "deployment_verification_failed",
        row.recovery_state.is_(None),
        row.mutation_started.is_(True),
        row.target_stopped.is_(True),
        row.sync_started.is_(True),
        row.sync_completed.is_(True),
        row.target_started.is_(True),
        row.inventory_classification.in_(["failed", "unknown"]),
        row.original_container_id.is_not(None),
        row.source_manifest_digest.is_not(None),
        row.verified_destination_digest.is_not(None),
        row.result_classification.in_(
            ["initial_deployment", "synchronized", "unchanged"]
        ),
        row.completed_at.is_not(None),
    )


def prior_workspace_copy(
    session: Session,
    operation: McpWorkspaceDeploymentOperationRow,
    *,
    container_id: str,
    baseline_verified_at: datetime,
) -> McpWorkspaceDeploymentOperationRow | None:
    """Prefer a newer verified workspace copy over its canonical source baseline.

    The caller must establish the baseline's exact runtime/container binding.
    Historical workspace receipts without a container binding rely on that
    retained binding; a contradictory recorded container is never accepted.
    Recovery also uses this projection on pre-0090 schemas; reuse-proof JSON
    is not consumed by this stopped-tree authority decision.
    """
    row = McpWorkspaceDeploymentOperationRow
    return session.scalar(
        select(row)
        .options(defer(row.managed_verification_json))
        .where(
            row.id != operation.id,
            row.workspace_id == operation.workspace_id,
            row.target_id == operation.target_id,
            row.development_source_id == operation.development_source_id,
            row.runtime_revision_id == operation.runtime_revision_id,
            row.target_class == "internal_lab",
            row.deployment_mode == "direct",
            or_(
                row.state == "succeeded",
                (row.state == "failed")
                & row.recovery_state.in_(
                    ["contained_requested_tree", "contained_prior_tree"]
                ),
            ),
            or_(
                row.original_container_id.is_(None),
                row.original_container_id == container_id,
            ),
            row.completed_at > timestamp(baseline_verified_at),
            row.completed_at <= operation.requested_at,
            row.source_manifest_digest.is_not(None),
            row.verified_destination_digest.is_not(None),
        )
        .order_by(row.completed_at.desc(), row.id.desc())
    )

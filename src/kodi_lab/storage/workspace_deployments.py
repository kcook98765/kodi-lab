"""Retained workspace copy evidence scoped to a historical deployment."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow


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
    """
    row = McpWorkspaceDeploymentOperationRow
    return session.scalar(
        select(row)
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

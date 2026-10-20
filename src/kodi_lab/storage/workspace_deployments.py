"""Retained workspace copy evidence scoped to a historical deployment."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow


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

"""Current workspace deployment evidence shared by smoke and GUI admission."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.application.mcp_development_requests import (
    deployment_terminal_evidence_is_consistent_sql,
)
from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow


class WorkspaceDeploymentAmbiguousError(RuntimeError):
    """Receipt timestamps cannot establish an authoritative attempt."""


def latest_verified_workspace_deployment(
    session: Session,
    *,
    workspace_id: str,
    target_id: str,
    source_id: str,
    generation: int,
    exclude_operation_id: str | None = None,
) -> McpWorkspaceDeploymentOperationRow | None:
    """Inspect the latest attempt before checking success; never fall back.

    UUID order makes reads deterministic but cannot prove mutation order when
    request timestamps tie. Reject that uncertainty, even for identical bytes.
    """
    row = McpWorkspaceDeploymentOperationRow
    attempts = list(
        session.scalars(
            select(row)
            .where(
                row.workspace_id == workspace_id,
                row.target_id == target_id,
                row.id != exclude_operation_id,
                row.target_class == "external_development",
            )
            .order_by(row.requested_at.desc(), row.id.desc())
            .limit(2)
        )
    )
    if not attempts:
        return None
    if len(attempts) == 2 and attempts[0].requested_at == attempts[1].requested_at:
        raise WorkspaceDeploymentAmbiguousError
    latest = attempts[0]
    consistent = session.scalar(
        select(deployment_terminal_evidence_is_consistent_sql()).where(
            row.id == latest.id
        )
    )
    if (
        latest.state != "succeeded"
        or not consistent
        or not latest.addon_enabled
        or latest.development_source_id != source_id
        or latest.generation != generation
    ):
        return None
    return latest

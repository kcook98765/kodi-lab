"""Current workspace deployment evidence shared by smoke and GUI admission."""

from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from kodi_lab.application.mcp_development_requests import (
    deployment_terminal_evidence_is_consistent_sql,
)
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow
from kodi_lab.storage.workspace_deployments import selected_profile_lineage_sql
from kodi_lab.workspace import (
    AgentWorkspaceStorageError,
    capture_workspace_deployment_tree,
)


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
    Historical receipts from other selected profiles remain historical.
    """
    row = McpWorkspaceDeploymentOperationRow
    attempts = list(
        session.scalars(
            select(row)
            .where(
                row.workspace_id == workspace_id,
                row.target_id == target_id,
                row.id != exclude_operation_id,
                or_(
                    selected_profile_lineage_sql(),
                    # Unbound non-successes cannot authorize a profile, but
                    # must remain refusal evidence: early failures/interruption
                    # can precede recording the observed original container.
                    and_(row.state != "succeeded", row.original_container_id.is_(None)),
                ),
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


def workspace_deployment_bytes_match(
    deployment: McpWorkspaceDeploymentOperationRow,
    *,
    data_root: Path,
    workspace_digest: str | None,
    profile: RuntimeExecutionProfile,
) -> bool:
    """Prove fresh private bytes and their ownership-bound destination digest.

    The caller holds the workspace lock and separately verifies the live tree.
    """
    source = deployment.source_manifest_digest
    destination = deployment.verified_destination_digest
    if source is None or destination is None or workspace_digest is None:
        return False
    if source.removeprefix("sha256:") != workspace_digest.removeprefix("sha256:"):
        return False
    try:
        manifest = capture_workspace_deployment_tree(
            data_root, UUID(deployment.workspace_id)
        )
    except (AgentWorkspaceStorageError, OSError):
        return False
    return manifest.digest.removeprefix("sha256:") == source.removeprefix(
        "sha256:"
    ) and destination.removeprefix("sha256:") == expected_destination_digest(
        manifest, runtime_uid=profile.runtime_uid, runtime_gid=profile.runtime_gid
    ).removeprefix("sha256:")

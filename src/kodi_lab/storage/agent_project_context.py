"""Positive private-project scope; supplies no credential or native authority."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    AgentProjectRow,
    AgentWorkspaceRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
    RuntimeRevisionRow,
    TargetRow,
)


def owned_project_workspace(
    session: Session,
    *,
    workspace_id: str,
    source_id: str,
    client_id: str | None = None,
) -> bool:
    """Require the current enabled role, private owner, binding and source grant."""
    query = (
        select(AgentProjectRow.source_id)
        .join(McpClientRow, McpClientRow.id == AgentProjectRow.client_id)
        .join(
            McpAgentWorkspaceBindingRow,
            McpAgentWorkspaceBindingRow.client_id == AgentProjectRow.client_id,
        )
        .join(
            AgentWorkspaceRow,
            AgentWorkspaceRow.id == McpAgentWorkspaceBindingRow.workspace_id,
        )
        .join(
            McpClientDevelopmentSourceGrantRow,
            (McpClientDevelopmentSourceGrantRow.client_id == AgentProjectRow.client_id)
            & (McpClientDevelopmentSourceGrantRow.development_source_id == source_id),
        )
        .where(
            AgentProjectRow.source_id == source_id,
            AgentProjectRow.state == "seeded",
            McpClientRow.enabled.is_(True),
            McpClientRow.managed_lab_role_version == 1,
            AgentWorkspaceRow.id == workspace_id,
            AgentWorkspaceRow.development_source_id == source_id,
            McpAgentWorkspaceBindingRow.development_source_id == source_id,
        )
    )
    if client_id is not None:
        query = query.where(AgentProjectRow.client_id == client_id)
    return session.scalar(query) is not None


def approved_project_target(
    session: Session, target_id: str, revision_id: str | None
) -> bool:
    """External targets can never use private managed session admission."""
    return (
        session.scalar(
            select(TargetRow.id)
            .join(ManagedInstanceRow, ManagedInstanceRow.target_id == TargetRow.id)
            .join(
                RuntimeRevisionRow,
                RuntimeRevisionRow.id == ManagedInstanceRow.runtime_revision_id,
            )
            .where(
                TargetRow.id == target_id,
                TargetRow.target_type == "managed",
                ManagedInstanceRow.instance_type == "disposable",
                ManagedInstanceRow.runtime_revision_id == revision_id,
                RuntimeRevisionRow.approval_state == "approved",
            )
        )
        is not None
    )


def owned_project_operation(
    session: Session,
    *,
    kind: str,
    operation_id: str,
    target_id: str,
) -> bool:
    """Resolve a typed unclaimed parent; no caller-supplied bypass boolean."""
    row: (
        McpWorkspaceDeploymentOperationRow
        | McpWorkspaceExecutionOperationRow
        | McpWorkspaceLaunchRow
        | None
    )
    if kind == "mcp_workspace_deployment":
        row = session.get(McpWorkspaceDeploymentOperationRow, operation_id)
    elif kind == "mcp_workspace_execution":
        row = session.get(McpWorkspaceExecutionOperationRow, operation_id)
    elif kind == "mcp_workspace_launch":
        row = session.get(McpWorkspaceLaunchRow, operation_id)
    else:
        return False
    if row is None or row.state != "pending" or row.target_id != target_id:
        return False
    if not owned_project_workspace(
        session,
        workspace_id=row.workspace_id,
        source_id=row.development_source_id,
        client_id=row.client_id,
    ):
        return False
    workspace = session.get(AgentWorkspaceRow, row.workspace_id)
    revision = (
        session.get(McpWorkspaceDeploymentOperationRow, row.deployment_id)
        if isinstance(row, McpWorkspaceExecutionOperationRow)
        else row
    )
    if (
        workspace is None
        or workspace.generation != row.generation
        or workspace.state.value != "ready"
        or workspace.bytes_state.value != "present"
        or revision is None
        or revision.client_id != row.client_id
        or revision.workspace_id != row.workspace_id
        or revision.target_id != target_id
        or revision.development_source_id != row.development_source_id
        or not approved_project_target(session, target_id, revision.runtime_revision_id)
    ):
        return False
    from kodi_lab.storage.development_sessions import (
        require_workspace_session_admission,
    )

    require_workspace_session_admission(
        session,
        target_id=target_id,
        revision_id=revision.runtime_revision_id,
        workspace_id=row.workspace_id,
        source_id=row.development_source_id,
    )
    return True

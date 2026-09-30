"""Honest source, capture, validation, and deployment status for one workspace."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureService,
    WorkspaceSourceScanResult,
)
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    McpWorkspaceValidationRow,
    RepositoryWorkspaceDeploymentRow,
    TargetRow,
)
from kodi_lab.storage.repositories import AgentWorkspaceRepository


class WorkspaceCaptureStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    source_id: UUID
    source_name: str
    source_addon_id: str
    source_scan: WorkspaceSourceScanResult | None
    source_state: Literal[
        "not_freshly_scanned", "matches_active_capture", "differs_from_active_capture"
    ]
    active_capture_version: str
    active_capture_digest: str | None
    workspace_generation: int
    workspace_tree_digest: str | None
    private_edits: bool
    capture_available: bool
    capture_unavailable_reason: str | None
    recover_operation_id: UUID | None
    recovery_state: str | None
    validation_generation: int | None
    validation_digest: str | None
    validation_state: Literal["none", "current", "stale"]
    deployment_generation: int | None
    deployment_state: str | None
    verified_target_id: UUID | None
    verified_target_name: str | None
    verified_generation: int | None
    verified_state: Literal[
        "none", "current", "older_than_workspace", "older_than_scanned_source"
    ]
    reported_at: datetime


class WorkspaceCapturePresentationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        captures: WorkspaceSourceCaptureService,
    ) -> None:
        self._sessions = sessions
        self._captures = captures

    def status(
        self, workspace_id: UUID, *, scan: WorkspaceSourceScanResult | None = None
    ) -> WorkspaceCaptureStatus:
        from datetime import UTC

        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            if workspace is None:
                from kodi_lab.application.agent_workspaces import (
                    AgentWorkspaceNotFoundError,
                )

                raise AgentWorkspaceNotFoundError(workspace_id)
            if scan is not None and (
                scan.workspace_id != workspace_id
                or scan.source_id != workspace.development_source_id
            ):
                raise ValueError("source scan does not belong to workspace")
            validation = session.scalar(
                select(McpWorkspaceValidationRow)
                .where(McpWorkspaceValidationRow.workspace_id == str(workspace_id))
                .order_by(McpWorkspaceValidationRow.validated_at.desc())
                .limit(1)
            )
            deployment = session.scalar(
                select(RepositoryWorkspaceDeploymentRow)
                .where(
                    RepositoryWorkspaceDeploymentRow.workspace_id == str(workspace_id)
                )
                .order_by(RepositoryWorkspaceDeploymentRow.created_at.desc())
                .limit(1)
            )
            verified = session.scalar(
                select(RepositoryWorkspaceDeploymentRow)
                .where(
                    RepositoryWorkspaceDeploymentRow.workspace_id == str(workspace_id),
                    RepositoryWorkspaceDeploymentRow.state == "succeeded",
                )
                .order_by(RepositoryWorkspaceDeploymentRow.verified_at.desc())
                .limit(1)
            )
            target = (
                None if verified is None else session.get(TargetRow, verified.target_id)
            )
            pending = session.scalar(
                select(AgentWorkspaceCaptureOperationRow)
                .where(
                    AgentWorkspaceCaptureOperationRow.workspace_id == str(workspace_id),
                    AgentWorkspaceCaptureOperationRow.state.in_(
                        ("staging", "switching", "reconciliation_required")
                    ),
                )
                .order_by(AgentWorkspaceCaptureOperationRow.requested_at.desc())
                .limit(1)
            )
        active_digest = workspace.baseline_manifest_digest
        private_edits = (
            workspace.current_tree_digest is not None
            and active_digest is not None
            and workspace.current_tree_digest != active_digest
        )
        eligibility = self._captures.capture_eligibility(
            workspace_id, workspace.generation
        )
        if pending is not None:
            eligibility = "workspace_capture_recovery_required"
        source_state: Literal[
            "not_freshly_scanned",
            "matches_active_capture",
            "differs_from_active_capture",
        ]
        if scan is None:
            source_state = "not_freshly_scanned"
        elif scan.tree_digest == active_digest:
            source_state = "matches_active_capture"
        else:
            source_state = "differs_from_active_capture"
        validation_current = (
            validation is not None
            and validation.generation == workspace.generation
            and validation.tree_digest == workspace.current_tree_digest
        )
        verified_current = (
            verified is not None
            and verified.workspace_generation == workspace.generation
        )
        return WorkspaceCaptureStatus(
            workspace_id=workspace_id,
            source_id=workspace.development_source_id,
            source_name=workspace.source_logical_name,
            source_addon_id=workspace.source_addon_id,
            source_scan=scan,
            source_state=source_state,
            active_capture_version=(
                workspace.active_observed_version or workspace.source_observed_version
            ),
            active_capture_digest=active_digest,
            workspace_generation=workspace.generation,
            workspace_tree_digest=workspace.current_tree_digest,
            private_edits=private_edits,
            capture_available=eligibility is None,
            capture_unavailable_reason=eligibility,
            recover_operation_id=(
                UUID(pending.id)
                if pending is not None and pending.state in {"staging", "switching"}
                else None
            ),
            recovery_state=None if pending is None else pending.state,
            validation_generation=None if validation is None else validation.generation,
            validation_digest=None if validation is None else validation.tree_digest,
            validation_state=(
                "none"
                if validation is None
                else "current"
                if validation_current
                else "stale"
            ),
            deployment_generation=(
                None if deployment is None else deployment.workspace_generation
            ),
            deployment_state=None if deployment is None else deployment.state,
            verified_target_id=None if verified is None else UUID(verified.target_id),
            verified_target_name=None if target is None else target.name,
            verified_generation=(
                None if verified is None else verified.workspace_generation
            ),
            verified_state=(
                "none"
                if verified is None
                else "older_than_workspace"
                if not verified_current
                else "older_than_scanned_source"
                if source_state == "differs_from_active_capture"
                else "current"
            ),
            reported_at=datetime.now(UTC),
        )

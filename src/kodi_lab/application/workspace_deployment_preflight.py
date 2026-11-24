"""Strictly read-only durable setup assessment; never converges runtime state.

This first projection deliberately reports live native/Bridge/add-on facts as
unknown. Stored target readiness or active trust cannot prove current liveness.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.development.activation import (
    ActivationAssessment,
    assess_declared_extensions,
)
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    DevelopmentSourceDeploymentConfidence,
    TargetType,
)
from kodi_lab.storage.models import (
    McpWorkspaceValidationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)

_VERIFIED = DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION

BridgeTrustState = Literal[
    "uninstalled", "prepared", "active", "invalidated", "revoked", "unknown"
]


class DeploymentPreflightInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    expected_generation: int = Field(ge=0)
    target_id: UUID
    deployment_mode: Literal["auto", "direct", "repository"] = "auto"


class PrerequisiteCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal[
        "scope",
        "session",
        "workspace",
        "generation",
        "validation",
        "source",
        "attachment",
        "source_baseline",
        "deployment_provenance",
        "target_lease",
        "global_lease",
        "reconciliation",
        "native",
        "bridge",
        "addon",
    ]
    state: Literal["satisfied", "blocked", "unknown", "not_required"]
    reason: Literal[
        "external_development_required",
        "development_session_not_active",
        "stored_session_scope_matches",
        "session_scope_not_assessed",
        "development_session_requires_review",
        "development_session_other_target",
        "development_session_identity_changed",
        "development_session_other_workspace",
        "development_session_runtime_pending",
        "workspace_ready",
        "workspace_not_ready",
        "generation_current",
        "workspace_generation_conflict",
        "validation_current",
        "validation_required",
        "validation_generation_mismatch",
        "validation_digest_unknown",
        "validation_digest_mismatch",
        "source_current",
        "source_identity_changed",
        "source_attached",
        "source_not_attached",
        "owned_project_attachment_not_required",
        "owned_project_baseline_not_required",
        "source_baseline_missing",
        "source_baseline_stale",
        "source_baseline_unknown",
        "source_baseline_runtime_mismatch",
        "source_baseline_requires_container_proof",
        "prior_workspace_requires_container_proof",
        "deployment_provenance_review_required",
        "bridge_not_assessed",
        "target_idle",
        "target_busy",
        "global_idle",
        "global_busy",
        "reconciled",
        "reconciliation_required",
        "native_not_observed",
        "bridge_not_required_for_direct_deployment",
        "addon_not_observed",
    ]
    evidence_at: datetime | None = None


class PreflightNextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "refresh_generation",
        "validate_workspace",
        "review_source",
        "attach_source",
        "copy_source",
        "review_history",
        "review_session",
        "inspect_native",
        "enable_addon",
    ]
    authority: Literal["agent", "web_administrator"]
    target_state: Literal["stopped", "running", "any"]
    status_url: str | None = Field(default=None, max_length=200)


class DeploymentPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_revision: Literal[1] = 1
    assessment_scope: Literal["durable_prerequisites"] = "durable_prerequisites"
    workspace_id: UUID
    target_id: UUID
    source_id: UUID | None
    requested_generation: int
    current_generation: int | None
    requested_mode: Literal["auto", "direct", "repository"]
    resolved_mode: Literal["direct"] | None
    checked_at: datetime
    verdict: Literal["blocked", "unknown"]
    checks: tuple[PrerequisiteCheck, ...] = Field(max_length=16)
    next_actions: tuple[PreflightNextAction, ...] = Field(max_length=8)
    bridge_trust_state: BridgeTrustState = "unknown"
    live_bridge_readiness: Literal["unknown"] = "unknown"
    reservation_created: Literal[False] = False
    activation_assessment: ActivationAssessment | None = None


class WorkspaceDeploymentPreflightService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def inspect(
        self,
        operation: DeploymentPreflightInput,
        *,
        read_session: Session | None = None,
    ) -> DeploymentPreflightResult:
        checks: list[PrerequisiteCheck] = []
        actions: list[PreflightNextAction] = []

        def check(
            name: str, state: str, reason: str, evidence_at: datetime | None = None
        ) -> None:
            checks.append(
                PrerequisiteCheck.model_validate(
                    dict(name=name, state=state, reason=reason, evidence_at=evidence_at)
                )
            )

        def action(name: str, authority: str, state: str = "any") -> None:
            item = PreflightNextAction.model_validate(
                dict(
                    action=name,
                    authority=authority,
                    target_state=state,
                )
            )
            if item not in actions:
                actions.append(item)

        with (
            self._sessions() if read_session is None else nullcontext(read_session)
        ) as session:
            workspace = AgentWorkspaceRepository(session).get(operation.workspace_id)
            target = session.get(TargetRow, str(operation.target_id))
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
            external = (
                target is not None
                and target.target_type is TargetType.EXTERNAL_DEVELOPMENT
            )
            check(
                "scope",
                "satisfied" if external else "blocked",
                "external_development_required",
            )
            current = None if workspace is None else workspace.generation
            ready = (
                workspace is not None
                and workspace.state is AgentWorkspaceState.READY
                and workspace.bytes_state is AgentWorkspaceBytesState.PRESENT
                and workspace.current_tree_digest is not None
            )
            check(
                "workspace",
                "satisfied" if ready else "blocked",
                "workspace_ready" if ready else "workspace_not_ready",
            )
            generation_current = current == operation.expected_generation
            check(
                "generation",
                "satisfied" if generation_current else "blocked",
                "generation_current"
                if generation_current
                else "workspace_generation_conflict",
            )
            if not generation_current:
                action("refresh_generation", "agent")
            validation = session.scalar(
                select(McpWorkspaceValidationRow).where(
                    McpWorkspaceValidationRow.workspace_id
                    == str(operation.workspace_id)
                )
            )
            validation_reason = (
                "validation_required"
                if validation is None
                else "validation_generation_mismatch"
                if validation.generation != operation.expected_generation
                else "validation_digest_unknown"
                if validation.tree_digest is None
                else "validation_digest_mismatch"
                if workspace is None
                or validation.tree_digest != workspace.current_tree_digest
                else "validation_current"
            )
            check(
                "validation",
                "satisfied"
                if validation_reason == "validation_current"
                else "unknown"
                if validation_reason == "validation_digest_unknown"
                else "blocked",
                validation_reason,
            )
            if validation_reason != "validation_current":
                action("validate_workspace", "agent")
            source_current = (
                source is not None
                and workspace is not None
                and source.addon_id == workspace.source_addon_id
                and (
                    validation is None
                    or (
                        validation.development_source_id == str(source.id)
                        and validation.addon_id == source.addon_id
                    )
                )
            )
            check(
                "source",
                "satisfied" if source_current else "blocked",
                "source_current" if source_current else "source_identity_changed",
            )
            if not source_current:
                action("review_source", "web_administrator")
        check("native", "unknown", "native_not_observed")
        check("bridge", "unknown", "bridge_not_assessed")
        check("addon", "unknown", "addon_not_observed")
        return DeploymentPreflightResult(
            workspace_id=operation.workspace_id,
            target_id=operation.target_id,
            source_id=None if source is None else source.id,
            requested_generation=operation.expected_generation,
            current_generation=current,
            requested_mode=operation.deployment_mode,
            resolved_mode=None,
            checked_at=datetime.now(UTC),
            verdict="blocked"
            if any(item.state == "blocked" for item in checks)
            else "unknown",
            checks=tuple(checks),
            next_actions=tuple(actions),
            activation_assessment=(
                assess_declared_extensions(validation.extension_types_json)
                if external
                and ready
                and generation_current
                and source_current
                and validation_reason == "validation_current"
                and validation is not None
                else None
            ),
        )

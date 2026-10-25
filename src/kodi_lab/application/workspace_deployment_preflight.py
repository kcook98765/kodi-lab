"""Strictly read-only durable setup assessment; never converges runtime state.

This first projection deliberately reports live native/Bridge/add-on facts as
unknown. Stored target readiness or active trust cannot prove current liveness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    DevelopmentSourceDeploymentConfidence,
    InstanceType,
    TargetType,
)
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceValidationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
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
        "workspace",
        "generation",
        "validation",
        "source",
        "attachment",
        "source_baseline",
        "target_lease",
        "global_lease",
        "reconciliation",
        "native",
        "bridge",
        "addon",
    ]
    state: Literal["satisfied", "blocked", "unknown", "not_required"]
    reason: Literal[
        "managed_direct_only",
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
        "source_baseline_missing",
        "source_baseline_stale",
        "source_baseline_unknown",
        "source_baseline_runtime_mismatch",
        "source_baseline_requires_container_proof",
        "prior_workspace_requires_container_proof",
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


class WorkspaceDeploymentPreflightService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def inspect(self, operation: DeploymentPreflightInput) -> DeploymentPreflightResult:
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
                    status_url=f"/instances/{operation.target_id}/history"
                    if name == "review_history"
                    else None,
                )
            )
            if item not in actions:
                actions.append(item)

        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(operation.workspace_id)
            target = session.get(TargetRow, str(operation.target_id))
            instance = ManagedInstanceRepository(session).get(operation.target_id)
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
            managed = (
                target is not None
                and target.target_type is TargetType.MANAGED
                and instance is not None
                and instance.instance_type is InstanceType.DISPOSABLE
                and operation.deployment_mode != "repository"
            )
            check("scope", "satisfied" if managed else "unknown", "managed_direct_only")
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
            trust = (
                session.get(ManagedBridgeTrustRow, str(operation.target_id))
                if managed
                else None
            )
            trust_state = (
                "uninstalled"
                if managed and trust is None
                else "unknown"
                if trust is None
                else trust.state
            )
            if managed:
                assert instance is not None
                attached = (
                    source is not None
                    and ManagedInstanceDevelopmentSourceRepository(session).get(
                        operation.target_id, source.id
                    )
                    is not None
                )
                check(
                    "attachment",
                    "satisfied" if attached else "blocked",
                    "source_attached" if attached else "source_not_attached",
                )
                if not attached:
                    action("attach_source", "web_administrator")
                baseline = (
                    None
                    if source is None
                    else DevelopmentSourceDeploymentRepository(session).get(
                        operation.target_id, source.id
                    )
                )
                baseline_reason = (
                    "source_baseline_missing"
                    if baseline is None
                    else "source_baseline_runtime_mismatch"
                    if baseline.runtime_revision_id != instance.runtime_revision_id
                    else "source_baseline_unknown"
                    if baseline.confidence
                    is DevelopmentSourceDeploymentConfidence.UNKNOWN
                    else "source_baseline_stale"
                    if (baseline.confidence is not _VERIFIED)
                    else "source_baseline_requires_container_proof"
                )
                prior = (
                    None
                    if source is None
                    else session.scalar(
                        select(McpWorkspaceDeploymentOperationRow)
                        .where(
                            McpWorkspaceDeploymentOperationRow.workspace_id
                            == str(operation.workspace_id),
                            McpWorkspaceDeploymentOperationRow.target_id
                            == str(operation.target_id),
                            McpWorkspaceDeploymentOperationRow.development_source_id
                            == str(source.id),
                            McpWorkspaceDeploymentOperationRow.runtime_revision_id
                            == str(instance.runtime_revision_id),
                            (McpWorkspaceDeploymentOperationRow.state == "succeeded")
                            | (
                                (McpWorkspaceDeploymentOperationRow.state == "failed")
                                & McpWorkspaceDeploymentOperationRow.recovery_state.in_(
                                    ["contained_requested_tree", "contained_prior_tree"]
                                )
                            ),
                            McpWorkspaceDeploymentOperationRow.source_manifest_digest.is_not(
                                None
                            ),
                            McpWorkspaceDeploymentOperationRow.verified_destination_digest.is_not(
                                None
                            ),
                        )
                        .limit(1)
                    )
                )
                if (
                    prior is not None
                    and baseline is not None
                    and baseline.runtime_revision_id == instance.runtime_revision_id
                ):
                    baseline_reason = "prior_workspace_requires_container_proof"
                # A prior workspace copy may satisfy mutation admission even when
                # source confidence is stale. Without fresh container/tree proof
                # this durable-only assessment must not declare it deployable.
                check(
                    "source_baseline",
                    "unknown"
                    if baseline_reason
                    in {
                        "source_baseline_requires_container_proof",
                        "prior_workspace_requires_container_proof",
                    }
                    else "blocked",
                    baseline_reason,
                    None if baseline is None else baseline.verified_at,
                )
                if baseline_reason not in {
                    "source_baseline_requires_container_proof",
                    "prior_workspace_requires_container_proof",
                }:
                    action("copy_source", "web_administrator", "stopped")
                lease = ManagedInstanceMutationLeaseRepository(session).current(
                    operation.target_id
                )
                check(
                    "target_lease",
                    "satisfied" if lease is None else "blocked",
                    "target_idle" if lease is None else "target_busy",
                )
                global_lease = GlobalHeavyOperationLeaseRepository(session).current()
                check(
                    "global_lease",
                    "satisfied" if global_lease is None else "blocked",
                    "global_idle" if global_lease is None else "global_busy",
                )
                if lease is not None or global_lease is not None:
                    action("review_history", "web_administrator")
        if managed:
            reconciliation = ManagedInstanceReconciliationService(
                self._sessions
            ).status(operation.target_id)
            check(
                "reconciliation",
                "blocked" if reconciliation.blocking else "satisfied",
                "reconciliation_required" if reconciliation.blocking else "reconciled",
            )
            if reconciliation.blocking:
                action("review_history", "web_administrator")
        check("native", "unknown", "native_not_observed")
        check(
            "bridge",
            "not_required" if managed else "unknown",
            "bridge_not_required_for_direct_deployment"
            if managed
            else "bridge_not_assessed",
        )
        check("addon", "unknown", "addon_not_observed")
        action("inspect_native", "web_administrator")
        return DeploymentPreflightResult(
            workspace_id=operation.workspace_id,
            target_id=operation.target_id,
            source_id=None if source is None else source.id,
            requested_generation=operation.expected_generation,
            current_generation=current,
            requested_mode=operation.deployment_mode,
            resolved_mode="direct" if managed else None,
            checked_at=datetime.now(UTC),
            verdict="blocked"
            if managed and any(item.state == "blocked" for item in checks)
            else "unknown",
            checks=tuple(checks),
            next_actions=tuple(actions),
            bridge_trust_state=cast(BridgeTrustState, trust_state),
        )

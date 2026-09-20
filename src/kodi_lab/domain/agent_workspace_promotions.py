"""Exact reviewed promotion of one protected workspace into its source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class AgentWorkspacePromotionPlanState(StrEnum):
    PREPARING = "preparing"
    REVIEWABLE = "reviewable"
    CONFLICTED = "conflicted"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    APPROVED_AND_QUEUED = "approved_and_queued"
    CONSUMED = "consumed"


class AgentWorkspacePromotionChangeKind(StrEnum):
    ADD = "add"
    MODIFY = "modify"


class AgentWorkspacePromotionOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AgentWorkspacePromotionPhase(StrEnum):
    QUEUED = "queued"
    FINAL_VALIDATION = "final_validation"
    BACKUP = "backup"
    APPLICATION_MARKER = "application_marker"
    APPLY = "apply"
    FINAL_CAPTURE = "final_capture"
    EVIDENCE_TRANSITION = "evidence_transition"
    FINALIZE = "finalize"


class AgentWorkspacePromotionJournalState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    UNCERTAIN = "uncertain"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AgentWorkspacePromotionPlan:
    id: UUID
    workspace_id: UUID
    development_source_id: UUID
    target_instance_id: UUID
    workspace_generation: int
    workspace_digest: str
    baseline_artifact_id: UUID
    baseline_digest: str
    source_capture_digest: str
    addon_id: str
    current_version: str
    promoted_name: str
    promoted_version: str
    target_runtime_revision_id: UUID
    target_container_id: str
    target_deployment_operation_id: UUID
    target_source_digest: str
    target_destination_digest: str
    expected_enabled: bool
    changed_file_count: int
    review_text_bytes: int
    created_at: datetime
    reviewable_at: datetime | None
    expires_at: datetime | None
    state: AgentWorkspacePromotionPlanState
    source_operation_id: UUID | None = None
    invalidation_reason: str | None = None
    live_target_instance_id: UUID | None = None
    original_target_name: str | None = None
    originating_mcp_client_id: UUID | None = None
    originating_mcp_request_id: UUID | None = None
    originating_mcp_deployment_id: UUID | None = None
    originating_mcp_execution_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.workspace_generation < 0:
            raise ValueError("workspace generation must be non-negative")
        if not 1 <= self.changed_file_count <= 100:
            raise ValueError("promotion changed-file count is invalid")
        if not 0 <= self.review_text_bytes <= 4 * 1024 * 1024:
            raise ValueError("promotion review byte count is invalid")
        if len(self.target_container_id) != 64:
            raise ValueError("promotion target container identity is invalid")
        origin = (
            self.originating_mcp_client_id,
            self.originating_mcp_request_id,
            self.originating_mcp_deployment_id,
            self.originating_mcp_execution_id,
        )
        if any(item is not None for item in origin) and not all(
            item is not None for item in origin
        ):
            raise ValueError("promotion MCP origin must be complete")
        for field in ("created_at", "reviewable_at", "expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))


@dataclass(frozen=True, slots=True)
class AgentWorkspacePromotionOperation:
    id: UUID
    plan_id: UUID
    workspace_id: UUID
    development_source_id: UUID
    target_instance_id: UUID
    state: AgentWorkspacePromotionOperationState
    phase: AgentWorkspacePromotionPhase
    requested_at: datetime
    application_started: bool = False
    backup_artifact_id: UUID | None = None
    backup_verified: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None
    live_target_instance_id: UUID | None = None

    def __post_init__(self) -> None:
        for field in ("requested_at", "started_at", "completed_at", "lease_expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.failure_code is not None and not 1 <= len(self.failure_code) <= 128:
            raise ValueError("promotion failure code is invalid")


@dataclass(frozen=True, slots=True)
class AgentWorkspacePromotionHandoff:
    id: UUID
    operation_id: UUID
    workspace_id: UUID
    development_source_id: UUID
    target_instance_id: UUID
    runtime_revision_id: UUID
    trusted_container_id: str
    prior_deployment_operation_id: UUID
    prior_source_digest: str
    prior_destination_digest: str
    created_at: datetime
    expected_addon_id: str | None = None
    expected_name: str | None = None
    expected_version: str | None = None
    expected_enabled: bool | None = None
    verification_operation_id: UUID | None = None
    consumed_at: datetime | None = None
    live_target_instance_id: UUID | None = None

    def __post_init__(self) -> None:
        if len(self.trusted_container_id) != 64:
            raise ValueError("handoff container identity is invalid")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "consumed_at", _utc(self.consumed_at, "consumed_at"))

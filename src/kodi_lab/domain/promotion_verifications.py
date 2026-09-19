"""Durable selected-target verification after an immutable source promotion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class PromotionVerificationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class PromotionVerificationPhase(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    LOG_CURSOR = "log_cursor"
    STOP = "stop"
    SYNC = "sync"
    START = "start"
    READINESS = "readiness"
    INVENTORY = "inventory"
    LOG_WINDOW = "log_window"
    FINALIZE = "finalize"


class PromotionVerificationStageResult(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class PromotionLogResult(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    TARGET_ERRORS = "target_errors"
    UNRELATED_WARNINGS = "unrelated_warnings"
    INCOMPLETE = "incomplete"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PromotionVerificationOperation:
    id: UUID
    source_promotion_id: UUID
    handoff_id: UUID
    workspace_id: UUID
    development_source_id: UUID
    target_instance_id: UUID
    runtime_revision_id: UUID
    deployment_operation_id: UUID
    state: PromotionVerificationState
    phase: PromotionVerificationPhase
    addon_id: str
    expected_name: str
    expected_version: str
    expected_enabled: bool
    requested_at: datetime
    sync_result: PromotionVerificationStageResult = (
        PromotionVerificationStageResult.PENDING
    )
    reload_result: PromotionVerificationStageResult = (
        PromotionVerificationStageResult.PENDING
    )
    inventory_result: PromotionVerificationStageResult = (
        PromotionVerificationStageResult.PENDING
    )
    log_result: PromotionLogResult = PromotionLogResult.PENDING
    current_version: str | None = None
    current_enabled: bool | None = None
    target_error_count: int = 0
    unrelated_warning_count: int = 0
    target_error_snippets: tuple[str, ...] = ()
    stop_completed: bool = False
    sync_started: bool = False
    sync_completed: bool = False
    start_dispatched: bool = False
    handoff_consumed: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("requested_at", "started_at", "completed_at", "lease_expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if len(self.target_error_snippets) > 20:
            raise ValueError("too many retained log snippets")
        if sum(len(item.encode("utf-8")) for item in self.target_error_snippets) > 8192:
            raise ValueError("retained log snippets are too large")

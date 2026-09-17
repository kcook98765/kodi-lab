"""Typed durable history for one bounded copied-add-on smoke invocation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.development_sources import (
    normalize_development_source_logical_name,
    validate_addon_id,
    validate_observed_metadata,
)


class DevelopmentSourceSmokeTestOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DevelopmentSourceSmokeTestOperationPhase(StrEnum):
    ACQUIRE = "acquire"
    VALIDATE = "validate"
    VERIFY_PRE_EXECUTION = "verify_pre_execution"
    INVOKE = "invoke"
    VERIFY_POST_EXECUTION = "verify_post_execution"
    FINALIZE = "finalize"


class DevelopmentSourceSmokeTestOverallResult(StrEnum):
    PASSED = "passed"
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    SOURCE_TREE_CHANGED = "source_tree_changed"
    UNSUPPORTED_ADDON_TYPE = "unsupported_addon_type"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DevelopmentSourceSmokeTestExecutionResult(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    DIRECTORY_RESULT = "directory_result"
    JSONRPC_ERROR = "jsonrpc_error"
    TIMEOUT = "timeout"
    AMBIGUOUS = "ambiguous"


class DevelopmentSourceSmokeTestPostProvenance(StrEnum):
    NOT_CHECKED = "not_checked"
    MATCHED = "matched"
    CHANGED = "changed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DevelopmentSourceSmokeTestOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    source_logical_name: str
    addon_id: str
    addon_version: str
    runtime_revision_id: UUID
    requested_at: datetime
    state: DevelopmentSourceSmokeTestOperationState
    phase: DevelopmentSourceSmokeTestOperationPhase
    overall_result: DevelopmentSourceSmokeTestOverallResult | None = None
    execution_result: DevelopmentSourceSmokeTestExecutionResult = (
        DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED
    )
    post_execution_provenance: DevelopmentSourceSmokeTestPostProvenance = (
        DevelopmentSourceSmokeTestPostProvenance.NOT_CHECKED
    )
    invocation_dispatched: bool = False
    invocation_duration_ms: int | None = None
    returned_item_count: int | None = None
    blocked_dependency_id: str | None = None
    blocked_dependency_reason: str | None = None
    expected_container_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_logical_name",
            normalize_development_source_logical_name(self.source_logical_name),
        )
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "addon_version",
            validate_observed_metadata(
                self.addon_version, field="version", maximum=128
            ),
        )
        if self.blocked_dependency_id is not None:
            object.__setattr__(
                self,
                "blocked_dependency_id",
                validate_addon_id(self.blocked_dependency_id),
            )
        if (
            self.blocked_dependency_reason is not None
            and not 1 <= len(self.blocked_dependency_reason) <= 64
        ):
            raise ValueError("blocked dependency reason must be bounded")
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("smoke-test failure reason must be bounded")
        if self.expected_container_id is not None and (
            len(self.expected_container_id) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.expected_container_id
            )
        ):
            raise ValueError("expected container identity must be canonical")
        if (
            self.invocation_duration_ms is not None
            and not 0 <= self.invocation_duration_ms <= 90_000
        ):
            raise ValueError("invocation duration must be bounded")
        if (
            self.returned_item_count is not None
            and not 0 <= self.returned_item_count <= 1
        ):
            raise ValueError("returned item count must be zero or one")
        for field in (
            "requested_at",
            "started_at",
            "completed_at",
            "lease_expires_at",
        ):
            value = getattr(self, field)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"{field} must be timezone-aware")
                object.__setattr__(self, field, value.astimezone(UTC))
        if self.state is DevelopmentSourceSmokeTestOperationState.QUEUED:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("queued smoke-test operation is inconsistent")
        elif self.state is DevelopmentSourceSmokeTestOperationState.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("running smoke-test operation is inconsistent")
        elif self.completed_at is None:
            raise ValueError("terminal smoke-test operation requires completion time")
        if self.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED and (
            self.overall_result is not DevelopmentSourceSmokeTestOverallResult.PASSED
            or self.execution_result
            is not DevelopmentSourceSmokeTestExecutionResult.DIRECTORY_RESULT
            or self.post_execution_provenance
            is not DevelopmentSourceSmokeTestPostProvenance.MATCHED
            or self.failure_reason is not None
            or self.reconciliation_required
        ):
            raise ValueError("successful smoke-test operation is inconsistent")
        if (
            self.state
            is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
            and not self.reconciliation_required
        ):
            raise ValueError("reconciliation smoke-test operation is inconsistent")

    @property
    def active(self) -> bool:
        return self.state in {
            DevelopmentSourceSmokeTestOperationState.QUEUED,
            DevelopmentSourceSmokeTestOperationState.RUNNING,
        }

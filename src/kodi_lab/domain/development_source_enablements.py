"""Typed durable history for copied DevelopmentSource enablement."""

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


class DevelopmentSourceEnablementOperationState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DevelopmentSourceEnablementOperationPhase(StrEnum):
    PREFLIGHT = "preflight"
    MUTATION = "mutation"
    POSTCONDITION = "postcondition"
    FINALIZE = "finalize"


class DevelopmentSourceEnablementResult(StrEnum):
    ENABLED = "enabled"
    ALREADY_ENABLED = "already_enabled"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    REJECTED = "rejected"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class DevelopmentSourceEnablementOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    source_logical_name: str
    addon_id: str
    addon_version: str
    runtime_revision_id: UUID
    requested_at: datetime
    started_at: datetime
    state: DevelopmentSourceEnablementOperationState
    phase: DevelopmentSourceEnablementOperationPhase
    result_classification: DevelopmentSourceEnablementResult | None = None
    prior_enabled: bool | None = None
    result_enabled: bool | None = None
    enable_performed: bool = False
    service_start_authorized: bool = False
    blocked_dependency_id: str | None = None
    blocked_dependency_reason: str | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        if type(self.service_start_authorized) is not bool:
            raise ValueError("service start authorization must be Boolean")
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
            raise ValueError("enablement failure reason must be bounded")
        for field in ("requested_at", "started_at", "completed_at"):
            value = getattr(self, field)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError(f"{field} must be timezone-aware")
                object.__setattr__(self, field, value.astimezone(UTC))
        if self.state is DevelopmentSourceEnablementOperationState.RUNNING:
            if (
                self.result_classification is not None
                or self.completed_at is not None
                or self.failure_reason is not None
                or self.reconciliation_required
            ):
                raise ValueError("running enablement operation fields are inconsistent")
        elif self.result_classification is None or self.completed_at is None:
            raise ValueError("terminal enablement operation fields are incomplete")
        if self.state is DevelopmentSourceEnablementOperationState.SUCCEEDED and (
            self.result_classification
            not in {
                DevelopmentSourceEnablementResult.ENABLED,
                DevelopmentSourceEnablementResult.ALREADY_ENABLED,
            }
            or self.result_enabled is not True
            or self.failure_reason is not None
            or self.reconciliation_required
        ):
            raise ValueError("successful enablement operation is inconsistent")
        if self.state is DevelopmentSourceEnablementOperationState.FAILED and (
            self.result_classification
            not in {
                DevelopmentSourceEnablementResult.DEPENDENCY_BLOCKED,
                DevelopmentSourceEnablementResult.REJECTED,
                DevelopmentSourceEnablementResult.FAILED,
            }
            or self.failure_reason is None
            or self.reconciliation_required
        ):
            raise ValueError("failed enablement operation is inconsistent")
        if (
            self.state
            is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
            and (
                self.result_classification
                is not DevelopmentSourceEnablementResult.RECONCILIATION_REQUIRED
                or self.failure_reason is None
                or not self.reconciliation_required
            )
        ):
            raise ValueError("reconciliation enablement operation is inconsistent")

    @property
    def active(self) -> bool:
        return self.state is DevelopmentSourceEnablementOperationState.RUNNING

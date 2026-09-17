"""Durable per-instance development-source attachment metadata and history."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.development_sources import (
    normalize_development_source_logical_name,
    validate_addon_id,
)

MAX_DEVELOPMENT_SOURCE_ATTACHMENT_REASON = 256


class DevelopmentSourceAttachmentOperationKind(StrEnum):
    """Closed configuration changes represented by attachment history."""

    ATTACH = "attach"
    DETACH = "detach"


class DevelopmentSourceAttachmentOperationState(StrEnum):
    """Terminal outcomes for one synchronous attachment request."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ManagedInstanceDevelopmentSource:
    """One mode-neutral association between an instance and catalog source."""

    managed_instance_id: UUID
    development_source_id: UUID
    attached_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "attached_at", _utc(self.attached_at, "attached_at"))


@dataclass(frozen=True, slots=True)
class DevelopmentSourceAttachmentOperation:
    """Typed terminal history for one synchronous attach or detach request."""

    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    source_logical_name: str
    addon_id: str
    kind: DevelopmentSourceAttachmentOperationKind
    requested_at: datetime
    completed_at: datetime
    state: DevelopmentSourceAttachmentOperationState
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_logical_name",
            normalize_development_source_logical_name(self.source_logical_name),
        )
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self, "requested_at", _utc(self.requested_at, "requested_at")
        )
        object.__setattr__(
            self, "completed_at", _utc(self.completed_at, "completed_at")
        )
        if self.completed_at < self.requested_at:
            raise ValueError("attachment operation completion precedes request")
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("attachment operation reason must be bounded")
        if self.state is DevelopmentSourceAttachmentOperationState.SUCCEEDED:
            if self.failure_reason is not None:
                raise ValueError("succeeded attachment operation cannot have a reason")
        elif self.failure_reason is None:
            raise ValueError("failed attachment operation requires a safe reason")

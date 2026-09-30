"""Durable protected agent-workspace identity and creation history."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.development_sources import (
    validate_addon_id,
    validate_observed_metadata,
)
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest


class AgentWorkspaceState(StrEnum):
    CREATING = "creating"
    CAPTURING = "capturing"
    READY = "ready"
    PROMOTED = "promoted"
    ABANDONED = "abandoned"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AgentWorkspaceBytesState(StrEnum):
    PRESENT = "present"
    CLEANED = "cleaned"


class AgentWorkspaceCleanupState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AgentWorkspaceCleanupPhase(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    DELETING = "deleting"
    VERIFYING = "verifying"
    FINALIZE = "finalize"


class AgentWorkspaceLifecycleEventKind(StrEnum):
    ABANDONED = "abandoned"
    CLEANUP_QUEUED = "cleanup_queued"
    CLEANUP_SUCCEEDED = "cleanup_succeeded"
    CLEANUP_FAILED = "cleanup_failed"
    CLEANUP_RECONCILIATION_REQUIRED = "cleanup_reconciliation_required"


class AgentWorkspaceCreationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AgentWorkspaceCreationPhase(StrEnum):
    QUEUED = "queued"
    CAPTURING = "capturing"
    VERIFYING_SOURCE = "verifying_source"
    PUBLISHING_BASELINE = "publishing_baseline"
    CREATING_TREE = "creating_tree"
    VERIFYING_TREE = "verifying_tree"
    FINALIZE = "finalize"


class AgentWorkspaceMutationKind(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    PATCH = "patch"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AgentWorkspace:
    id: UUID
    development_source_id: UUID
    source_addon_id: str
    source_logical_name: str
    source_observed_name: str
    source_observed_version: str
    state: AgentWorkspaceState
    baseline_artifact_id: UUID
    baseline_manifest_digest: str | None
    generation: int
    current_tree_digest: str | None
    capture_started_at: datetime
    captured_at: datetime | None
    created_at: datetime
    updated_at: datetime
    creation_baseline_digest: str | None = None
    active_baseline_artifact_id: UUID | None = None
    active_capture_id: UUID | None = None
    active_observed_version: str | None = None
    bytes_state: AgentWorkspaceBytesState = AgentWorkspaceBytesState.PRESENT
    promoted_at: datetime | None = None
    abandoned_at: datetime | None = None
    cleaned_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_addon_id", validate_addon_id(self.source_addon_id)
        )
        for field, maximum in (
            ("source_logical_name", 120),
            ("source_observed_name", 256),
            ("source_observed_version", 128),
            ("active_observed_version", 128),
        ):
            if getattr(self, field) is None:
                continue
            object.__setattr__(
                self,
                field,
                validate_observed_metadata(
                    getattr(self, field), field=field, maximum=maximum
                ),
            )
        for field in (
            "baseline_manifest_digest",
            "creation_baseline_digest",
            "current_tree_digest",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, normalize_sha256_digest(value, field))
        if self.generation < 0:
            raise ValueError("workspace generation must be non-negative")
        for field in (
            "capture_started_at",
            "captured_at",
            "created_at",
            "updated_at",
            "promoted_at",
            "abandoned_at",
            "cleaned_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.state is AgentWorkspaceState.READY and (
            self.baseline_manifest_digest is None
            or self.current_tree_digest is None
            or self.captured_at is None
        ):
            raise ValueError("ready workspace requires complete baseline provenance")
        if (self.bytes_state is AgentWorkspaceBytesState.CLEANED) != (
            self.cleaned_at is not None
        ):
            raise ValueError("workspace byte state and cleanup timestamp disagree")


@dataclass(frozen=True, slots=True)
class AgentWorkspaceCleanupOperation:
    id: UUID
    workspace_id: UUID
    state: AgentWorkspaceCleanupState
    phase: AgentWorkspaceCleanupPhase
    requested_at: datetime
    deletion_started: bool = False
    expected_device: int | None = None
    expected_inode: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("requested_at", "started_at", "completed_at", "lease_expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if (self.expected_device is None) != (self.expected_inode is None):
            raise ValueError("cleanup storage identity is incomplete")
        if self.deletion_started and self.expected_device is None:
            raise ValueError("cleanup deletion requires storage identity")
        if self.failure_code is not None and not 1 <= len(self.failure_code) <= 128:
            raise ValueError("workspace cleanup failure code is invalid")


@dataclass(frozen=True, slots=True)
class AgentWorkspaceLifecycleEvent:
    id: UUID
    workspace_id: UUID
    kind: AgentWorkspaceLifecycleEventKind
    occurred_at: datetime
    cleanup_operation_id: UUID | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if self.failure_code is not None and not 1 <= len(self.failure_code) <= 128:
            raise ValueError("workspace lifecycle failure code is invalid")


@dataclass(frozen=True, slots=True)
class AgentWorkspaceCreationOperation:
    id: UUID
    workspace_id: UUID
    development_source_id: UUID
    requested_at: datetime
    state: AgentWorkspaceCreationState
    phase: AgentWorkspaceCreationPhase
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("requested_at", "started_at", "completed_at", "lease_expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.failure_code is not None and not 1 <= len(self.failure_code) <= 128:
            raise ValueError("workspace creation failure code is invalid")
        if self.lease_token_digest is not None and (
            len(self.lease_token_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.lease_token_digest
            )
        ):
            raise ValueError("workspace creation lease digest is invalid")


@dataclass(frozen=True, slots=True)
class AgentWorkspaceMutationEvent:
    id: UUID
    workspace_id: UUID
    generation: int
    kind: AgentWorkspaceMutationKind
    relative_path: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("workspace mutation generation must be positive")
        try:
            encoded = self.relative_path.encode("utf-8", "strict")
        except UnicodeEncodeError as error:
            raise ValueError("workspace mutation path is invalid") from error
        if (
            not self.relative_path
            or len(encoded) > 512
            or any(
                unicodedata.category(character) == "Cc"
                for character in self.relative_path
            )
        ):
            raise ValueError("workspace mutation path is invalid")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))

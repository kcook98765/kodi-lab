"""Immutable snapshot metadata and create-only operation records."""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.models import InstanceRuntimeState
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest


class SnapshotState(StrEnum):
    CREATING = "creating"
    AVAILABLE = "available"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class SnapshotValidationResult(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class SnapshotCreateOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SnapshotCreateOperationPhase(StrEnum):
    QUEUED = "queued"
    ACQUIRING_LEASES = "acquiring_leases"
    RESOLVING_INSTANCE = "resolving_instance"
    STOPPING = "stopping"
    CAPTURING = "capturing"
    VALIDATING_ARCHIVE = "validating_archive"
    VALIDATING_DATABASES = "validating_databases"
    PUBLISHING = "publishing"
    RESTORING_RUNNING_STATE = "restoring_running_state"
    VERIFYING_READINESS = "verifying_readiness"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ManagedInstanceMutationKind(StrEnum):
    RESET = "reset"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    LIFECYCLE = "lifecycle"
    FIXTURE_ASSIGNMENT = "fixture_assignment"
    RUNTIME_REASSIGNMENT = "runtime_reassignment"


class GlobalHeavyOperationKind(StrEnum):
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"


def normalize_snapshot_label(value: str | None) -> str | None:
    if value is None:
        return None
    label = unicodedata.normalize("NFC", value.strip())
    if not label:
        return None
    if len(label) > 120:
        raise ValueError("snapshot label must contain at most 120 characters")
    if any(unicodedata.category(character) == "Cc" for character in label):
        raise ValueError("snapshot label must not contain control characters")
    if "/" in label or "\\" in label:
        raise ValueError("snapshot label must not contain path separators")
    return label


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: UUID
    source_managed_instance_id: UUID
    runtime_revision_id: UUID
    captured_fixture_definition_id: UUID | None
    captured_at: datetime
    prior_runtime_state: InstanceRuntimeState | None
    label: str | None
    manifest_version: int
    archive_digest: str | None
    manifest_digest: str | None
    archive_size: int | None
    member_count: int | None
    state: SnapshotState
    validation_at: datetime | None
    validation_result: SnapshotValidationResult

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("snapshot captured_at must be timezone-aware")
        if self.validation_at is not None and (
            self.validation_at.tzinfo is None or self.validation_at.utcoffset() is None
        ):
            raise ValueError("snapshot validation_at must be timezone-aware")
        object.__setattr__(self, "label", normalize_snapshot_label(self.label))
        if self.manifest_version != 1:
            raise ValueError("snapshot manifest version must be 1")
        for field_name in ("archive_digest", "manifest_digest"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, normalize_sha256_digest(value, field_name)
                )
        if self.archive_size is not None and self.archive_size < 0:
            raise ValueError("snapshot archive size must not be negative")
        if self.member_count is not None and self.member_count < 1:
            raise ValueError("snapshot member count must be positive")


@dataclass(frozen=True, slots=True)
class SnapshotCreateOperation:
    id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    requested_at: datetime
    state: SnapshotCreateOperationState = SnapshotCreateOperationState.QUEUED
    phase: SnapshotCreateOperationPhase = SnapshotCreateOperationPhase.QUEUED
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.state in {
            SnapshotCreateOperationState.QUEUED,
            SnapshotCreateOperationState.RUNNING,
        }

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
    DELETING = "deleting"


class SnapshotSourceBinding(StrEnum):
    ATTACHED = "attached"
    DELETED_SOURCE = "deleted_source"


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


class SnapshotRestoreOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SnapshotRestoreOperationPhase(StrEnum):
    QUEUED = "queued"
    ACQUIRING_LEASES = "acquiring_leases"
    VALIDATING_SNAPSHOT = "validating_snapshot"
    RESOLVING_INSTANCE = "resolving_instance"
    STOPPING = "stopping"
    DEPROVISIONING = "deprovisioning"
    PROVISIONING = "provisioning"
    APPLYING_SNAPSHOT = "applying_snapshot"
    VERIFYING_FILES = "verifying_files"
    VERIFYING_DATABASES = "verifying_databases"
    RESTORING_RUNNING_STATE = "restoring_running_state"
    WAITING_READINESS = "waiting_readiness"
    FINAL_VERIFICATION = "final_verification"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SnapshotRestoreReconciliationState(StrEnum):
    OLD_CONTAINER_PRESENT = "old_container_present"
    UNPROVISIONED = "unprovisioned"
    FRESH_CONTAINER_STOPPED = "fresh_container_stopped"
    FRESH_CONTAINER_RUNNING = "fresh_container_running"
    AMBIGUOUS_TRANSITIONAL = "ambiguous_transitional"


class SnapshotDeleteOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SnapshotDeleteOperationPhase(StrEnum):
    QUEUED = "queued"
    ACQUIRING_HEAVY_LEASE = "acquiring_heavy_lease"
    VALIDATING = "validating"
    DELETING_ARTIFACT = "deleting_artifact"
    VERIFYING_ABSENCE = "verifying_absence"
    DELETING_METADATA = "deleting_metadata"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class SnapshotDeleteReconciliationState(StrEnum):
    SNAPSHOT_AND_ARTIFACT_PRESENT = "snapshot_and_artifact_present"
    SNAPSHOT_PRESENT_ARTIFACT_ABSENT = "snapshot_present_artifact_absent"
    INCONSISTENT_STORAGE = "inconsistent_storage"


class ManagedInstanceMutationKind(StrEnum):
    RESET = "reset"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    LIFECYCLE = "lifecycle"
    FIXTURE_ASSIGNMENT = "fixture_assignment"
    RUNTIME_REASSIGNMENT = "runtime_reassignment"
    INSTANCE_DELETE = "instance_delete"
    DEVELOPMENT_SOURCE_ATTACHMENT = "development_source_attachment"
    DEVELOPMENT_SOURCE_DEPLOYMENT = "development_source_deployment"
    DEVELOPMENT_SOURCE_ENABLEMENT = "development_source_enablement"


class GlobalHeavyOperationKind(StrEnum):
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SNAPSHOT_DELETE = "snapshot_delete"
    INSTANCE_DELETE = "instance_delete"
    DEVELOPMENT_SOURCE_DEPLOYMENT = "development_source_deployment"


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
    source_managed_instance_id: UUID | None
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
    original_source_managed_instance_id: UUID | None = None
    source_binding: SnapshotSourceBinding = SnapshotSourceBinding.ATTACHED
    source_instance_name: str | None = None
    source_deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("snapshot captured_at must be timezone-aware")
        if self.validation_at is not None and (
            self.validation_at.tzinfo is None or self.validation_at.utcoffset() is None
        ):
            raise ValueError("snapshot validation_at must be timezone-aware")
        if self.source_deleted_at is not None and (
            self.source_deleted_at.tzinfo is None
            or self.source_deleted_at.utcoffset() is None
        ):
            raise ValueError("snapshot source_deleted_at must be timezone-aware")
        if self.original_source_managed_instance_id is None:
            object.__setattr__(
                self,
                "original_source_managed_instance_id",
                self.source_managed_instance_id,
            )
        if self.original_source_managed_instance_id is None:
            raise ValueError("snapshot original source identity is required")
        if self.source_binding is SnapshotSourceBinding.ATTACHED:
            if (
                self.source_managed_instance_id is None
                or self.source_managed_instance_id
                != self.original_source_managed_instance_id
                or self.source_instance_name is not None
                or self.source_deleted_at is not None
            ):
                raise ValueError("attached snapshot source identity is inconsistent")
        elif (
            self.source_managed_instance_id is not None
            or self.source_instance_name is None
            or not self.source_instance_name.strip()
            or self.source_deleted_at is None
        ):
            raise ValueError("deleted-source snapshot identity is inconsistent")
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


@dataclass(frozen=True, slots=True)
class SnapshotRestoreOperation:
    id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    requested_at: datetime
    state: SnapshotRestoreOperationState = SnapshotRestoreOperationState.QUEUED
    phase: SnapshotRestoreOperationPhase = SnapshotRestoreOperationPhase.QUEUED
    prior_source_runtime_state: InstanceRuntimeState | None = None
    old_container_id: str | None = None
    fresh_container_id: str | None = None
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
            SnapshotRestoreOperationState.QUEUED,
            SnapshotRestoreOperationState.RUNNING,
        }


@dataclass(frozen=True, slots=True)
class SnapshotDeleteOperation:
    id: UUID
    snapshot_id: UUID
    managed_instance_id: UUID
    prior_snapshot_state: SnapshotState
    requested_at: datetime
    state: SnapshotDeleteOperationState = SnapshotDeleteOperationState.QUEUED
    phase: SnapshotDeleteOperationPhase = SnapshotDeleteOperationPhase.QUEUED
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
            SnapshotDeleteOperationState.QUEUED,
            SnapshotDeleteOperationState.RUNNING,
        }


class ManagedInstanceDeleteOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ManagedInstanceDeleteOperationPhase(StrEnum):
    QUEUED = "queued"
    ACQUIRING_LEASES = "acquiring_leases"
    RESOLVING_INSTANCE = "resolving_instance"
    STOPPING = "stopping"
    DEPROVISIONING = "deprovisioning"
    VERIFYING_CONTAINER_ABSENCE = "verifying_container_absence"
    PRESERVING_SNAPSHOTS = "preserving_snapshots"
    DELETING_SNAPSHOTS = "deleting_snapshots"
    VERIFYING_SNAPSHOT_ABSENCE = "verifying_snapshot_absence"
    DELETING_METADATA = "deleting_metadata"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ManagedInstanceDeletePriorState(StrEnum):
    UNPROVISIONED = "unprovisioned"
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ManagedInstanceDeleteOperation:
    id: UUID
    original_managed_instance_id: UUID
    source_instance_name: str
    runtime_revision_id: UUID
    fixture_definition_id: UUID | None
    preserve_snapshots: bool
    snapshot_count: int
    aggregate_snapshot_bytes: int
    requested_at: datetime
    current_snapshot_id: UUID | None = None
    completed_snapshot_count: int = 0
    expected_container_id: str | None = None
    observed_container_id: str | None = None
    prior_state: ManagedInstanceDeletePriorState | None = None
    notes_at_deletion: str | None = None
    state: ManagedInstanceDeleteOperationState = (
        ManagedInstanceDeleteOperationState.QUEUED
    )
    phase: ManagedInstanceDeleteOperationPhase = (
        ManagedInstanceDeleteOperationPhase.QUEUED
    )
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.source_instance_name) <= 256:
            raise ValueError("delete operation source instance name must be bounded")
        if (
            self.snapshot_count < 0
            or not 0 <= self.completed_snapshot_count <= self.snapshot_count
        ):
            raise ValueError("delete operation snapshot progress is invalid")
        if self.aggregate_snapshot_bytes < 0:
            raise ValueError("delete operation aggregate bytes must not be negative")
        if self.notes_at_deletion is not None and len(self.notes_at_deletion) > 4096:
            raise ValueError(
                "delete operation notes must contain at most 4096 code points"
            )
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("delete operation failure reason must be bounded")

    @property
    def active(self) -> bool:
        return self.state in {
            ManagedInstanceDeleteOperationState.QUEUED,
            ManagedInstanceDeleteOperationState.RUNNING,
        }

"""Immutable uploaded-ZIP artifacts and typed installation history."""

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
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest


class AddonZipOrigin(StrEnum):
    UPLOADED_ZIP = "uploaded_zip"
    REPOSITORY_DOWNLOAD = "repository_download"


class AddonZipValidationState(StrEnum):
    ACCEPTED = "accepted"


class AddonZipExtensionClassification(StrEnum):
    PYTHON_VIDEO_PLUGIN = "python_video_plugin"


class AddonZipImportOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AddonZipImportOperationPhase(StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    FINALIZE = "finalize"


class AddonZipInstallationConfidence(StrEnum):
    VERIFIED_AT_LAST_OPERATION = "verified_at_last_operation"
    STALE = "stale"
    UNKNOWN = "unknown"


class AddonZipInstallationResult(StrEnum):
    INSTALLED_RECOGNIZED_DISABLED = "installed_recognized_disabled"
    REPOSITORY_REPLACEMENT_RECOGNIZED = "repository_replacement_recognized"


class AddonZipInstallationOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AddonZipInstallationOperationPhase(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    STOPPING = "stopping"
    APPLYING = "applying"
    VERIFY_STOPPED = "verify_stopped"
    RESTARTING = "restarting"
    READINESS = "readiness"
    RECOGNITION = "recognition"
    VERIFY_FINAL = "verify_final"
    FINALIZE = "finalize"


class AddonZipEnablementOperationState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AddonZipEnablementOperationPhase(StrEnum):
    PREFLIGHT = "preflight"
    PAYLOAD_VERIFICATION = "payload_verification"
    MUTATION = "mutation"
    POSTCONDITION = "postcondition"
    FINALIZE = "finalize"


class AddonZipEnablementResult(StrEnum):
    ENABLED = "enabled"
    ALREADY_ENABLED = "already_enabled"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    REJECTED = "rejected"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AddonZipPayloadVerificationOutcome(StrEnum):
    NOT_CHECKED = "not_checked"
    MATCHED = "matched"
    CHANGED = "changed"
    UNKNOWN = "unknown"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _container_id(value: str | None) -> str | None:
    if value is not None and (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("container identity must be canonical")
    return value


@dataclass(frozen=True, slots=True)
class AddonZipDependency:
    dependency_id: str
    version: str | None
    minversion: str | None
    optional: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependency_id", validate_addon_id(self.dependency_id))
        for field in ("version", "minversion"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self,
                    field,
                    validate_observed_metadata(
                        value, field=f"dependency {field}", maximum=128
                    ),
                )


@dataclass(frozen=True, slots=True)
class AddonZipArtifact:
    id: UUID
    logical_label: str
    sha256_digest: str
    compressed_size: int
    total_uncompressed_bytes: int
    member_count: int
    addon_id: str
    addon_name: str
    addon_version: str
    extension_classification: AddonZipExtensionClassification
    dependencies: tuple[AddonZipDependency, ...]
    payload_manifest_digest: str
    imported_at: datetime
    origin: AddonZipOrigin = AddonZipOrigin.UPLOADED_ZIP
    validation_state: AddonZipValidationState = AddonZipValidationState.ACCEPTED
    repository_source_id: UUID | None = None
    repository_snapshot_id: UUID | None = None
    repository_candidate_id: UUID | None = None
    repository_fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "logical_label",
            normalize_development_source_logical_name(self.logical_label),
        )
        object.__setattr__(
            self,
            "sha256_digest",
            normalize_sha256_digest(self.sha256_digest, "sha256_digest"),
        )
        object.__setattr__(
            self,
            "payload_manifest_digest",
            normalize_sha256_digest(
                self.payload_manifest_digest, "payload_manifest_digest"
            ),
        )
        if not 1 <= self.compressed_size <= 256 * 1024 * 1024:
            raise ValueError("compressed artifact size is outside the fixed bound")
        if not 0 <= self.total_uncompressed_bytes <= 1024 * 1024 * 1024:
            raise ValueError("uncompressed artifact size is outside the fixed bound")
        if not 1 <= self.member_count <= 50_000:
            raise ValueError("artifact member count is outside the fixed bound")
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "addon_name",
            validate_observed_metadata(self.addon_name, field="name", maximum=256),
        )
        object.__setattr__(
            self,
            "addon_version",
            validate_observed_metadata(
                self.addon_version, field="version", maximum=128
            ),
        )
        if len(self.dependencies) > 256 or len(
            {item.dependency_id for item in self.dependencies}
        ) != len(self.dependencies):
            raise ValueError("artifact dependencies must be bounded and unique")
        object.__setattr__(
            self,
            "dependencies",
            tuple(sorted(self.dependencies, key=lambda item: item.dependency_id)),
        )
        normalized = _utc(self.imported_at, "imported_at")
        assert normalized is not None
        object.__setattr__(self, "imported_at", normalized)
        lineage = (
            self.repository_source_id,
            self.repository_snapshot_id,
            self.repository_candidate_id,
            self.repository_fetched_at,
        )
        if self.origin is AddonZipOrigin.UPLOADED_ZIP and any(
            value is not None for value in lineage
        ):
            raise ValueError("uploaded ZIP artifact cannot carry repository lineage")
        if self.origin is AddonZipOrigin.REPOSITORY_DOWNLOAD and any(
            value is None for value in lineage
        ):
            raise ValueError("repository ZIP artifact requires exact lineage")
        if self.repository_fetched_at is not None:
            object.__setattr__(
                self,
                "repository_fetched_at",
                _utc(self.repository_fetched_at, "repository_fetched_at"),
            )


@dataclass(frozen=True, slots=True)
class AddonZipImportOperation:
    id: UUID
    artifact_id: UUID
    logical_label: str
    requested_at: datetime
    state: AddonZipImportOperationState
    phase: AddonZipImportOperationPhase
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "logical_label",
            normalize_development_source_logical_name(self.logical_label),
        )
        for field in ("requested_at", "started_at", "completed_at", "lease_expires_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("ZIP import failure reason must be bounded")


@dataclass(frozen=True, slots=True)
class AddonZipInstallationEvidence:
    managed_instance_id: UUID
    original_managed_instance_id: UUID
    artifact_id: UUID
    installation_operation_id: UUID | None
    runtime_revision_id: UUID
    trusted_container_id: str
    addon_id: str
    addon_version: str
    payload_manifest_digest: str
    verified_at: datetime
    confidence: AddonZipInstallationConfidence
    result: AddonZipInstallationResult
    artifact_origin: AddonZipOrigin = AddonZipOrigin.UPLOADED_ZIP
    repository_source_id: UUID | None = None
    repository_snapshot_id: UUID | None = None
    repository_candidate_id: UUID | None = None
    replacement_operation_id: UUID | None = None

    def __post_init__(self) -> None:
        _container_id(self.trusted_container_id)
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "addon_version",
            validate_observed_metadata(
                self.addon_version, field="version", maximum=128
            ),
        )
        object.__setattr__(
            self,
            "payload_manifest_digest",
            normalize_sha256_digest(
                self.payload_manifest_digest, "payload_manifest_digest"
            ),
        )
        normalized = _utc(self.verified_at, "verified_at")
        assert normalized is not None
        object.__setattr__(self, "verified_at", normalized)
        lineage = (
            self.repository_source_id,
            self.repository_snapshot_id,
            self.repository_candidate_id,
        )
        if self.artifact_origin is AddonZipOrigin.UPLOADED_ZIP and any(
            value is not None for value in lineage
        ):
            raise ValueError("uploaded ZIP evidence cannot carry repository lineage")
        if self.artifact_origin is AddonZipOrigin.REPOSITORY_DOWNLOAD and any(
            value is None for value in lineage
        ):
            raise ValueError("repository ZIP evidence requires exact lineage")
        if (self.installation_operation_id is None) == (
            self.replacement_operation_id is None
        ):
            raise ValueError("ZIP evidence requires exactly one producing operation")
        if (
            self.result is AddonZipInstallationResult.REPOSITORY_REPLACEMENT_RECOGNIZED
        ) != (self.replacement_operation_id is not None):
            raise ValueError("replacement evidence result is inconsistent")


@dataclass(frozen=True, slots=True)
class AddonZipInstallationOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    artifact_id: UUID
    addon_id: str
    addon_version: str
    runtime_revision_id: UUID
    requested_at: datetime
    state: AddonZipInstallationOperationState
    phase: AddonZipInstallationOperationPhase
    expected_container_id: str | None = None
    application_started: bool = False
    stopped_readback_matched: bool = False
    final_readback_matched: bool = False
    recognition_enabled: bool | None = None
    dependency_baseline_json: str | None = None
    dependency_post_json: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "addon_version",
            validate_observed_metadata(
                self.addon_version, field="version", maximum=128
            ),
        )
        object.__setattr__(
            self, "expected_container_id", _container_id(self.expected_container_id)
        )
        for field in ("dependency_baseline_json", "dependency_post_json"):
            value = getattr(self, field)
            if value is not None and not 2 <= len(value.encode("utf-8")) <= 16 * 1024:
                raise ValueError("dependency observation must be bounded")
        for field in (
            "requested_at",
            "started_at",
            "heartbeat_at",
            "completed_at",
            "lease_expires_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("ZIP installation failure reason must be bounded")

    @property
    def active(self) -> bool:
        return self.state in {
            AddonZipInstallationOperationState.QUEUED,
            AddonZipInstallationOperationState.RUNNING,
        }


@dataclass(frozen=True, slots=True)
class AddonZipEnablementOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    artifact_id: UUID
    installation_operation_id: UUID
    addon_id: str
    addon_version: str
    runtime_revision_id: UUID
    requested_at: datetime
    started_at: datetime
    state: AddonZipEnablementOperationState
    phase: AddonZipEnablementOperationPhase
    expected_container_id: str | None = None
    result_classification: AddonZipEnablementResult | None = None
    payload_verification: AddonZipPayloadVerificationOutcome = (
        AddonZipPayloadVerificationOutcome.NOT_CHECKED
    )
    prior_enabled: bool | None = None
    result_enabled: bool | None = None
    enable_performed: bool = False
    blocked_dependency_id: str | None = None
    blocked_dependency_reason: str | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "addon_version",
            validate_observed_metadata(
                self.addon_version, field="version", maximum=128
            ),
        )
        object.__setattr__(
            self, "expected_container_id", _container_id(self.expected_container_id)
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
            raise ValueError("ZIP enablement failure reason must be bounded")
        for field in ("requested_at", "started_at", "completed_at"):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.state is AddonZipEnablementOperationState.RUNNING:
            if (
                self.result_classification is not None
                or self.completed_at is not None
                or self.failure_reason is not None
                or self.reconciliation_required
            ):
                raise ValueError("running ZIP enablement fields are inconsistent")
        elif self.result_classification is None or self.completed_at is None:
            raise ValueError("terminal ZIP enablement fields are incomplete")
        if self.state is AddonZipEnablementOperationState.SUCCEEDED and (
            self.result_classification
            not in {
                AddonZipEnablementResult.ENABLED,
                AddonZipEnablementResult.ALREADY_ENABLED,
            }
            or self.payload_verification
            is not AddonZipPayloadVerificationOutcome.MATCHED
            or self.result_enabled is not True
            or self.failure_reason is not None
            or self.reconciliation_required
        ):
            raise ValueError("successful ZIP enablement is inconsistent")
        if self.result_classification is AddonZipEnablementResult.ENABLED and (
            self.prior_enabled is not False or not self.enable_performed
        ):
            raise ValueError("enabled ZIP operation must record one mutation")
        if self.result_classification is AddonZipEnablementResult.ALREADY_ENABLED and (
            self.prior_enabled is not True or self.enable_performed
        ):
            raise ValueError("already-enabled ZIP operation must be idempotent")
        if self.state is AddonZipEnablementOperationState.FAILED and (
            self.result_classification
            not in {
                AddonZipEnablementResult.DEPENDENCY_BLOCKED,
                AddonZipEnablementResult.REJECTED,
                AddonZipEnablementResult.FAILED,
            }
            or self.failure_reason is None
            or self.reconciliation_required
        ):
            raise ValueError("failed ZIP enablement is inconsistent")
        if self.state is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED and (
            self.result_classification
            is not AddonZipEnablementResult.RECONCILIATION_REQUIRED
            or self.failure_reason is None
            or not self.reconciliation_required
            or not self.enable_performed
        ):
            raise ValueError("reconciliation ZIP enablement is inconsistent")

    @property
    def active(self) -> bool:
        return self.state is AddonZipEnablementOperationState.RUNNING

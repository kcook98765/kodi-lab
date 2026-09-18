"""Persisted external HTTPS repository identity and immutable discovery state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.addon_zips import AddonZipDependency
from kodi_lab.domain.development_sources import (
    normalize_development_source_logical_name,
    validate_addon_id,
    validate_observed_metadata,
)
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest


class AddonRepositoryOrigin(StrEnum):
    EXTERNAL_HTTPS_REPOSITORY = "external_https_repository"


class AddonRepositoryCandidateClassification(StrEnum):
    PYTHON_VIDEO_PLUGIN = "python_video_plugin"
    UNSUPPORTED = "unsupported"


class AddonRepositoryCandidateSupport(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class AddonRepositoryOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AddonRepositoryAttachmentKind(StrEnum):
    ATTACH = "attach"
    DETACH = "detach"


class AddonRepositoryAttachmentState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AddonRepositoryRefreshPhase(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    FETCHING = "fetching"
    PARSING = "parsing"
    PUBLISHING = "publishing"
    FINALIZE = "finalize"


class AddonRepositoryFetchPhase(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    FETCHING = "fetching"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    FINALIZE = "finalize"


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AddonRepositorySource:
    id: UUID
    logical_name: str
    canonical_index_url: str
    created_at: datetime
    origin: AddonRepositoryOrigin = AddonRepositoryOrigin.EXTERNAL_HTTPS_REPOSITORY

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "logical_name",
            normalize_development_source_logical_name(self.logical_name),
        )
        if not 1 <= len(self.canonical_index_url.encode("utf-8")) <= 2304:
            raise ValueError("repository index URL must be bounded")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class AddonRepositoryAttachmentOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    repository_source_id: UUID
    repository_logical_name: str
    kind: AddonRepositoryAttachmentKind
    requested_at: datetime
    completed_at: datetime
    state: AddonRepositoryAttachmentState
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requested_at", _utc(self.requested_at, "requested_at")
        )
        object.__setattr__(
            self, "completed_at", _utc(self.completed_at, "completed_at")
        )
        if not 1 <= len(self.repository_logical_name) <= 120:
            raise ValueError("repository attachment name must be bounded")
        if self.completed_at < self.requested_at:
            raise ValueError("repository attachment completion precedes request")
        if self.state is AddonRepositoryAttachmentState.SUCCEEDED:
            if self.failure_reason is not None:
                raise ValueError("successful repository attachment has a reason")
        elif self.failure_reason is None or not 1 <= len(self.failure_reason) <= 128:
            raise ValueError("failed repository attachment needs a safe reason")


@dataclass(frozen=True, slots=True)
class ManagedInstanceAddonRepositorySource:
    managed_instance_id: UUID
    repository_source_id: UUID
    attached_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "attached_at", _utc(self.attached_at, "attached_at"))


@dataclass(frozen=True, slots=True)
class AddonRepositoryMetadataSnapshot:
    id: UUID
    repository_source_id: UUID
    fetched_at: datetime
    transfer_digest: str
    canonical_metadata_digest: str
    transfer_size: int
    expanded_size: int
    entry_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transfer_digest",
            normalize_sha256_digest(self.transfer_digest, "transfer_digest"),
        )
        object.__setattr__(
            self,
            "canonical_metadata_digest",
            normalize_sha256_digest(
                self.canonical_metadata_digest, "canonical_metadata_digest"
            ),
        )
        if not 1 <= self.transfer_size <= 8 * 1024 * 1024:
            raise ValueError("repository transfer size is outside the fixed bound")
        if not 1 <= self.expanded_size <= 32 * 1024 * 1024:
            raise ValueError("repository expanded size is outside the fixed bound")
        if not 0 <= self.entry_count <= 2048:
            raise ValueError("repository entry count is outside the fixed bound")
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at, "fetched_at"))


@dataclass(frozen=True, slots=True)
class AddonRepositoryCandidate:
    id: UUID
    snapshot_id: UUID
    repository_source_id: UUID
    addon_id: str
    name: str
    version: str
    classification: AddonRepositoryCandidateClassification
    dependencies: tuple[AddonZipDependency, ...]
    support: AddonRepositoryCandidateSupport
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "name",
            validate_observed_metadata(self.name, field="name", maximum=256),
        )
        object.__setattr__(
            self,
            "version",
            validate_observed_metadata(self.version, field="version", maximum=128),
        )
        if len(self.dependencies) > 256 or len(
            {item.dependency_id for item in self.dependencies}
        ) != len(self.dependencies):
            raise ValueError("repository candidate dependencies must be bounded")
        object.__setattr__(
            self,
            "dependencies",
            tuple(sorted(self.dependencies, key=lambda item: item.dependency_id)),
        )
        if self.support is AddonRepositoryCandidateSupport.SUPPORTED:
            if (
                self.classification
                is not AddonRepositoryCandidateClassification.PYTHON_VIDEO_PLUGIN
                or self.unsupported_reason is not None
            ):
                raise ValueError("supported repository candidate is inconsistent")
        elif (
            self.unsupported_reason is None
            or not 1 <= len(self.unsupported_reason) <= 64
        ):
            raise ValueError("unsupported repository candidate needs a safe reason")


@dataclass(frozen=True, slots=True)
class AddonRepositoryRefreshOperation:
    id: UUID
    repository_source_id: UUID
    requested_at: datetime
    state: AddonRepositoryOperationState
    phase: AddonRepositoryRefreshPhase
    snapshot_id: UUID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requested_at", _utc(self.requested_at, "requested_at")
        )
        for field in ("started_at", "completed_at", "lease_expires_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _utc(value, field))
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 128:
            raise ValueError("repository refresh failure reason must be bounded")


@dataclass(frozen=True, slots=True)
class AddonRepositoryFetchOperation:
    id: UUID
    repository_source_id: UUID
    snapshot_id: UUID
    candidate_id: UUID
    artifact_id: UUID
    requested_at: datetime
    state: AddonRepositoryOperationState
    phase: AddonRepositoryFetchPhase
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requested_at", _utc(self.requested_at, "requested_at")
        )
        for field in ("started_at", "completed_at", "lease_expires_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _utc(value, field))
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 128:
            raise ValueError("repository fetch failure reason must be bounded")

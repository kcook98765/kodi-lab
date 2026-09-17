"""Durable stopped-only development-source deployment evidence and history."""

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


class DevelopmentSourceDeploymentConfidence(StrEnum):
    VERIFIED_AT_LAST_OPERATION = "verified_at_last_operation"
    STALE = "stale"
    UNKNOWN = "unknown"


class DevelopmentSourceDeploymentMode(StrEnum):
    COPY = "copy"


class DevelopmentSourceDeploymentRequestKind(StrEnum):
    COPY_SYNC = "copy_sync"


class DevelopmentSourceDeploymentResult(StrEnum):
    INITIAL_DEPLOYMENT = "initial_deployment"
    SYNCHRONIZED = "synchronized"
    UNCHANGED = "unchanged"


class DevelopmentSourceDeploymentOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class DevelopmentSourceDeploymentOperationPhase(StrEnum):
    ACQUIRE = "acquire"
    RESOLVE = "resolve"
    STAGE = "stage"
    INSPECT_DESTINATION = "inspect_destination"
    APPLY = "apply"
    VERIFY = "verify"
    FINALIZE = "finalize"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDeployment:
    managed_instance_id: UUID
    development_source_id: UUID
    addon_id: str
    mode: DevelopmentSourceDeploymentMode
    last_successful_operation_id: UUID
    source_manifest_digest: str
    verified_destination_digest: str
    runtime_revision_id: UUID
    trusted_container_id: str
    observed_source_name: str
    observed_source_version: str
    verified_at: datetime
    confidence: DevelopmentSourceDeploymentConfidence

    def __post_init__(self) -> None:
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        for field in ("source_manifest_digest", "verified_destination_digest"):
            object.__setattr__(
                self, field, normalize_sha256_digest(getattr(self, field), field)
            )
        if len(self.trusted_container_id) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.trusted_container_id
        ):
            raise ValueError("trusted container identity must be canonical")
        object.__setattr__(
            self,
            "observed_source_name",
            validate_observed_metadata(
                self.observed_source_name, field="name", maximum=256
            ),
        )
        object.__setattr__(
            self,
            "observed_source_version",
            validate_observed_metadata(
                self.observed_source_version, field="version", maximum=128
            ),
        )
        normalized = _utc(self.verified_at, "verified_at")
        assert normalized is not None
        object.__setattr__(self, "verified_at", normalized)


@dataclass(frozen=True, slots=True)
class DevelopmentSourceDeploymentOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    development_source_id: UUID
    source_logical_name: str
    addon_id: str
    runtime_revision_id: UUID
    request_kind: DevelopmentSourceDeploymentRequestKind
    requested_at: datetime
    state: DevelopmentSourceDeploymentOperationState
    phase: DevelopmentSourceDeploymentOperationPhase
    result_classification: DevelopmentSourceDeploymentResult | None = None
    new_count: int | None = None
    changed_count: int | None = None
    unchanged_count: int | None = None
    destination_only_count: int | None = None
    transferred_bytes: int | None = None
    expected_container_id: str | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
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
        for field in (
            "requested_at",
            "started_at",
            "heartbeat_at",
            "completed_at",
            "lease_expires_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        for field in (
            "new_count",
            "changed_count",
            "unchanged_count",
            "destination_only_count",
            "transferred_bytes",
        ):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValueError("deployment aggregate counts must not be negative")
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("deployment failure reason must be bounded")

    @property
    def active(self) -> bool:
        return self.state in {
            DevelopmentSourceDeploymentOperationState.QUEUED,
            DevelopmentSourceDeploymentOperationState.RUNNING,
        }

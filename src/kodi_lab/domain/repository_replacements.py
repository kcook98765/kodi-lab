"""Exact one-shot repository replacement of an attached DevelopmentSource."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.development_sources import (
    validate_addon_id,
    validate_observed_metadata,
)
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest


class RepositoryReplacementIncumbentClass(StrEnum):
    ATTACHED_UNMATERIALIZED = "attached_unmaterialized"
    VERIFIED_COPIED = "verified_copied"


class RepositoryReplacementPlanState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"


class RepositoryReplacementTerminalOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class RepositoryReplacementOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class RepositoryReplacementOperationPhase(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    STOPPING = "stopping"
    REVALIDATE_STOPPED = "revalidate_stopped"
    APPLYING = "applying"
    VERIFY_STOPPED = "verify_stopped"
    RESTARTING = "restarting"
    READINESS = "readiness"
    RECOGNITION = "recognition"
    VERIFY_FINAL = "verify_final"
    FINALIZE = "finalize"


def _utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _container(value: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("container identity must be canonical")
    return value


@dataclass(frozen=True, slots=True)
class RepositoryReplacementPlan:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    repository_source_id: UUID
    repository_attachment_at: datetime
    repository_snapshot_id: UUID
    repository_candidate_id: UUID
    incoming_artifact_id: UUID
    addon_id: str
    incumbent_class: RepositoryReplacementIncumbentClass
    development_source_id: UUID
    development_source_attachment_at: datetime
    development_deployment_operation_id: UUID | None
    development_source_manifest_digest: str | None
    development_destination_digest: str | None
    incumbent_version: str | None
    incumbent_type: str | None
    incumbent_enabled: bool | None
    runtime_revision_id: UUID
    expected_container_id: str
    qualifying_snapshot_id: UUID | None
    incoming_version: str
    incoming_payload_manifest_digest: str
    created_at: datetime
    expires_at: datetime
    state: RepositoryReplacementPlanState
    consumed_execution_operation_id: UUID | None = None
    terminal_outcome: RepositoryReplacementTerminalOutcome | None = None
    invalidation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "incoming_version",
            validate_observed_metadata(
                self.incoming_version, field="version", maximum=128
            ),
        )
        if self.incumbent_version is not None:
            object.__setattr__(
                self,
                "incumbent_version",
                validate_observed_metadata(
                    self.incumbent_version, field="incumbent version", maximum=128
                ),
            )
        if self.incumbent_type is not None and not 1 <= len(self.incumbent_type) <= 128:
            raise ValueError("incumbent type must be bounded")
        for field in (
            "development_source_manifest_digest",
            "development_destination_digest",
            "incoming_payload_manifest_digest",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, normalize_sha256_digest(value, field))
        object.__setattr__(
            self, "expected_container_id", _container(self.expected_container_id)
        )
        for field in (
            "repository_attachment_at",
            "development_source_attachment_at",
            "created_at",
            "expires_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.expires_at <= self.created_at:
            raise ValueError("replacement plan expiry must follow creation")
        if (
            self.invalidation_reason is not None
            and not 1 <= len(self.invalidation_reason) <= 128
        ):
            raise ValueError("replacement plan invalidation reason must be bounded")
        copied = (
            self.incumbent_class is RepositoryReplacementIncumbentClass.VERIFIED_COPIED
        )
        copied_fields = (
            self.development_deployment_operation_id,
            self.development_source_manifest_digest,
            self.development_destination_digest,
            self.incumbent_version,
            self.incumbent_type,
            self.incumbent_enabled,
            self.qualifying_snapshot_id,
        )
        if copied != all(value is not None for value in copied_fields):
            raise ValueError("replacement incumbent binding is inconsistent")
        if not copied and any(value is not None for value in copied_fields):
            raise ValueError("unmaterialized incumbent cannot carry copied evidence")


@dataclass(frozen=True, slots=True)
class RepositoryReplacementOperation:
    id: UUID
    plan_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    incoming_artifact_id: UUID
    development_source_id: UUID
    addon_id: str
    incoming_version: str
    incumbent_class: RepositoryReplacementIncumbentClass
    runtime_revision_id: UUID
    expected_container_id: str
    requested_at: datetime
    state: RepositoryReplacementOperationState
    phase: RepositoryReplacementOperationPhase
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
            "incoming_version",
            validate_observed_metadata(
                self.incoming_version, field="version", maximum=128
            ),
        )
        object.__setattr__(
            self, "expected_container_id", _container(self.expected_container_id)
        )
        for field in (
            "requested_at",
            "started_at",
            "heartbeat_at",
            "completed_at",
            "lease_expires_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        for field in ("dependency_baseline_json", "dependency_post_json"):
            value = getattr(self, field)
            if value is not None and not 2 <= len(value.encode("utf-8")) <= 16 * 1024:
                raise ValueError("replacement dependency evidence must be bounded")
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("replacement failure reason must be bounded")

    @property
    def active(self) -> bool:
        return self.state in {
            RepositoryReplacementOperationState.QUEUED,
            RepositoryReplacementOperationState.RUNNING,
        }

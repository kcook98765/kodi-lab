"""Durable, reset-specific operation coordination records."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class ResetOperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ResetOperationPhase(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    STOPPING = "stopping"
    DEPROVISIONING = "deprovisioning"
    PROVISIONING = "provisioning"
    APPLYING_FIXTURE = "applying_fixture"
    STARTING_FOR_FIXTURE = "starting_for_fixture"
    WAITING_READINESS = "waiting_readiness"
    SCANNING_FIXTURE = "scanning_fixture"
    VERIFYING_FIXTURE = "verifying_fixture"
    APPLYING_POST_SCAN_STATE = "applying_post_scan_state"
    VERIFYING_POST_SCAN_STATE = "verifying_post_scan_state"
    FINAL_STOPPING = "final_stopping"
    FINAL_VERIFYING = "final_verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ResetOperation:
    id: UUID
    managed_instance_id: UUID
    requested_at: datetime
    state: ResetOperationState = ResetOperationState.QUEUED
    phase: ResetOperationPhase = ResetOperationPhase.QUEUED
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False
    lease_token_digest: str | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for value in (
            self.requested_at,
            self.started_at,
            self.heartbeat_at,
            self.completed_at,
            self.lease_expires_at,
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError("reset operation timestamps must be timezone-aware")
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("reset operation failure reason must be bounded")
        if self.lease_token_digest is not None and (
            len(self.lease_token_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.lease_token_digest
            )
        ):
            raise ValueError("reset operation lease digest must be SHA-256 hex")

    @property
    def active(self) -> bool:
        return self.state in {ResetOperationState.QUEUED, ResetOperationState.RUNNING}

    def lease_is_fresh(self, now: datetime | None = None) -> bool:
        instant = datetime.now(UTC) if now is None else now
        return (
            self.state is ResetOperationState.RUNNING
            and self.lease_expires_at is not None
            and self.lease_expires_at > instant
        )

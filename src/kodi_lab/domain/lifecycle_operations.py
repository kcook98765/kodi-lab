"""Typed durable history for synchronous managed-instance lifecycle actions."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ManagedInstanceLifecycleKind(StrEnum):
    PROVISION = "provision"
    START = "start"
    STOP = "stop"
    DEPROVISION = "deprovision"


class ManagedInstanceLifecycleOperationState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ManagedInstanceSemanticState(StrEnum):
    """Bounded runtime meaning without Docker identity or raw daemon state."""

    UNPROVISIONED = "unprovisioned"
    STOPPED = "stopped"
    RUNNING_USABLE = "running_usable"
    RUNNING_NOT_READY = "running_not_ready"
    TRANSITIONAL = "transitional"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class ManagedInstanceLifecycleOperation:
    id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    instance_name: str
    kind: ManagedInstanceLifecycleKind
    requested_at: datetime
    started_at: datetime
    prior_state: ManagedInstanceSemanticState
    state: ManagedInstanceLifecycleOperationState = (
        ManagedInstanceLifecycleOperationState.RUNNING
    )
    resulting_state: ManagedInstanceSemanticState | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        if not 1 <= len(self.instance_name) <= 256:
            raise ValueError("lifecycle operation instance name must be bounded")
        for value in (self.requested_at, self.started_at, self.completed_at):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(
                    "lifecycle operation timestamps must be timezone-aware"
                )
        if self.failure_reason is not None and not 1 <= len(self.failure_reason) <= 256:
            raise ValueError("lifecycle operation reason must be bounded")
        if self.state is ManagedInstanceLifecycleOperationState.RUNNING:
            if (
                self.completed_at is not None
                or self.resulting_state is not None
                or self.failure_reason is not None
                or self.reconciliation_required
            ):
                raise ValueError("running lifecycle operation fields are inconsistent")
        elif self.completed_at is None or self.resulting_state is None:
            raise ValueError("terminal lifecycle operation fields are incomplete")
        if self.state is ManagedInstanceLifecycleOperationState.SUCCEEDED and (
            self.failure_reason is not None or self.reconciliation_required
        ):
            raise ValueError("succeeded lifecycle operation fields are inconsistent")
        if self.state is ManagedInstanceLifecycleOperationState.FAILED and (
            self.failure_reason is None or self.reconciliation_required
        ):
            raise ValueError("failed lifecycle operation fields are inconsistent")
        if (
            self.state is ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
            and (self.failure_reason is None or not self.reconciliation_required)
        ):
            raise ValueError(
                "reconciliation lifecycle operation fields are inconsistent"
            )

    @property
    def active(self) -> bool:
        return self.state is ManagedInstanceLifecycleOperationState.RUNNING

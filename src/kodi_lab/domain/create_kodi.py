"""Fixed Create intent/checkpoints; none of these records grant runtime authority."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid5

CREATE_SCOPE = "create_native_v1"


class CreateStage(StrEnum):
    INSTALLATION = "installation"
    REGISTRATION = "registration"
    PROVISIONING = "provisioning"
    STARTUP = "startup"
    READINESS = "readiness"


STAGES = tuple(CreateStage)


class CreateCheckpoint(StrEnum):
    ACCEPTED = "accepted"
    PLANNED = "planned"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    NEEDS_ATTENTION = "needs_attention"
    COMPLETE = "complete"


class CreateOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class CreateIntent:
    generation: str
    target_id: UUID
    recipe_id: str
    recipe_revision: int
    recipe_digest: str
    start_after_setup: bool = True
    preview_approved: bool = False


@dataclass(frozen=True, slots=True)
class CreateOperation:
    id: UUID
    request_id: UUID
    target_id: UUID
    generation: str
    recipe_id: str
    recipe_revision: int
    recipe_digest: str
    scope: str
    stage: CreateStage
    checkpoint: CreateCheckpoint
    version: int
    approved_at: str
    updated_at: str
    dispatched_at: str | None
    completed_at: str | None
    outcome: CreateOutcome | None
    reason: str | None
    installation_operation_id: UUID | None
    provision_operation_id: UUID | None
    start_operation_id: UUID | None
    runtime_revision_id: UUID | None
    image_id: str | None
    container_id: str | None
    runtime_state_revision: int | None
    start_after_setup: bool = True
    preview_approved: bool = False

    @property
    def child_id(self) -> UUID | None:
        """Local registration/readiness keys are correlation, not lifecycle IDs."""
        if self.checkpoint is CreateCheckpoint.ACCEPTED:
            return None
        if self.stage is CreateStage.INSTALLATION:
            return self.installation_operation_id
        if self.stage is CreateStage.PROVISIONING:
            return self.provision_operation_id
        if self.stage is CreateStage.STARTUP:
            return self.start_operation_id
        return uuid5(self.id, f"create-kodi:{self.stage}:v1")


@dataclass(frozen=True, slots=True)
class CreateAcceptance:
    operation: CreateOperation
    replayed: bool


@dataclass(frozen=True, slots=True)
class CreateDispatchClaim:
    """Fresh ledger transition result, never reconstructed by a status read.

    This is not a broker capability or a substitute for child admission.
    """

    operation: CreateOperation


@dataclass(frozen=True, slots=True)
class CreateChildResult:
    """Trusted adapter evidence. Milestone 1 exercises this with fake children."""

    stage: CreateStage
    child_id: UUID
    target_id: UUID
    recipe_digest: str
    outcome: CreateOutcome
    runtime_revision_id: UUID | None = None
    image_id: str | None = None
    container_id: str | None = None
    runtime_state_revision: int | None = None
    reason: str | None = None


class CreateConflict(ValueError):
    def __init__(self, reason: str, operation_id: UUID) -> None:
        super().__init__(reason)
        self.operation_id = operation_id


class CreateTransitionError(ValueError):
    """Stale command, invalid transition or mismatched child evidence."""

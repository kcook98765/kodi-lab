"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, TypeVar, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from kodi_lab import __version__
from kodi_lab.broker.docker_inventory import (
    MAX_MANAGED_CONTAINERS,
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import (
    HandlerExecutionError,
    OperationError,
    RequestError,
)
from kodi_lab.broker.protocol import (
    PROTOCOL_VERSION,
    Request,
    StrictModel,
    is_canonical_uuid,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_SUBNET,
)
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerBusy,
    ReviewedWorkerError,
)
from kodi_lab.development.addition_reviews import ReviewPaths
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
)
from kodi_lab.runtime.preview import PreviewAcknowledgement


class EmptyParams(StrictModel):
    """An operation that accepts no caller-controlled parameters."""


class EstablishManagedBridgeTrustParams(StrictModel):
    managed_instance_id: str
    expected_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1, strict=True)
    install_reviewed: bool = Field(default=False, strict=True)

    @field_validator("managed_instance_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed identity must be canonical")
        return value


class EstablishManagedBridgeTrustResult(StrictModel):
    managed_instance_id: str
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1, strict=True)
    bridge_id: str

    @field_validator("managed_instance_id", "bridge_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be canonical")
        return value


class ReviewedInstallationLaunchParams(StrictModel):
    generation: Literal["matrix", "nexus", "omega", "piers"]
    operation_id: str
    preview_acknowledgement: PreviewAcknowledgement | None = None

    @model_validator(mode="after")
    def _preview_scope(self) -> ReviewedInstallationLaunchParams:
        if (self.generation == "piers") != (self.preview_acknowledgement is not None):
            raise ValueError("exact Preview acknowledgement/provider mismatch")
        return self

    @field_validator("operation_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("operation identity must be canonical")
        return value


class ReviewedInstallationIdParams(StrictModel):
    operation_id: str

    @field_validator("operation_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("operation identity must be canonical")
        return value


class ReviewedInstallationLaunchResult(StrictModel):
    operation_id: str
    generation: Literal["matrix", "nexus", "omega", "piers"]
    newly_accepted: bool


class ReviewedInstallationActiveResult(StrictModel):
    operation_id: str | None
    generation: Literal["matrix", "nexus", "omega", "piers"] | None
    lock_held: bool


class ReviewedInstallationWorkerResult(StrictModel):
    operation_id: str
    state: Literal["running", "exited", "missing", "mismatch"]
    lock_held: bool


class ReviewedInstallationImageResult(StrictModel):
    operation_id: str
    present: bool


class PromotionKodiLogParams(StrictModel):
    verification_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("verification_id", "managed_instance_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CapturePromotionKodiLogCursorResult(StrictModel):
    verification_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    cursor: str = Field(min_length=1, max_length=4096)


class ReadPromotionKodiLogWindowParams(PromotionKodiLogParams):
    cursor: str = Field(min_length=1, max_length=4096)


class ReadPromotionKodiLogWindowResult(StrictModel):
    verification_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    complete: bool
    classification: Literal[
        "clean", "target_errors", "unrelated_warnings", "incomplete"
    ]
    target_error_count: int = Field(ge=0, le=1_000_000)
    unrelated_warning_count: int = Field(ge=0, le=1_000_000)
    snippets: list[str] = Field(max_length=20)
    scanned_bytes: int = Field(ge=0, le=1024 * 1024)


class SmokeLogParams(StrictModel):
    execution_id: str = Field(min_length=36, max_length=36)

    @field_validator("execution_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("execution identity must be canonical")
        return value


class CaptureSmokeLogResult(StrictModel):
    execution_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: str = Field(min_length=1, max_length=4096)


class ReadSmokeLogParams(SmokeLogParams):
    cursor: str = Field(min_length=1, max_length=4096)


class ReadSmokeLogResult(StrictModel):
    execution_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic: AddonExceptionDiagnostic


class PingResult(StrictModel):
    """Safe broker liveness result."""

    status: Literal["healthy"]
    protocol_version: Literal[1]


class BrokerInfoResult(StrictModel):
    """Non-sensitive package/protocol identity."""

    package_version: str = Field(min_length=1, max_length=64)
    protocol_version: Literal[1]


class CapabilitiesResult(StrictModel):
    """The exact registered operation set."""

    operations: list[str] = Field(max_length=41)
    protocol_version: Literal[1]


class ManagedContainerInventoryEntry(StrictModel):
    """Minimal Docker-independent inventory data exposed to clients."""

    managed_instance_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    runtime_revision_id: str | None = None
    runtime_state: ContainerRuntimeState
    health_state: ContainerHealthState

    @field_validator("runtime_revision_id")
    @classmethod
    def _canonical_runtime_revision_id(cls, value: str | None) -> str | None:
        if value is not None and not is_canonical_uuid(value):
            raise ValueError("runtime revision identity must be canonical")
        return value

    @field_validator("runtime_state", mode="before")
    @classmethod
    def _runtime_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value

    @field_validator("health_state", mode="before")
    @classmethod
    def _health_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerHealthState(value)
        return value


class ManagedContainerInventoryResult(StrictModel):
    """Bounded deterministic Kodi Lab-owned container inventory."""

    containers: list[ManagedContainerInventoryEntry] = Field(
        max_length=MAX_MANAGED_CONTAINERS
    )


class ManagedContainerInventoryParams(StrictModel):
    """Optional exact managed identity for a scoped inventory read."""

    managed_instance_id: str | None = None

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_instance_id(cls, value: str | None) -> str | None:
        if value is not None and not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value


class ObserveManagedKodiRuntimeParams(StrictModel):
    """Identity-only preconditions for fixed managed Kodi observation."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value


class ObserveManagedKodiRuntimeResult(StrictModel):
    """Minimal fixed-network Kodi process and endpoint observation."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    runtime_state: ContainerRuntimeState
    process_state: KodiProcessState
    ipv4_address: str = Field(min_length=7, max_length=15)
    jsonrpc_port: int

    @field_validator("runtime_state", mode="before")
    @classmethod
    def _runtime_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value

    @field_validator("process_state", mode="before")
    @classmethod
    def _process_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return KodiProcessState(value)
        return value

    @field_validator("managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value

    @field_validator("ipv4_address")
    @classmethod
    def _managed_ipv4(cls, value: str) -> str:
        try:
            address = IPv4Address(value)
        except ValueError as error:
            raise ValueError("ipv4_address must be canonical IPv4") from error
        network = IPv4Network(MANAGED_NETWORK_SUBNET)
        if str(address) != value or address not in network:
            raise ValueError("ipv4_address must be on the managed network")
        return value

    @model_validator(mode="after")
    def _fixed_runtime_endpoint(self) -> ObserveManagedKodiRuntimeResult:
        if self.runtime_state is not ContainerRuntimeState.RUNNING:
            raise ValueError("managed Kodi observation requires running state")
        if self.jsonrpc_port != 8080:
            raise ValueError("managed Kodi JSON-RPC port must be 8080")
        return self


class CaptureManagedKodiScreenshotParams(StrictModel):
    """Only server-bound identities select the one fixed screenshot operation."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    expected_runtime_revision_id: str = Field(min_length=36, max_length=36)
    capture_id: str = Field(min_length=36, max_length=36)

    @field_validator(
        "managed_instance_id", "expected_runtime_revision_id", "capture_id"
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CaptureManagedKodiScreenshotResult(StrictModel):
    """Path-free bounded proof of a private raw screenshot artifact."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    capture_id: str = Field(min_length=36, max_length=36)
    backend: Literal["x11_get_image"]
    raw_byte_size: int = Field(ge=1, le=8 * 1024 * 1024)

    @field_validator("managed_instance_id", "runtime_revision_id", "capture_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ProvisionManagedContainerParams(StrictModel):
    """The sole public input to trusted provisioning resolution."""

    managed_instance_id: str = Field(min_length=36, max_length=36)

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_managed_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value


class ProvisionManagedContainerResult(StrictModel):
    """Minimal verified creation/idempotency result."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    runtime_state: ContainerRuntimeState
    created: bool

    @field_validator("managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value

    @field_validator("runtime_state", mode="before")
    @classmethod
    def _runtime_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value

    @model_validator(mode="after")
    def _require_provisioned_state(self) -> ProvisionManagedContainerResult:
        if self.runtime_state not in {
            ContainerRuntimeState.RUNNING,
            ContainerRuntimeState.STOPPED,
        }:
            raise ValueError("provisioned state must be running or stopped")
        if self.created and self.runtime_state is not ContainerRuntimeState.STOPPED:
            raise ValueError("newly created container must be stopped")
        return self


class StartManagedContainerParams(StrictModel):
    """Optimistic identity/state preconditions for one semantic start."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    expected_runtime_state: ContainerRuntimeState
    ordinary_start_proof: SecretStr | None = Field(
        default=None, min_length=43, max_length=43
    )

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_managed_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value

    @field_validator("expected_runtime_state", mode="before")
    @classmethod
    def _runtime_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value


class StopManagedContainerParams(StrictModel):
    """Optimistic identity/state preconditions for one semantic stop."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    expected_runtime_state: ContainerRuntimeState

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_managed_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value

    @field_validator("expected_runtime_state", mode="before")
    @classmethod
    def _runtime_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value


class StopManagedContainerResult(StrictModel):
    """Verified stopped state without raw Docker response data."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    runtime_state: ContainerRuntimeState
    stop_performed: bool

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_managed_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value

    @field_validator("runtime_state", mode="before")
    @classmethod
    def _stopped_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value

    @model_validator(mode="after")
    def _require_stopped_state(self) -> StopManagedContainerResult:
        if self.runtime_state is not ContainerRuntimeState.STOPPED:
            raise ValueError("stop result must report stopped state")
        return self


class StartManagedContainerResult(StrictModel):
    """Verified running state without raw Docker response data."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    runtime_state: ContainerRuntimeState
    start_performed: bool

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_managed_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value

    @field_validator("runtime_state", mode="before")
    @classmethod
    def _running_state_from_wire(cls, value: object) -> object:
        if isinstance(value, str):
            return ContainerRuntimeState(value)
        return value

    @model_validator(mode="after")
    def _require_running_state(self) -> StartManagedContainerResult:
        if self.runtime_state is not ContainerRuntimeState.RUNNING:
            raise ValueError("start result must report running state")
        return self


class DeprovisionManagedContainerParams(StrictModel):
    """Strict destructive identity, revision, and stopped-state preconditions."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    expected_runtime_revision_id: str = Field(min_length=36, max_length=36)
    expected_runtime_state: Literal[ContainerRuntimeState.STOPPED]

    @field_validator("managed_instance_id", "expected_runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class DeprovisionManagedContainerResult(StrictModel):
    """Minimal result after verified destructive absence."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    removed_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    removed: Literal[True]

    @field_validator("managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ReconstructionDestinationParams(StrictModel):
    managed_instance_id: str

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be canonical")
        return value


class ReconstructionDestinationResult(StrictModel):
    managed_instance_id: str
    desired_runtime_revision_id: str
    desired_image_id: str


class RemoveMismatchedManagedContainerParams(StrictModel):
    managed_instance_id: str
    operation_id: str
    lease_token_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    owned_runtime_state_revision: int = Field(ge=0)
    old_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    old_runtime_revision_id: str
    desired_runtime_revision_id: str
    expected_runtime_state: Literal[ContainerRuntimeState.STOPPED]

    @field_validator(
        "managed_instance_id",
        "operation_id",
        "old_runtime_revision_id",
        "desired_runtime_revision_id",
    )
    @classmethod
    def _canonical(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be canonical")
        return value


class RemoveMismatchedManagedContainerResult(StrictModel):
    managed_instance_id: str
    removed_container_id: str
    old_runtime_revision_id: str
    removed: Literal[True]


class ApplyAssignedFixtureParams(StrictModel):
    """Only identity preconditions for the one trusted fixture operation."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id")
    @classmethod
    def _canonical_instance_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("managed_instance_id must be a canonical UUID string")
        return value


class ApplyAssignedFixtureResult(StrictModel):
    """Bounded semantic result with no path, archive, or Docker configuration."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    fixture_slug: Literal[
        "small-mixed-v1",
        "small-mixed-v2",
        "small-mixed-v3",
        "small-mixed-v4",
        "small-mixed-v5",
        "small-mixed-v6",
        "small-movies-v1",
        "small-movies-v2",
        "small-movies-v3",
        "small-movies-v4",
        "small-movies-v5",
        "small-tv-v1",
        "small-tv-v2",
        "small-tv-v3",
        "medium-mixed-v1",
        "cross-movies-matrix-v1",
        "cross-movies-nexus-v1",
        "cross-movies-omega-v1",
    ]
    fixture_version: Literal[1, 2, 3, 4, 5, 6]
    content_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    runtime_state: Literal[ContainerRuntimeState.STOPPED]

    @field_validator("managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value

    @model_validator(mode="after")
    def _matching_fixture_identity(self) -> ApplyAssignedFixtureResult:
        expected_version = {
            "small-mixed-v1": 1,
            "small-mixed-v2": 2,
            "small-mixed-v3": 3,
            "small-mixed-v4": 4,
            "small-mixed-v5": 5,
            "small-mixed-v6": 6,
            "small-movies-v1": 1,
            "small-movies-v2": 2,
            "small-movies-v3": 3,
            "small-movies-v4": 4,
            "small-movies-v5": 5,
            "small-tv-v1": 1,
            "small-tv-v2": 2,
            "small-tv-v3": 3,
            "medium-mixed-v1": 1,
            "cross-movies-matrix-v1": 1,
            "cross-movies-nexus-v1": 1,
            "cross-movies-omega-v1": 1,
        }[self.fixture_slug]
        if self.fixture_version != expected_version:
            raise ValueError("fixture slug and version do not match")
        return self


class CaptureManagedInstanceSnapshotParams(StrictModel):
    """Only the snapshot, source, and observed immutable container identities."""

    snapshot_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("snapshot_id", "managed_instance_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CaptureManagedInstanceSnapshotResult(StrictModel):
    """Bounded artifact identity without paths or member inventory."""

    snapshot_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    archive_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    manifest_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    archive_size: int = Field(gt=0, le=16 * 1024**3)
    member_count: int = Field(gt=0, le=250_000)

    @field_validator("snapshot_id", "managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ApplyManagedInstanceSnapshotParams(StrictModel):
    """Only snapshot, source-instance, and fresh-container identities."""

    snapshot_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("snapshot_id", "managed_instance_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ApplyManagedInstanceSnapshotResult(StrictModel):
    """Bounded artifact and readback proof without file or Docker authority."""

    snapshot_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    archive_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    manifest_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    archive_size: int = Field(gt=0, le=16 * 1024**3)
    member_count: int = Field(gt=0, le=250_000)
    recognized_databases_checked: int = Field(ge=0, le=250_000)

    @field_validator("snapshot_id", "managed_instance_id", "runtime_revision_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class DeleteManagedSnapshotArtifactParams(StrictModel):
    """The only deletion authority is one canonical Snapshot UUID."""

    snapshot_id: str = Field(min_length=36, max_length=36)

    @field_validator("snapshot_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("snapshot_id must be a canonical UUID string")
        return value


class DeleteManagedSnapshotArtifactResult(StrictModel):
    """Bounded proof that the exact UUID artifact directory is absent."""

    snapshot_id: str = Field(min_length=36, max_length=36)
    artifact_absent: Literal[True]
    artifact_was_present: bool

    @field_validator("snapshot_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("snapshot_id must be a canonical UUID string")
        return value


class CopySyncAttachedDevelopmentSourceParams(StrictModel):
    """Only persisted identities and immutable container precondition."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    development_source_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    deployment_operation_id: str = Field(min_length=36, max_length=36)

    @field_validator(
        "managed_instance_id", "development_source_id", "deployment_operation_id"
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CopySyncPromotedDevelopmentSourceParams(StrictModel):
    verification_operation_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("verification_operation_id", "managed_instance_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CopySyncMcpWorkspaceParams(StrictModel):
    """Only semantic identities for one exact-generation workspace sync."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    workspace_id: str = Field(min_length=36, max_length=36)
    expected_generation: int = Field(ge=0)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    deployment_operation_id: str = Field(min_length=36, max_length=36)

    @field_validator("managed_instance_id", "workspace_id", "deployment_operation_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class CopySyncAttachedDevelopmentSourceResult(StrictModel):
    """Aggregate copy/sync proof with no path, member, or ownership fields."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    development_source_id: str = Field(min_length=36, max_length=36)
    deployment_operation_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    result_classification: Literal["initial_deployment", "synchronized", "unchanged"]
    source_manifest_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    verified_destination_digest: str = Field(
        min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    observed_source_name: str = Field(min_length=1, max_length=256)
    observed_source_version: str = Field(min_length=1, max_length=128)
    new_count: int = Field(ge=0, le=50_000)
    changed_count: int = Field(ge=0, le=25_000)
    unchanged_count: int = Field(ge=0, le=50_000)
    destination_only_count: int = Field(ge=0, le=50_000)
    transferred_bytes: int = Field(ge=0, le=1024**3)

    @field_validator(
        "managed_instance_id",
        "development_source_id",
        "deployment_operation_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAttachedDevelopmentSourceDestinationParams(StrictModel):
    """Only semantic identities for one fixed destination readback."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    development_source_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id", "development_source_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ReviewSourceAdditionsParams(VerifyAttachedDevelopmentSourceDestinationParams):
    paths: ReviewPaths

    @field_validator("paths", mode="before")
    @classmethod
    def _json_paths(cls, value: object) -> object:
        # JSON carries tuples as arrays; retain strict element/count validation.
        return tuple(value) if isinstance(value, list) else value


class VerifyMcpWorkspaceDestinationParams(StrictModel):
    deployment_operation_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("deployment_operation_id", "managed_instance_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAttachedDevelopmentSourceDestinationResult(StrictModel):
    """Safe aggregate current-byte verification result."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    development_source_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    addon_id: str = Field(min_length=1, max_length=128)
    addon_version: str = Field(min_length=1, max_length=128)
    matched: Literal[True]
    video_plugin_descriptor_eligible: bool = False
    python_service_descriptor_eligible: bool = False
    addon_xml_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    readback_bytes: int | None = Field(default=None, ge=0)

    @field_validator(
        "managed_instance_id", "development_source_id", "runtime_revision_id"
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class InspectMcpWorkspaceDeploymentResult(StrictModel):
    deployment_operation_id: str = Field(min_length=36, max_length=36)
    managed_instance_id: str = Field(min_length=36, max_length=36)
    development_source_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_destination_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    installed_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    matches_requested: bool
    matches_prior: bool


class NativeZipObservationParams(StrictModel):
    operation_id: UUID

    @field_validator("operation_id", mode="before")
    @classmethod
    def _canonical_uuid(cls, value: object) -> UUID:
        # JSON carries UUIDs as strings; keep strict validation for every
        # other type and require the same canonical spelling as other operations.
        if isinstance(value, UUID):
            return value
        if isinstance(value, str) and is_canonical_uuid(value):
            return UUID(value)
        raise ValueError("operation_id must be a canonical UUID string")


class AddonZipInstallationParams(StrictModel):
    """Only installation, artifact, operation, and immutable container identity."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    installation_operation_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id", "artifact_id", "installation_operation_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class AddonZipDestinationResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    installation_operation_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    destination_state: Literal[
        "absent", "exact_payload_present", "partial_or_unexpected"
    ]
    matched: bool

    @field_validator(
        "managed_instance_id",
        "artifact_id",
        "installation_operation_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAddonZipEnablementDestinationParams(StrictModel):
    """Only ZIP enablement semantic identity and exact-container precondition."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    enablement_operation_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id", "artifact_id", "enablement_operation_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAddonZipEnablementDestinationResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    enablement_operation_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    addon_id: str = Field(min_length=1, max_length=128)
    addon_version: str = Field(min_length=1, max_length=128)
    destination_state: Literal[
        "absent", "exact_payload_present", "partial_or_unexpected"
    ]
    matched: bool

    @field_validator(
        "managed_instance_id",
        "artifact_id",
        "enablement_operation_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAddonZipSmokeDestinationParams(StrictModel):
    """Only ZIP smoke semantic identity and exact-container precondition."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    smoke_operation_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id", "artifact_id", "smoke_operation_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class VerifyAddonZipSmokeDestinationResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    smoke_operation_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    addon_id: str = Field(min_length=1, max_length=128)
    addon_version: str = Field(min_length=1, max_length=128)
    destination_state: Literal[
        "absent", "exact_payload_present", "partial_or_unexpected"
    ]
    matched: bool

    @field_validator(
        "managed_instance_id",
        "artifact_id",
        "smoke_operation_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ApplyAddonZipInstallationResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    installation_operation_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    matched: Literal[True]

    @field_validator(
        "managed_instance_id",
        "artifact_id",
        "installation_operation_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class InspectRepositoryReplacementParams(StrictModel):
    """Only instance, exact approval, and immutable container precondition."""

    managed_instance_id: str = Field(min_length=36, max_length=36)
    approval_id: str = Field(min_length=36, max_length=36)
    expected_container_id: str = Field(
        min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("managed_instance_id", "approval_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class InspectRepositoryReplacementResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    approval_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    topology: Literal[
        "absent_eligible",
        "overwrite_add_eligible",
        "exact_payload_noop",
        "deletion_required",
        "type_transition",
        "incumbent_drift",
    ]

    @field_validator(
        "managed_instance_id", "approval_id", "artifact_id", "runtime_revision_id"
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ApplyRepositoryReplacementParams(InspectRepositoryReplacementParams):
    """Add exactly one replacement-operation UUID; no filesystem controls."""

    replacement_operation_id: str = Field(min_length=36, max_length=36)

    @field_validator("replacement_operation_id")
    @classmethod
    def _canonical_operation_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


class ApplyRepositoryReplacementResult(StrictModel):
    managed_instance_id: str = Field(min_length=36, max_length=36)
    approval_id: str = Field(min_length=36, max_length=36)
    replacement_operation_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    runtime_revision_id: str = Field(min_length=36, max_length=36)
    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    matched: Literal[True]

    @field_validator(
        "managed_instance_id",
        "approval_id",
        "replacement_operation_id",
        "artifact_id",
        "runtime_revision_id",
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


type OperationHandler = Callable[[BaseModel], BaseModel]
type OperationAuditBuilder = Callable[[BaseModel], dict[str, object]]


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    """One explicit operation and both sides of its schema."""

    name: str
    params_model: type[BaseModel]
    result_model: type[BaseModel]
    handler: OperationHandler
    requires_authorized_peer: bool = True
    audit_builder: OperationAuditBuilder | None = None


ParamsT = TypeVar("ParamsT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


class OperationRegistry:
    """Closed mapping from protocol names to typed handlers."""

    def __init__(self) -> None:
        self._operations: dict[str, OperationDefinition] = {}

    def register(
        self,
        *,
        name: str,
        params_model: type[ParamsT],
        result_model: type[ResultT],
        handler: Callable[[ParamsT], ResultT],
        requires_authorized_peer: bool = True,
        audit_builder: Callable[[ResultT], dict[str, object]] | None = None,
    ) -> None:
        """Register one fixed operation; duplicates are programming errors."""

        if name in self._operations:
            raise ValueError(f"operation {name!r} is already registered")
        self._operations[name] = OperationDefinition(
            name=name,
            params_model=params_model,
            result_model=result_model,
            handler=cast(OperationHandler, handler),
            requires_authorized_peer=requires_authorized_peer,
            audit_builder=cast(OperationAuditBuilder | None, audit_builder),
        )

    @property
    def names(self) -> tuple[str, ...]:
        """Return deterministic capability names."""

        return tuple(sorted(self._operations))

    def definition(self, name: str) -> OperationDefinition | None:
        """Return only an explicitly registered definition."""

        return self._operations.get(name)

    def audit_fields_for(self, name: str, result: BaseModel) -> dict[str, object]:
        """Return only trusted operation-defined audit metadata."""

        definition = self.definition(name)
        if definition is None or definition.audit_builder is None:
            return {}
        return definition.audit_builder(result)

    def dispatch(self, request: Request) -> BaseModel:
        """Validate operation parameters, invoke, and revalidate the result."""

        definition = self.definition(request.operation)
        if definition is None:
            raise RequestError(
                "unknown_operation",
                "The requested operation is not available.",
                request_id=request.request_id,
                operation=request.operation,
                protocol_version=request.version,
            )
        try:
            params = definition.params_model.model_validate(request.params)
        except ValidationError as error:
            raise RequestError(
                "invalid_params",
                "Operation parameters do not match the required schema.",
                request_id=request.request_id,
                operation=request.operation,
                protocol_version=request.version,
            ) from error

        try:
            result = definition.handler(params)
            return definition.result_model.model_validate(
                result.model_dump(mode="python")
            )
        except OperationError as error:
            raise RequestError(
                error.code,
                error.safe_message,
                request_id=request.request_id,
                operation=request.operation,
                protocol_version=request.version,
                audit_fields=error.audit_fields,
            ) from error
        except Exception as error:
            raise HandlerExecutionError(
                request_id=request.request_id,
                operation=request.operation,
                protocol_version=request.version,
            ) from error


def _ping(_params: EmptyParams) -> PingResult:
    return PingResult(status="healthy", protocol_version=PROTOCOL_VERSION)


def _broker_info(_params: EmptyParams) -> BrokerInfoResult:
    return BrokerInfoResult(
        package_version=__version__, protocol_version=PROTOCOL_VERSION
    )


def register_reviewed_runtime_operations(
    registry: OperationRegistry, reviewed_worker: DockerReviewedInstallationWorker
) -> None:
    """The installed broker keeps acquisition separate from working profiles."""
    registry.register(
        name="ping",
        params_model=EmptyParams,
        result_model=PingResult,
        handler=_ping,
    )
    registry.register(
        name="broker_info",
        params_model=EmptyParams,
        result_model=BrokerInfoResult,
        handler=_broker_info,
    )

    def launch_reviewed_installation(
        params: ReviewedInstallationLaunchParams,
    ) -> ReviewedInstallationLaunchResult:
        try:
            accepted = reviewed_worker.launch(
                params.generation,
                UUID(params.operation_id),
                **(
                    {"preview_acknowledgement": params.preview_acknowledgement}
                    if params.generation == "piers"
                    else {}
                ),
            )
        except ReviewedWorkerBusy as error:
            raise OperationError(
                "runtime_installation_busy", "Another runtime installation is active."
            ) from error
        except ReviewedWorkerError as error:
            raise OperationError(
                "runtime_installation_unavailable",
                "Reviewed runtime installation is unavailable.",
            ) from error
        return ReviewedInstallationLaunchResult(
            operation_id=params.operation_id,
            generation=params.generation,
            newly_accepted=accepted,
        )

    registry.register(
        name="launch_reviewed_runtime_installation",
        params_model=ReviewedInstallationLaunchParams,
        result_model=ReviewedInstallationLaunchResult,
        handler=launch_reviewed_installation,
    )

    def active_reviewed_installation(
        _params: EmptyParams,
    ) -> ReviewedInstallationActiveResult:
        try:
            active = reviewed_worker.active()
            lock_held = reviewed_worker.lock_held()
        except ReviewedWorkerError as error:
            raise OperationError(
                "runtime_installation_unavailable",
                "Reviewed runtime installation is unavailable.",
            ) from error
        return ReviewedInstallationActiveResult(
            operation_id=str(active[0]) if active else None,
            generation=cast(
                Literal["matrix", "nexus", "omega", "piers"] | None,
                active[1] if active else None,
            ),
            lock_held=lock_held,
        )

    registry.register(
        name="active_reviewed_runtime_installation",
        params_model=EmptyParams,
        result_model=ReviewedInstallationActiveResult,
        handler=active_reviewed_installation,
    )

    def observe_reviewed_installation(
        params: ReviewedInstallationIdParams,
    ) -> ReviewedInstallationWorkerResult:
        try:
            observed = reviewed_worker.observe(UUID(params.operation_id))
        except ReviewedWorkerError as error:
            raise OperationError(
                "runtime_installation_unavailable",
                "Reviewed runtime installation is unavailable.",
            ) from error
        return ReviewedInstallationWorkerResult(
            operation_id=params.operation_id,
            state=observed.state,
            lock_held=observed.lock_held,
        )

    registry.register(
        name="observe_reviewed_runtime_installation",
        params_model=ReviewedInstallationIdParams,
        result_model=ReviewedInstallationWorkerResult,
        handler=observe_reviewed_installation,
    )

    def reviewed_image_present(
        params: ReviewedInstallationIdParams,
    ) -> ReviewedInstallationImageResult:
        try:
            present = reviewed_worker.image_present(UUID(params.operation_id))
        except ReviewedWorkerError as error:
            raise OperationError(
                "runtime_installation_unavailable",
                "Reviewed runtime installation is unavailable.",
            ) from error
        return ReviewedInstallationImageResult(
            operation_id=params.operation_id, present=present
        )

    registry.register(
        name="reviewed_runtime_image_present",
        params_model=ReviewedInstallationIdParams,
        result_model=ReviewedInstallationImageResult,
        handler=reviewed_image_present,
    )

    def capabilities(_params: EmptyParams) -> CapabilitiesResult:
        return CapabilitiesResult(
            operations=list(registry.names), protocol_version=PROTOCOL_VERSION
        )

    registry.register(
        name="capabilities",
        params_model=EmptyParams,
        result_model=CapabilitiesResult,
        handler=capabilities,
    )


def build_reviewed_runtime_registry() -> OperationRegistry:
    registry = OperationRegistry()
    register_reviewed_runtime_operations(registry, DockerReviewedInstallationWorker())
    return registry

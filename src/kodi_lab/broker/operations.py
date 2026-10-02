"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from kodi_lab import __version__
from kodi_lab.broker.addon_zips import (
    AddonZipInstallationStateError,
    SqliteAddonZipInstallationResolver,
)
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentResolver,
    DevelopmentSourceDeploymentStateError,
    PromotionDevelopmentSourceDeploymentResolver,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import (
    MAX_MANAGED_CONTAINERS,
    AddonZipInstallationProvider,
    AppliedAssignedFixtureRecord,
    AssignedFixtureApplicationProvider,
    ContainerHealthState,
    ContainerRuntimeState,
    DeprovisionedManagedContainerRecord,
    DevelopmentSourceDeploymentProvider,
    DockerManagedContainerAdapter,
    KodiProcessState,
    ManagedContainerDeprovisioningProvider,
    ManagedContainerOperationsProvider,
    ManagedContainerProvisioningProvider,
    ManagedInstanceSnapshotApplyProvider,
    ManagedInstanceSnapshotCaptureProvider,
    ManagedKodiRuntimeObservationProvider,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    AssignedFixtureIncompatibleError,
    AssignedFixtureMissingError,
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedError,
    ContainerStateNotReconcilableError,
    ContainerStateNotStoppableError,
    DockerAddonZipInstallationError,
    DockerCreateError,
    DockerDaemonUnavailableError,
    DockerDevelopmentSourceDeploymentError,
    DockerFixtureArchiveError,
    DockerInventoryReadError,
    DockerRemoveError,
    DockerSnapshotCaptureError,
    DockerSnapshotRestoreError,
    DockerStartError,
    DockerStopError,
    FixtureBaselineConflictError,
    HandlerExecutionError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
    ManagedContainerNotFoundError,
    ManagedNetworkConflictError,
    ManagedNetworkUnavailableError,
    ManagedRuntimeEndpointError,
    ManagedRuntimeNotRunningError,
    OperationError,
    PackagedFixtureUnavailableError,
    PartialProvisioningError,
    PersistentProvisioningUnsupportedError,
    PersistentSnapshotUnsupportedError,
    PostCreateVerificationError,
    PostDeprovisionVerificationError,
    PostStartVerificationError,
    PostStopVerificationError,
    ProvisioningConflictError,
    ProvisioningStateError,
    RequestError,
    RuntimeContainerArtifactUnavailableError,
    RuntimeExecutionProfileUnavailableError,
    RuntimeImageUnavailableError,
    RuntimeRevisionPreconditionError,
    RuntimeStatePreconditionError,
    SnapshotStateError,
)
from kodi_lab.broker.fixtures import (
    AssignedFixtureResolver,
    SqliteAssignedFixtureResolver,
)
from kodi_lab.broker.kodi_logs import (
    KodiLogAuthorityResolver,
    KodiLogWindowError,
    KodiLogWindowProvider,
    SqliteKodiLogAuthorityResolver,
)
from kodi_lab.broker.protocol import (
    PROTOCOL_VERSION,
    Request,
    StrictModel,
    is_canonical_uuid,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_SUBNET,
    ManagedInstanceRevisionResolver,
    ProvisioningResolver,
    RuntimeObservationResolver,
    SqliteManagedInstanceRevisionResolver,
    SqliteProvisioningResolver,
    SqliteRuntimeObservationResolver,
)
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerBusy,
    ReviewedWorkerError,
)
from kodi_lab.broker.screenshots import (
    DockerManagedScreenshotCaptureProvider,
    ManagedScreenshotCaptureProvider,
    ScreenshotCaptureError,
    ScreenshotCaptureIdentityError,
    ScreenshotCaptureResolver,
    ScreenshotCaptureStateError,
    ScreenshotCaptureUnsupportedError,
    SqliteScreenshotCaptureResolver,
)
from kodi_lab.broker.snapshots import (
    LocalSnapshotArtifactDeletionProvider,
    SnapshotArtifactDeletionProvider,
    SnapshotCaptureResolver,
    SnapshotDeleteResolver,
    SnapshotRestoreResolver,
    SqliteSnapshotCaptureResolver,
    SqliteSnapshotDeleteResolver,
    SqliteSnapshotRestoreResolver,
)
from kodi_lab.snapshot.storage import SnapshotArtifactDeletionError


class EmptyParams(StrictModel):
    """An operation that accepts no caller-controlled parameters."""


class ReviewedInstallationLaunchParams(StrictModel):
    generation: Literal["matrix", "nexus", "omega"]
    operation_id: str

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
    generation: Literal["matrix", "nexus", "omega"]
    newly_accepted: bool


class ReviewedInstallationActiveResult(StrictModel):
    operation_id: str | None
    generation: Literal["matrix", "nexus", "omega"] | None
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

    operations: list[str] = Field(max_length=32)
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
    runtime_state: ContainerRuntimeState
    health_state: ContainerHealthState

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

    @field_validator(
        "managed_instance_id", "development_source_id", "runtime_revision_id"
    )
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("identity must be a canonical UUID string")
        return value


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


def build_foundation_registry(
    container_provider: ManagedContainerOperationsProvider | None = None,
    *,
    provisioning_resolver: ProvisioningResolver | None = None,
    provisioning_provider: ManagedContainerProvisioningProvider | None = None,
    deprovisioning_provider: ManagedContainerDeprovisioningProvider | None = None,
    revision_resolver: ManagedInstanceRevisionResolver | None = None,
    runtime_observation_resolver: RuntimeObservationResolver | None = None,
    runtime_observation_provider: ManagedKodiRuntimeObservationProvider | None = None,
    screenshot_capture_resolver: ScreenshotCaptureResolver | None = None,
    screenshot_capture_provider: ManagedScreenshotCaptureProvider | None = None,
    assigned_fixture_resolver: AssignedFixtureResolver | None = None,
    assigned_fixture_provider: AssignedFixtureApplicationProvider | None = None,
    snapshot_capture_resolver: SnapshotCaptureResolver | None = None,
    snapshot_capture_provider: ManagedInstanceSnapshotCaptureProvider | None = None,
    snapshot_restore_resolver: SnapshotRestoreResolver | None = None,
    snapshot_restore_provider: ManagedInstanceSnapshotApplyProvider | None = None,
    snapshot_delete_resolver: SnapshotDeleteResolver | None = None,
    snapshot_delete_provider: SnapshotArtifactDeletionProvider | None = None,
    development_source_deployment_resolver: (
        DevelopmentSourceDeploymentResolver | None
    ) = None,
    development_source_deployment_provider: (
        DevelopmentSourceDeploymentProvider | None
    ) = None,
    addon_zip_installation_resolver: SqliteAddonZipInstallationResolver | None = None,
    addon_zip_installation_provider: AddonZipInstallationProvider | None = None,
    kodi_log_resolver: KodiLogAuthorityResolver | None = None,
    kodi_log_provider: KodiLogWindowProvider | None = None,
    reviewed_installation_worker: DockerReviewedInstallationWorker | None = None,
) -> OperationRegistry:
    """Build the exact Phase 2 operation registry."""

    registry = OperationRegistry()
    reviewed_worker = reviewed_installation_worker or DockerReviewedInstallationWorker()
    provider = container_provider or DockerManagedContainerAdapter()
    resolver = provisioning_resolver or SqliteProvisioningResolver.production()
    provisioner = provisioning_provider or cast(
        ManagedContainerProvisioningProvider, provider
    )
    deprovisioner = deprovisioning_provider or cast(
        ManagedContainerDeprovisioningProvider, provider
    )
    pin_resolver = (
        revision_resolver or SqliteManagedInstanceRevisionResolver.production()
    )
    observation_resolver = (
        runtime_observation_resolver or SqliteRuntimeObservationResolver.production()
    )
    observation_provider = runtime_observation_provider or cast(
        ManagedKodiRuntimeObservationProvider, provider
    )
    screenshot_resolver = (
        screenshot_capture_resolver or SqliteScreenshotCaptureResolver.production()
    )
    screenshot_provider = (
        screenshot_capture_provider or DockerManagedScreenshotCaptureProvider()
    )
    fixture_resolver = (
        assigned_fixture_resolver or SqliteAssignedFixtureResolver.production()
    )
    fixture_provider = assigned_fixture_provider or cast(
        AssignedFixtureApplicationProvider, provider
    )
    snapshot_resolver = (
        snapshot_capture_resolver or SqliteSnapshotCaptureResolver.production()
    )
    snapshot_provider = snapshot_capture_provider or cast(
        ManagedInstanceSnapshotCaptureProvider, provider
    )
    restore_resolver = (
        snapshot_restore_resolver or SqliteSnapshotRestoreResolver.production()
    )
    restore_provider = snapshot_restore_provider or cast(
        ManagedInstanceSnapshotApplyProvider, provider
    )
    delete_resolver = (
        snapshot_delete_resolver or SqliteSnapshotDeleteResolver.production()
    )
    delete_provider = (
        snapshot_delete_provider or LocalSnapshotArtifactDeletionProvider()
    )
    deployment_resolver = (
        development_source_deployment_resolver
        or SqliteDevelopmentSourceDeploymentResolver.production()
    )
    deployment_provider = development_source_deployment_provider or cast(
        DevelopmentSourceDeploymentProvider, provider
    )
    zip_resolver = (
        addon_zip_installation_resolver
        or SqliteAddonZipInstallationResolver.production()
    )
    zip_provider = addon_zip_installation_provider or cast(
        AddonZipInstallationProvider, provider
    )
    log_resolver = kodi_log_resolver or SqliteKodiLogAuthorityResolver.production()
    log_provider = kodi_log_provider or KodiLogWindowProvider()
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
                params.generation, UUID(params.operation_id)
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
                Literal["matrix", "nexus", "omega"] | None,
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

    def capture_promotion_kodi_log_cursor(
        params: PromotionKodiLogParams,
    ) -> CapturePromotionKodiLogCursorResult:
        try:
            authority = log_resolver.resolve(
                params.verification_id,
                params.managed_instance_id,
                params.expected_container_id,
            )
            cursor = log_provider.capture(authority)
        except KodiLogWindowError as error:
            raise OperationError(
                error.code,
                error.safe_message,
                audit_fields={
                    "broker_verification_id": params.verification_id,
                    "broker_managed_instance_id": params.managed_instance_id,
                },
            ) from error
        return CapturePromotionKodiLogCursorResult(
            verification_id=params.verification_id,
            managed_instance_id=params.managed_instance_id,
            cursor=cursor,
        )

    registry.register(
        name="capture_promotion_kodi_log_cursor",
        params_model=PromotionKodiLogParams,
        result_model=CapturePromotionKodiLogCursorResult,
        handler=capture_promotion_kodi_log_cursor,
        audit_builder=lambda result: {
            "broker_verification_id": result.verification_id,
            "broker_managed_instance_id": result.managed_instance_id,
        },
    )

    def read_promotion_kodi_log_window(
        params: ReadPromotionKodiLogWindowParams,
    ) -> ReadPromotionKodiLogWindowResult:
        try:
            authority = log_resolver.resolve(
                params.verification_id,
                params.managed_instance_id,
                params.expected_container_id,
            )
            window = log_provider.read(authority, params.cursor)
        except KodiLogWindowError as error:
            raise OperationError(
                error.code,
                error.safe_message,
                audit_fields={
                    "broker_verification_id": params.verification_id,
                    "broker_managed_instance_id": params.managed_instance_id,
                },
            ) from error
        return ReadPromotionKodiLogWindowResult(
            verification_id=params.verification_id,
            managed_instance_id=params.managed_instance_id,
            complete=window.complete,
            classification=cast(
                Literal["clean", "target_errors", "unrelated_warnings", "incomplete"],
                window.classification,
            ),
            target_error_count=window.target_error_count,
            unrelated_warning_count=window.unrelated_warning_count,
            snippets=list(window.snippets),
            scanned_bytes=window.scanned_bytes,
        )

    registry.register(
        name="read_promotion_kodi_log_window",
        params_model=ReadPromotionKodiLogWindowParams,
        result_model=ReadPromotionKodiLogWindowResult,
        handler=read_promotion_kodi_log_window,
        audit_builder=lambda result: {
            "broker_verification_id": result.verification_id,
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_log_classification": result.classification,
            "broker_log_target_error_count": result.target_error_count,
            "broker_log_unrelated_warning_count": result.unrelated_warning_count,
            "broker_log_scanned_bytes": result.scanned_bytes,
        },
    )

    def managed_container_inventory(
        _params: EmptyParams,
    ) -> ManagedContainerInventoryResult:
        try:
            records = provider.list_managed_containers()
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely read Docker inventory.",
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container metadata is invalid or ambiguous.",
            ) from error
        except InventoryLimitExceededError as error:
            raise OperationError(
                "inventory_limit_exceeded",
                "Managed container inventory exceeds the supported limit.",
            ) from error

        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=record.managed_instance_id,
                    container_id=record.container_id,
                    image_id=record.image_id,
                    runtime_state=record.runtime_state,
                    health_state=record.health_state,
                )
                for record in records
            ]
        )

    def inventory_audit(
        result: ManagedContainerInventoryResult,
    ) -> dict[str, object]:
        return {"broker_inventory_count": len(result.containers)}

    registry.register(
        name="managed_container_inventory",
        params_model=EmptyParams,
        result_model=ManagedContainerInventoryResult,
        handler=managed_container_inventory,
        audit_builder=inventory_audit,
    )

    def observe_managed_kodi_runtime(
        params: ObserveManagedKodiRuntimeParams,
    ) -> ObserveManagedKodiRuntimeResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id
        }
        try:
            spec = observation_resolver.resolve(params.managed_instance_id)
            outcome = observation_provider.observe_managed_kodi_runtime(
                spec=spec,
                expected_container_id=params.expected_container_id,
            )
        except RuntimeExecutionProfileUnavailableError as error:
            raise OperationError(
                "runtime_execution_profile_unavailable",
                "The pinned runtime has no trusted execution profile.",
                audit_fields=audit_fields,
            ) from error
        except ProvisioningStateError as error:
            raise OperationError(
                "managed_instance_state_unavailable",
                "Canonical managed-instance state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container metadata is invalid.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed runtime revision differs from trusted state.",
                audit_fields=audit_fields,
            ) from error
        except ManagedRuntimeNotRunningError as error:
            raise OperationError(
                "managed_runtime_not_running",
                "The managed Kodi runtime is not running.",
                audit_fields=audit_fields,
            ) from error
        except ManagedRuntimeEndpointError as error:
            raise OperationError(
                "managed_runtime_endpoint_invalid",
                "The managed Kodi runtime endpoint is unavailable or invalid.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely observe the managed runtime.",
                audit_fields=audit_fields,
            ) from error
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            runtime_state=outcome.runtime_state,
            process_state=outcome.process_state,
            ipv4_address=outcome.ipv4_address,
            jsonrpc_port=outcome.jsonrpc_port,
        )

    registry.register(
        name="observe_managed_kodi_runtime",
        params_model=ObserveManagedKodiRuntimeParams,
        result_model=ObserveManagedKodiRuntimeResult,
        handler=observe_managed_kodi_runtime,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_resulting_runtime_state": result.runtime_state.value,
            "broker_kodi_process_state": result.process_state.value,
        },
    )

    def capture_managed_kodi_screenshot(
        params: CaptureManagedKodiScreenshotParams,
    ) -> CaptureManagedKodiScreenshotResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_capture_id": params.capture_id,
        }
        try:
            spec = screenshot_resolver.resolve(
                params.managed_instance_id,
                params.expected_runtime_revision_id,
                params.capture_id,
            )
            outcome = screenshot_provider.capture_managed_kodi_screenshot(
                spec=spec,
                expected_container_id=params.expected_container_id,
            )
        except ScreenshotCaptureUnsupportedError as error:
            raise OperationError(
                "screenshot_unsupported",
                "The managed runtime does not support screenshot capture.",
                audit_fields=audit_fields,
            ) from error
        except ScreenshotCaptureStateError as error:
            raise OperationError(
                "screenshot_capture_unavailable",
                "Canonical screenshot capture state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ScreenshotCaptureIdentityError as error:
            raise OperationError(
                "screenshot_runtime_identity_changed",
                "The managed runtime identity changed during screenshot capture.",
                audit_fields=audit_fields,
            ) from error
        except ScreenshotCaptureError as error:
            raise OperationError(
                "screenshot_capture_failed",
                "The fixed screenshot backend could not complete capture.",
                audit_fields=audit_fields,
            ) from error
        return CaptureManagedKodiScreenshotResult(
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            capture_id=outcome.capture_id,
            backend="x11_get_image",
            raw_byte_size=outcome.raw_byte_size,
        )

    registry.register(
        name="capture_managed_kodi_screenshot",
        params_model=CaptureManagedKodiScreenshotParams,
        result_model=CaptureManagedKodiScreenshotResult,
        handler=capture_managed_kodi_screenshot,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_capture_id": result.capture_id,
            "broker_screenshot_backend": result.backend,
            "broker_screenshot_raw_bytes": result.raw_byte_size,
        },
    )

    def provision_managed_container(
        params: ProvisionManagedContainerParams,
    ) -> ProvisionManagedContainerResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id
        }
        try:
            spec = resolver.resolve(params.managed_instance_id)
            outcome = provisioner.provision_managed_container(spec)
        except PersistentProvisioningUnsupportedError as error:
            raise OperationError(
                "persistent_provisioning_unsupported",
                "Persistent managed-container provisioning is not supported.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeContainerArtifactUnavailableError as error:
            raise OperationError(
                "runtime_container_artifact_unavailable",
                "The pinned runtime has no materialized container artifact.",
                audit_fields=audit_fields,
            ) from error
        except ProvisioningStateError as error:
            raise OperationError(
                "provisioning_state_unavailable",
                "Canonical managed-instance provisioning state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeImageUnavailableError as error:
            raise OperationError(
                "runtime_image_unavailable",
                "The exact immutable runtime image is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ManagedNetworkUnavailableError as error:
            raise OperationError(
                "managed_network_unavailable",
                "The required managed Docker network is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ManagedNetworkConflictError as error:
            raise OperationError(
                "managed_network_conflict",
                "The managed Docker network does not match its trusted contract.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except (
            ProvisioningConflictError,
            InvalidManagedContainerMetadataError,
        ) as error:
            raise OperationError(
                "managed_container_provisioning_conflict",
                "Existing Docker state conflicts with managed provisioning.",
                audit_fields=audit_fields,
            ) from error
        except DockerCreateError as error:
            raise OperationError(
                "docker_create_failed",
                "Docker could not safely create the managed container.",
                audit_fields=audit_fields,
            ) from error
        except PostCreateVerificationError as error:
            raise OperationError(
                "post_create_verification_failed",
                "The broker could not verify the created container.",
                audit_fields=audit_fields,
            ) from error
        except PartialProvisioningError as error:
            raise OperationError(
                "partial_provisioning_requires_reconciliation",
                "Partial provisioning requires operator reconciliation.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely inspect Docker prerequisites.",
                audit_fields=audit_fields,
            ) from error

        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.runtime_revision_id != spec.runtime_revision_id
            or outcome.image_id != spec.image_id
        ):
            raise RuntimeError("Provisioning result identity is inconsistent")
        return ProvisionManagedContainerResult(
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            image_id=outcome.image_id,
            runtime_state=outcome.runtime_state,
            created=outcome.created,
        )

    def provision_audit(
        result: ProvisionManagedContainerResult,
    ) -> dict[str, object]:
        return {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_container_created": result.created,
            "broker_resulting_runtime_state": result.runtime_state.value,
        }

    registry.register(
        name="provision_managed_container",
        params_model=ProvisionManagedContainerParams,
        result_model=ProvisionManagedContainerResult,
        handler=provision_managed_container,
        audit_builder=provision_audit,
    )

    def start_managed_container(
        params: StartManagedContainerParams,
    ) -> StartManagedContainerResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id
        }
        try:
            outcome = provider.start_managed_container(
                managed_instance_id=params.managed_instance_id,
                expected_container_id=params.expected_container_id,
                expected_runtime_state=params.expected_runtime_state,
            )
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container metadata is invalid or inconsistent.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeStatePreconditionError as error:
            raise OperationError(
                "runtime_state_precondition_failed",
                "The managed container runtime state changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except ContainerStateNotReconcilableError as error:
            raise OperationError(
                "container_state_not_reconcilable",
                "The managed container is outside safe two-state reconciliation.",
                audit_fields=audit_fields,
            ) from error
        except DockerStartError as error:
            raise OperationError(
                "docker_start_failed",
                "Docker could not safely complete the managed-container start.",
                audit_fields=audit_fields,
            ) from error
        except PostStartVerificationError as error:
            raise OperationError(
                "post_start_verification_failed",
                "The broker could not verify the running postcondition.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely read Docker container state.",
                audit_fields=audit_fields,
            ) from error

        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.container_id != params.expected_container_id
        ):
            raise RuntimeError("Docker adapter returned inconsistent start identity")

        return StartManagedContainerResult(
            managed_instance_id=outcome.managed_instance_id,
            container_id=outcome.container_id,
            runtime_state=outcome.runtime_state,
            start_performed=outcome.start_performed,
        )

    def start_audit(result: StartManagedContainerResult) -> dict[str, object]:
        return {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_start_performed": result.start_performed,
            "broker_resulting_runtime_state": result.runtime_state.value,
        }

    registry.register(
        name="start_managed_container",
        params_model=StartManagedContainerParams,
        result_model=StartManagedContainerResult,
        handler=start_managed_container,
        audit_builder=start_audit,
    )

    def stop_managed_container(
        params: StopManagedContainerParams,
    ) -> StopManagedContainerResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id
        }
        try:
            outcome = provider.stop_managed_container(
                managed_instance_id=params.managed_instance_id,
                expected_container_id=params.expected_container_id,
                expected_runtime_state=params.expected_runtime_state,
            )
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container metadata is invalid or inconsistent.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeStatePreconditionError as error:
            raise OperationError(
                "runtime_state_precondition_failed",
                "The managed container runtime state changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except ContainerStateNotStoppableError as error:
            raise OperationError(
                "container_state_not_stoppable",
                "The managed container is not in a safely stoppable state.",
                audit_fields=audit_fields,
            ) from error
        except DockerStopError as error:
            raise OperationError(
                "docker_stop_failed",
                "Docker could not safely complete the managed-container stop.",
                audit_fields=audit_fields,
            ) from error
        except PostStopVerificationError as error:
            raise OperationError(
                "post_stop_verification_failed",
                "The broker could not verify the stopped postcondition.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely read Docker container state.",
                audit_fields=audit_fields,
            ) from error

        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.container_id != params.expected_container_id
        ):
            raise RuntimeError("Docker adapter returned inconsistent stop identity")

        return StopManagedContainerResult(
            managed_instance_id=outcome.managed_instance_id,
            container_id=outcome.container_id,
            runtime_state=outcome.runtime_state,
            stop_performed=outcome.stop_performed,
        )

    def stop_audit(result: StopManagedContainerResult) -> dict[str, object]:
        return {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_stop_performed": result.stop_performed,
            "broker_resulting_runtime_state": result.runtime_state.value,
        }

    registry.register(
        name="stop_managed_container",
        params_model=StopManagedContainerParams,
        result_model=StopManagedContainerResult,
        handler=stop_managed_container,
        audit_builder=stop_audit,
    )

    def deprovision_managed_container(
        params: DeprovisionManagedContainerParams,
    ) -> DeprovisionManagedContainerResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_runtime_revision_id": params.expected_runtime_revision_id,
        }
        try:
            pinned_revision_id = pin_resolver.resolve_runtime_revision_id(
                params.managed_instance_id
            )
            if pinned_revision_id != params.expected_runtime_revision_id:
                raise RuntimeRevisionPreconditionError(
                    "Persisted runtime revision differs from the precondition"
                )
            outcome: DeprovisionedManagedContainerRecord = (
                deprovisioner.deprovision_managed_container(
                    managed_instance_id=params.managed_instance_id,
                    expected_container_id=params.expected_container_id,
                    expected_runtime_revision_id=params.expected_runtime_revision_id,
                    expected_runtime_state=params.expected_runtime_state,
                )
            )
        except ProvisioningStateError as error:
            raise OperationError(
                "managed_instance_state_unavailable",
                "Canonical managed-instance state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed instance runtime revision changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container identity metadata is invalid.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "The managed container must be stopped before deprovisioning.",
                audit_fields=audit_fields,
            ) from error
        except DockerRemoveError as error:
            raise OperationError(
                "docker_remove_failed",
                "Docker could not safely remove the managed container.",
                audit_fields=audit_fields,
            ) from error
        except PostDeprovisionVerificationError as error:
            raise OperationError(
                "post_deprovision_verification_failed",
                "The broker could not verify complete container removal.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely read Docker container state.",
                audit_fields=audit_fields,
            ) from error

        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.removed_container_id != params.expected_container_id
            or outcome.runtime_revision_id != params.expected_runtime_revision_id
            or not outcome.removed
        ):
            raise RuntimeError(
                "Docker adapter returned inconsistent deprovision identity"
            )
        return DeprovisionManagedContainerResult(
            managed_instance_id=outcome.managed_instance_id,
            removed_container_id=outcome.removed_container_id,
            runtime_revision_id=outcome.runtime_revision_id,
            removed=True,
        )

    def deprovision_audit(
        result: DeprovisionManagedContainerResult,
    ) -> dict[str, object]:
        return {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_runtime_revision_id": result.runtime_revision_id,
            "broker_container_removed": result.removed,
        }

    registry.register(
        name="deprovision_managed_container",
        params_model=DeprovisionManagedContainerParams,
        result_model=DeprovisionManagedContainerResult,
        handler=deprovision_managed_container,
        audit_builder=deprovision_audit,
    )

    def apply_assigned_fixture(
        params: ApplyAssignedFixtureParams,
    ) -> ApplyAssignedFixtureResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id
        }
        try:
            spec = fixture_resolver.resolve(params.managed_instance_id)
            outcome: AppliedAssignedFixtureRecord = (
                fixture_provider.apply_assigned_fixture(
                    spec=spec,
                    expected_container_id=params.expected_container_id,
                )
            )
        except PersistentProvisioningUnsupportedError as error:
            raise OperationError(
                "persistent_fixture_unsupported",
                "Fixtures are not supported for persistent managed instances.",
                audit_fields=audit_fields,
            ) from error
        except AssignedFixtureMissingError as error:
            raise OperationError(
                "assigned_fixture_missing",
                "The managed instance has no assigned fixture.",
                audit_fields=audit_fields,
            ) from error
        except AssignedFixtureIncompatibleError as error:
            raise OperationError(
                "assigned_fixture_incompatible",
                "The assigned fixture is incompatible with the pinned runtime.",
                audit_fields=audit_fields,
            ) from error
        except PackagedFixtureUnavailableError as error:
            raise OperationError(
                "packaged_fixture_unavailable",
                "The trusted packaged fixture is unavailable or inconsistent.",
                audit_fields=audit_fields,
            ) from error
        except ProvisioningStateError as error:
            raise OperationError(
                "assigned_fixture_state_unavailable",
                "Canonical assigned-fixture state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container identity metadata is invalid.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed runtime revision differs from trusted state.",
                audit_fields=audit_fields,
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "The managed container must be stopped for fixture application.",
                audit_fields=audit_fields,
            ) from error
        except FixtureBaselineConflictError as error:
            raise OperationError(
                "fixture_baseline_conflict",
                "The fresh container already contains fixture-owned state.",
                audit_fields=audit_fields,
            ) from error
        except DockerFixtureArchiveError as error:
            raise OperationError(
                "fixture_archive_failed",
                "Docker could not safely apply the trusted fixture archive.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely inspect fixture container state.",
                audit_fields=audit_fields,
            ) from error
        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.container_id != params.expected_container_id
            or outcome.runtime_revision_id != spec.runtime_revision_id
            or outcome.runtime_state is not ContainerRuntimeState.STOPPED
            or outcome.fixture_slug != spec.fixture_slug
            or outcome.fixture_version != spec.fixture_version
            or outcome.content_digest != spec.content_digest
        ):
            raise RuntimeError("Docker adapter returned inconsistent fixture identity")
        return ApplyAssignedFixtureResult(
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            fixture_slug=cast(
                Literal[
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
                ],
                outcome.fixture_slug,
            ),
            fixture_version=cast(Literal[1, 2, 3, 4, 5, 6], outcome.fixture_version),
            content_digest=outcome.content_digest,
            runtime_state=ContainerRuntimeState.STOPPED,
        )

    registry.register(
        name="apply_assigned_fixture",
        params_model=ApplyAssignedFixtureParams,
        result_model=ApplyAssignedFixtureResult,
        handler=apply_assigned_fixture,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_fixture_slug": result.fixture_slug,
            "broker_fixture_version": result.fixture_version,
            "broker_resulting_runtime_state": result.runtime_state.value,
        },
    )

    def capture_managed_instance_snapshot(
        params: CaptureManagedInstanceSnapshotParams,
    ) -> CaptureManagedInstanceSnapshotResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_snapshot_id": params.snapshot_id,
        }
        try:
            spec = snapshot_resolver.resolve(
                params.snapshot_id, params.managed_instance_id
            )
            outcome = snapshot_provider.capture_managed_instance_snapshot(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except PersistentSnapshotUnsupportedError as error:
            raise OperationError(
                "persistent_snapshot_unsupported",
                "Persistent snapshot capture is not supported.",
                audit_fields=audit_fields,
            ) from error
        except SnapshotStateError as error:
            raise OperationError(
                "snapshot_state_unavailable",
                "Canonical snapshot capture state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except InvalidManagedContainerMetadataError as error:
            raise OperationError(
                "invalid_managed_container_metadata",
                "Kodi Lab managed-container metadata is invalid.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed runtime revision differs from trusted state.",
                audit_fields=audit_fields,
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "Snapshot capture requires a stopped managed container.",
                audit_fields=audit_fields,
            ) from error
        except DockerSnapshotCaptureError as error:
            raise OperationError(
                (
                    "insufficient_snapshot_storage"
                    if error.code == "insufficient_snapshot_storage"
                    else "snapshot_capture_failed"
                ),
                (
                    "Snapshot storage does not have the required free-space reserve."
                    if error.code == "insufficient_snapshot_storage"
                    else "The broker could not safely capture the snapshot."
                ),
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except DockerInventoryReadError as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely inspect snapshot container state.",
                audit_fields=audit_fields,
            ) from error
        artifact = outcome.artifact
        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.container_id != params.expected_container_id
            or outcome.runtime_revision_id != str(spec.context.runtime_revision_id)
            or artifact.snapshot_id != spec.context.snapshot_id
        ):
            raise RuntimeError("Snapshot adapter returned inconsistent identity")
        return CaptureManagedInstanceSnapshotResult(
            snapshot_id=str(artifact.snapshot_id),
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            archive_digest=artifact.archive_digest,
            manifest_digest=artifact.manifest_digest,
            archive_size=artifact.archive_size,
            member_count=artifact.member_count,
        )

    registry.register(
        name="capture_managed_instance_snapshot",
        params_model=CaptureManagedInstanceSnapshotParams,
        result_model=CaptureManagedInstanceSnapshotResult,
        handler=capture_managed_instance_snapshot,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_snapshot_id": result.snapshot_id,
            "broker_snapshot_archive_size": result.archive_size,
            "broker_snapshot_member_count": result.member_count,
        },
    )

    def apply_managed_instance_snapshot(
        params: ApplyManagedInstanceSnapshotParams,
    ) -> ApplyManagedInstanceSnapshotResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_snapshot_id": params.snapshot_id,
        }
        try:
            spec = restore_resolver.resolve(
                params.snapshot_id, params.managed_instance_id
            )
            outcome = restore_provider.apply_managed_instance_snapshot(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except PersistentSnapshotUnsupportedError as error:
            raise OperationError(
                "persistent_snapshot_unsupported",
                "Persistent snapshot restore is not supported.",
                audit_fields=audit_fields,
            ) from error
        except SnapshotStateError as error:
            raise OperationError(
                "snapshot_restore_incompatible",
                "Canonical snapshot restore state is unavailable or incompatible.",
                audit_fields=audit_fields,
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "Snapshot restore requires a stopped fresh container.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed runtime revision differs from trusted state.",
                audit_fields=audit_fields,
            ) from error
        except DockerSnapshotRestoreError as error:
            raise OperationError(
                "snapshot_restore_failed",
                "The broker could not safely apply and verify the snapshot.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except (
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
        ) as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely inspect restore container state.",
                audit_fields=audit_fields,
            ) from error
        artifact = outcome.artifact
        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.container_id != params.expected_container_id
            or outcome.runtime_revision_id != str(spec.context.runtime_revision_id)
            or artifact != spec.artifact
        ):
            raise RuntimeError(
                "Snapshot restore adapter returned inconsistent identity"
            )
        return ApplyManagedInstanceSnapshotResult(
            snapshot_id=str(artifact.snapshot_id),
            managed_instance_id=outcome.managed_instance_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            archive_digest=artifact.archive_digest,
            manifest_digest=artifact.manifest_digest,
            archive_size=artifact.archive_size,
            member_count=artifact.member_count,
            recognized_databases_checked=outcome.recognized_databases_checked,
        )

    registry.register(
        name="apply_managed_instance_snapshot",
        params_model=ApplyManagedInstanceSnapshotParams,
        result_model=ApplyManagedInstanceSnapshotResult,
        handler=apply_managed_instance_snapshot,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_snapshot_id": result.snapshot_id,
            "broker_snapshot_member_count": result.member_count,
            "broker_snapshot_databases_checked": result.recognized_databases_checked,
        },
    )

    def delete_managed_snapshot_artifact(
        params: DeleteManagedSnapshotArtifactParams,
    ) -> DeleteManagedSnapshotArtifactResult:
        audit_fields: dict[str, object] = {
            "broker_snapshot_id": params.snapshot_id,
        }
        try:
            spec = delete_resolver.resolve(params.snapshot_id)
            outcome = delete_provider.delete_managed_snapshot_artifact(spec=spec)
        except SnapshotStateError as error:
            raise OperationError(
                "snapshot_delete_incompatible",
                "Canonical snapshot deletion state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except SnapshotArtifactDeletionError as error:
            raise OperationError(
                error.code,
                error.safe_message,
                audit_fields=audit_fields,
            ) from error
        if outcome.snapshot_id != spec.snapshot_id:
            raise RuntimeError(
                "Snapshot deletion adapter returned inconsistent identity"
            )
        return DeleteManagedSnapshotArtifactResult(
            snapshot_id=str(outcome.snapshot_id),
            artifact_absent=True,
            artifact_was_present=outcome.artifact_was_present,
        )

    registry.register(
        name="delete_managed_snapshot_artifact",
        params_model=DeleteManagedSnapshotArtifactParams,
        result_model=DeleteManagedSnapshotArtifactResult,
        handler=delete_managed_snapshot_artifact,
        audit_builder=lambda result: {
            "broker_snapshot_id": result.snapshot_id,
            "broker_snapshot_artifact_was_present": result.artifact_was_present,
        },
    )

    def copy_sync_attached_development_source(
        params: CopySyncAttachedDevelopmentSourceParams,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_development_source_id": params.development_source_id,
            "broker_deployment_operation_id": params.deployment_operation_id,
        }
        try:
            spec = deployment_resolver.resolve(
                params.managed_instance_id,
                params.development_source_id,
                params.deployment_operation_id,
                params.expected_container_id,
            )
            outcome = deployment_provider.copy_sync_attached_development_source(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except DevelopmentSourceDeploymentStateError as error:
            raise OperationError(
                error.code,
                "Canonical attached development-source state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerDevelopmentSourceDeploymentError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except ManagedContainerNotFoundError as error:
            raise OperationError(
                "managed_container_not_found",
                "No Kodi Lab-owned container exists for the managed instance.",
                audit_fields=audit_fields,
            ) from error
        except AmbiguousManagedContainerError as error:
            raise OperationError(
                "ambiguous_managed_container",
                "Multiple containers claim the managed instance identity.",
                audit_fields=audit_fields,
            ) from error
        except ContainerIdentityPreconditionError as error:
            raise OperationError(
                "container_identity_precondition_failed",
                "The managed container identity changed after observation.",
                audit_fields=audit_fields,
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "Development-source deployment requires a stopped container.",
                audit_fields=audit_fields,
            ) from error
        except RuntimeRevisionPreconditionError as error:
            raise OperationError(
                "runtime_revision_precondition_failed",
                "The managed runtime revision differs from trusted state.",
                audit_fields=audit_fields,
            ) from error
        except DockerDaemonUnavailableError as error:
            raise OperationError(
                "docker_daemon_unavailable",
                "The Docker daemon is unavailable to the broker.",
                audit_fields=audit_fields,
            ) from error
        except (
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
        ) as error:
            raise OperationError(
                "docker_inventory_failed",
                "The broker could not safely inspect deployment container state.",
                audit_fields=audit_fields,
            ) from error
        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.development_source_id != params.development_source_id
            or outcome.deployment_operation_id != params.deployment_operation_id
            or outcome.container_id != params.expected_container_id
            or outcome.runtime_revision_id != spec.runtime_revision_id
        ):
            raise RuntimeError("Deployment adapter returned inconsistent identity")
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=outcome.managed_instance_id,
            development_source_id=outcome.development_source_id,
            deployment_operation_id=outcome.deployment_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            result_classification=outcome.result_classification,
            source_manifest_digest=outcome.source_manifest_digest,
            verified_destination_digest=outcome.verified_destination_digest,
            observed_source_name=outcome.observed_source_name,
            observed_source_version=outcome.observed_source_version,
            new_count=outcome.new_count,
            changed_count=outcome.changed_count,
            unchanged_count=outcome.unchanged_count,
            destination_only_count=outcome.destination_only_count,
            transferred_bytes=outcome.transferred_bytes,
        )

    registry.register(
        name="copy_sync_attached_development_source",
        params_model=CopySyncAttachedDevelopmentSourceParams,
        result_model=CopySyncAttachedDevelopmentSourceResult,
        handler=copy_sync_attached_development_source,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_development_source_id": result.development_source_id,
            "broker_deployment_operation_id": result.deployment_operation_id,
            "broker_deployment_result": result.result_classification,
            "broker_deployment_new_count": result.new_count,
            "broker_deployment_changed_count": result.changed_count,
            "broker_deployment_unchanged_count": result.unchanged_count,
            "broker_deployment_destination_only_count": (result.destination_only_count),
            "broker_deployment_transferred_bytes": result.transferred_bytes,
        },
    )

    def copy_sync_promoted_development_source(
        params: CopySyncPromotedDevelopmentSourceParams,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        audit_fields: dict[str, object] = {
            "broker_verification_id": params.verification_operation_id,
            "broker_managed_instance_id": params.managed_instance_id,
        }
        try:
            promotion_resolver = cast(
                PromotionDevelopmentSourceDeploymentResolver, deployment_resolver
            )
            spec = promotion_resolver.resolve_promotion_verification(
                params.verification_operation_id,
                params.managed_instance_id,
                params.expected_container_id,
            )
            outcome = deployment_provider.copy_sync_attached_development_source(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except DevelopmentSourceDeploymentStateError as error:
            raise OperationError(
                error.code,
                "Promotion-bound sync authority is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerDevelopmentSourceDeploymentError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            ManagedContainerNotFoundError,
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerMustBeStoppedError,
            RuntimeRevisionPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
        ) as error:
            raise OperationError(
                "promotion_verification_sync_failed",
                "Promotion-bound sync could not be completed safely.",
                audit_fields=audit_fields,
            ) from error
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=outcome.managed_instance_id,
            development_source_id=outcome.development_source_id,
            deployment_operation_id=outcome.deployment_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            result_classification=outcome.result_classification,
            source_manifest_digest=outcome.source_manifest_digest,
            verified_destination_digest=outcome.verified_destination_digest,
            observed_source_name=outcome.observed_source_name,
            observed_source_version=outcome.observed_source_version,
            new_count=outcome.new_count,
            changed_count=outcome.changed_count,
            unchanged_count=outcome.unchanged_count,
            destination_only_count=outcome.destination_only_count,
            transferred_bytes=outcome.transferred_bytes,
        )

    registry.register(
        name="copy_sync_promoted_development_source",
        params_model=CopySyncPromotedDevelopmentSourceParams,
        result_model=CopySyncAttachedDevelopmentSourceResult,
        handler=copy_sync_promoted_development_source,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_deployment_operation_id": result.deployment_operation_id,
            "broker_deployment_result": result.result_classification,
        },
    )

    def copy_sync_mcp_workspace(
        params: CopySyncMcpWorkspaceParams,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_workspace_id": params.workspace_id,
            "broker_deployment_operation_id": params.deployment_operation_id,
            "broker_workspace_generation": params.expected_generation,
        }
        try:
            spec = deployment_resolver.resolve_workspace_deployment(
                params.managed_instance_id,
                params.workspace_id,
                params.expected_generation,
                params.deployment_operation_id,
                params.expected_container_id,
            )
            outcome = deployment_provider.copy_sync_attached_development_source(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except DevelopmentSourceDeploymentStateError as error:
            raise OperationError(
                error.code,
                "Canonical workspace deployment state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerDevelopmentSourceDeploymentError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except ContainerMustBeStoppedError as error:
            raise OperationError(
                "container_must_be_stopped",
                "Workspace deployment requires the exact stopped target.",
                audit_fields=audit_fields,
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "workspace_deployment_failed",
                "Workspace deployment could not be completed safely.",
                audit_fields=audit_fields,
            ) from error
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=outcome.managed_instance_id,
            development_source_id=outcome.development_source_id,
            deployment_operation_id=outcome.deployment_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            result_classification=outcome.result_classification,
            source_manifest_digest=outcome.source_manifest_digest,
            verified_destination_digest=outcome.verified_destination_digest,
            observed_source_name=outcome.observed_source_name,
            observed_source_version=outcome.observed_source_version,
            new_count=outcome.new_count,
            changed_count=outcome.changed_count,
            unchanged_count=outcome.unchanged_count,
            destination_only_count=outcome.destination_only_count,
            transferred_bytes=outcome.transferred_bytes,
        )

    registry.register(
        name="copy_sync_mcp_workspace",
        params_model=CopySyncMcpWorkspaceParams,
        result_model=CopySyncAttachedDevelopmentSourceResult,
        handler=copy_sync_mcp_workspace,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_deployment_operation_id": result.deployment_operation_id,
            "broker_deployment_result": result.result_classification,
        },
    )

    def verify_attached_development_source_destination(
        params: VerifyAttachedDevelopmentSourceDestinationParams,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_development_source_id": params.development_source_id,
        }
        try:
            spec = deployment_resolver.resolve_destination_verification(
                params.managed_instance_id,
                params.development_source_id,
                params.expected_container_id,
            )
            outcome = (
                deployment_provider.verify_attached_development_source_destination(
                    spec=spec, expected_container_id=params.expected_container_id
                )
            )
        except DevelopmentSourceDeploymentStateError as error:
            raise OperationError(
                error.code,
                "Canonical destination-verification state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerDevelopmentSourceDeploymentError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except ManagedRuntimeNotRunningError as error:
            raise OperationError(
                "managed_runtime_not_running",
                "Destination verification requires the exact running runtime.",
                audit_fields=audit_fields,
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "development_destination_verification_failed",
                "The copied destination could not be safely verified.",
                audit_fields=audit_fields,
            ) from error
        if (
            outcome.managed_instance_id != params.managed_instance_id
            or outcome.development_source_id != params.development_source_id
            or outcome.container_id != params.expected_container_id
            or not outcome.matched
        ):
            raise RuntimeError("Destination verifier returned inconsistent identity")
        return VerifyAttachedDevelopmentSourceDestinationResult(
            managed_instance_id=outcome.managed_instance_id,
            development_source_id=outcome.development_source_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            addon_id=outcome.addon_id,
            addon_version=outcome.addon_version,
            matched=True,
            video_plugin_descriptor_eligible=(outcome.video_plugin_descriptor_eligible),
        )

    registry.register(
        name="verify_attached_development_source_destination",
        params_model=VerifyAttachedDevelopmentSourceDestinationParams,
        result_model=VerifyAttachedDevelopmentSourceDestinationResult,
        handler=verify_attached_development_source_destination,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_development_source_id": result.development_source_id,
            "broker_destination_matched": result.matched,
        },
    )

    def verify_mcp_workspace_destination(
        params: VerifyMcpWorkspaceDestinationParams,
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_deployment_operation_id": params.deployment_operation_id,
        }
        try:
            spec = deployment_resolver.resolve_workspace_destination_verification(
                params.deployment_operation_id,
                params.managed_instance_id,
                params.expected_container_id,
            )
            outcome = (
                deployment_provider.verify_attached_development_source_destination(
                    spec=spec, expected_container_id=params.expected_container_id
                )
            )
        except DevelopmentSourceDeploymentStateError as error:
            raise OperationError(
                error.code,
                "Workspace destination evidence is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerDevelopmentSourceDeploymentError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            ManagedRuntimeNotRunningError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "workspace_destination_verification_failed",
                "Workspace destination could not be safely verified.",
                audit_fields=audit_fields,
            ) from error
        return VerifyAttachedDevelopmentSourceDestinationResult(
            managed_instance_id=outcome.managed_instance_id,
            development_source_id=outcome.development_source_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            addon_id=outcome.addon_id,
            addon_version=outcome.addon_version,
            matched=True,
            video_plugin_descriptor_eligible=(outcome.video_plugin_descriptor_eligible),
        )

    registry.register(
        name="verify_mcp_workspace_destination",
        params_model=VerifyMcpWorkspaceDestinationParams,
        result_model=VerifyAttachedDevelopmentSourceDestinationResult,
        handler=verify_mcp_workspace_destination,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_destination_matched": result.matched,
        },
    )

    def observe_addon_zip_destination(
        params: AddonZipInstallationParams,
    ) -> AddonZipDestinationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_addon_zip_artifact_id": params.artifact_id,
            "broker_addon_zip_operation_id": params.installation_operation_id,
        }
        try:
            spec = zip_resolver.resolve_observation(
                params.managed_instance_id,
                params.artifact_id,
                params.installation_operation_id,
                params.expected_container_id,
            )
            outcome = zip_provider.observe_addon_zip_destination(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical uploaded-ZIP installation state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "addon_zip_destination_observation_failed",
                "ZIP destination could not be safely observed.",
                audit_fields=audit_fields,
            ) from error
        return AddonZipDestinationResult(
            managed_instance_id=outcome.managed_instance_id,
            artifact_id=outcome.artifact_id,
            installation_operation_id=outcome.installation_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            destination_state=outcome.state,
            matched=outcome.matched,
        )

    registry.register(
        name="observe_addon_zip_destination",
        params_model=AddonZipInstallationParams,
        result_model=AddonZipDestinationResult,
        handler=observe_addon_zip_destination,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_addon_zip_artifact_id": result.artifact_id,
            "broker_addon_zip_operation_id": result.installation_operation_id,
            "broker_addon_zip_destination_state": result.destination_state,
            "broker_addon_zip_destination_matched": result.matched,
        },
    )

    def verify_addon_zip_enablement_destination(
        params: VerifyAddonZipEnablementDestinationParams,
    ) -> VerifyAddonZipEnablementDestinationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_addon_zip_artifact_id": params.artifact_id,
            "broker_addon_zip_enablement_operation_id": (
                params.enablement_operation_id
            ),
        }
        try:
            spec = zip_resolver.resolve_enablement_verification(
                params.managed_instance_id,
                params.artifact_id,
                params.enablement_operation_id,
                params.expected_container_id,
            )
            outcome = zip_provider.observe_addon_zip_destination(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical ZIP enablement verification state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "addon_zip_enablement_destination_verification_failed",
                "ZIP enablement destination could not be safely verified.",
                audit_fields=audit_fields,
            ) from error
        return VerifyAddonZipEnablementDestinationResult(
            managed_instance_id=outcome.managed_instance_id,
            artifact_id=outcome.artifact_id,
            enablement_operation_id=params.enablement_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            addon_id=spec.addon_id,
            addon_version=spec.addon_version,
            destination_state=outcome.state,
            matched=outcome.matched,
        )

    registry.register(
        name="verify_addon_zip_enablement_destination",
        params_model=VerifyAddonZipEnablementDestinationParams,
        result_model=VerifyAddonZipEnablementDestinationResult,
        handler=verify_addon_zip_enablement_destination,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_addon_zip_artifact_id": result.artifact_id,
            "broker_addon_zip_enablement_operation_id": (
                result.enablement_operation_id
            ),
            "broker_addon_zip_destination_state": result.destination_state,
            "broker_addon_zip_destination_matched": result.matched,
        },
    )

    def verify_addon_zip_smoke_destination(
        params: VerifyAddonZipSmokeDestinationParams,
    ) -> VerifyAddonZipSmokeDestinationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_addon_zip_artifact_id": params.artifact_id,
            "broker_addon_zip_smoke_operation_id": params.smoke_operation_id,
        }
        try:
            spec = zip_resolver.resolve_smoke_verification(
                params.managed_instance_id,
                params.artifact_id,
                params.smoke_operation_id,
                params.expected_container_id,
            )
            outcome = zip_provider.observe_addon_zip_destination(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical ZIP smoke verification state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "addon_zip_smoke_destination_verification_failed",
                "ZIP smoke destination could not be safely verified.",
                audit_fields=audit_fields,
            ) from error
        return VerifyAddonZipSmokeDestinationResult(
            managed_instance_id=outcome.managed_instance_id,
            artifact_id=outcome.artifact_id,
            smoke_operation_id=params.smoke_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            addon_id=spec.addon_id,
            addon_version=spec.addon_version,
            destination_state=outcome.state,
            matched=outcome.matched,
        )

    registry.register(
        name="verify_addon_zip_smoke_destination",
        params_model=VerifyAddonZipSmokeDestinationParams,
        result_model=VerifyAddonZipSmokeDestinationResult,
        handler=verify_addon_zip_smoke_destination,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_addon_zip_artifact_id": result.artifact_id,
            "broker_addon_zip_smoke_operation_id": result.smoke_operation_id,
            "broker_addon_zip_destination_state": result.destination_state,
            "broker_addon_zip_destination_matched": result.matched,
        },
    )

    def apply_addon_zip_installation(
        params: AddonZipInstallationParams,
    ) -> ApplyAddonZipInstallationResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_addon_zip_artifact_id": params.artifact_id,
            "broker_addon_zip_operation_id": params.installation_operation_id,
        }
        try:
            spec = zip_resolver.resolve(
                params.managed_instance_id,
                params.artifact_id,
                params.installation_operation_id,
                params.expected_container_id,
            )
            outcome = zip_provider.apply_addon_zip_installation(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical uploaded-ZIP installation state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerMustBeStoppedError,
            DockerDaemonUnavailableError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ) as error:
            raise OperationError(
                "addon_zip_application_failed",
                "ZIP payload application could not be completed safely.",
                audit_fields=audit_fields,
            ) from error
        if not outcome.matched:
            raise RuntimeError("ZIP installation adapter returned an unverified result")
        return ApplyAddonZipInstallationResult(
            managed_instance_id=outcome.managed_instance_id,
            artifact_id=outcome.artifact_id,
            installation_operation_id=outcome.installation_operation_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            matched=True,
        )

    registry.register(
        name="apply_addon_zip_installation",
        params_model=AddonZipInstallationParams,
        result_model=ApplyAddonZipInstallationResult,
        handler=apply_addon_zip_installation,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_addon_zip_artifact_id": result.artifact_id,
            "broker_addon_zip_operation_id": result.installation_operation_id,
            "broker_addon_zip_matched": result.matched,
        },
    )

    def inspect_repository_replacement(
        params: InspectRepositoryReplacementParams,
    ) -> InspectRepositoryReplacementResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_replacement_approval_id": params.approval_id,
        }
        try:
            spec = zip_resolver.resolve_replacement_plan(
                params.managed_instance_id,
                params.approval_id,
                params.expected_container_id,
            )
            outcome = zip_provider.inspect_repository_replacement(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical repository replacement state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        return InspectRepositoryReplacementResult(
            managed_instance_id=outcome.managed_instance_id,
            approval_id=outcome.approval_id,
            artifact_id=outcome.artifact_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            topology=outcome.topology,
        )

    registry.register(
        name="inspect_repository_replacement",
        params_model=InspectRepositoryReplacementParams,
        result_model=InspectRepositoryReplacementResult,
        handler=inspect_repository_replacement,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_replacement_approval_id": result.approval_id,
            "broker_replacement_topology": result.topology,
        },
    )

    def apply_repository_replacement(
        params: ApplyRepositoryReplacementParams,
    ) -> ApplyRepositoryReplacementResult:
        audit_fields: dict[str, object] = {
            "broker_managed_instance_id": params.managed_instance_id,
            "broker_replacement_approval_id": params.approval_id,
            "broker_replacement_operation_id": params.replacement_operation_id,
        }
        try:
            spec = zip_resolver.resolve_replacement_execution(
                params.managed_instance_id,
                params.approval_id,
                params.replacement_operation_id,
                params.expected_container_id,
            )
            outcome = zip_provider.apply_repository_replacement(
                spec=spec, expected_container_id=params.expected_container_id
            )
        except AddonZipInstallationStateError as error:
            raise OperationError(
                error.code,
                "Canonical repository replacement state is unavailable.",
                audit_fields=audit_fields,
            ) from error
        except DockerAddonZipInstallationError as error:
            raise OperationError(
                error.code, error.safe_message, audit_fields=audit_fields
            ) from error
        return ApplyRepositoryReplacementResult(
            managed_instance_id=outcome.managed_instance_id,
            approval_id=outcome.approval_id,
            replacement_operation_id=outcome.replacement_operation_id,
            artifact_id=outcome.artifact_id,
            runtime_revision_id=outcome.runtime_revision_id,
            container_id=outcome.container_id,
            matched=True,
        )

    registry.register(
        name="apply_repository_replacement",
        params_model=ApplyRepositoryReplacementParams,
        result_model=ApplyRepositoryReplacementResult,
        handler=apply_repository_replacement,
        audit_builder=lambda result: {
            "broker_managed_instance_id": result.managed_instance_id,
            "broker_replacement_approval_id": result.approval_id,
            "broker_replacement_operation_id": result.replacement_operation_id,
            "broker_replacement_matched": result.matched,
        },
    )
    return registry

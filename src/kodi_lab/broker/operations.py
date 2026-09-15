"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from kodi_lab import __version__
from kodi_lab.broker.docker_inventory import (
    MAX_MANAGED_CONTAINERS,
    AppliedAssignedFixtureRecord,
    AssignedFixtureApplicationProvider,
    ContainerHealthState,
    ContainerRuntimeState,
    DeprovisionedManagedContainerRecord,
    DockerManagedContainerAdapter,
    KodiProcessState,
    ManagedContainerDeprovisioningProvider,
    ManagedContainerOperationsProvider,
    ManagedContainerProvisioningProvider,
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
    DockerCreateError,
    DockerDaemonUnavailableError,
    DockerFixtureArchiveError,
    DockerInventoryReadError,
    DockerRemoveError,
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
)
from kodi_lab.broker.fixtures import (
    AssignedFixtureResolver,
    SqliteAssignedFixtureResolver,
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


class EmptyParams(StrictModel):
    """An operation that accepts no caller-controlled parameters."""


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
    fixture_slug: Literal["small-mixed-v1", "small-mixed-v2"]
    fixture_version: Literal[1, 2]
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
        }[self.fixture_slug]
        if self.fixture_version != expected_version:
            raise ValueError("fixture slug and version do not match")
        return self


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
    assigned_fixture_resolver: AssignedFixtureResolver | None = None,
    assigned_fixture_provider: AssignedFixtureApplicationProvider | None = None,
) -> OperationRegistry:
    """Build the exact Phase 2 operation registry."""

    registry = OperationRegistry()
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
    fixture_resolver = (
        assigned_fixture_resolver or SqliteAssignedFixtureResolver.production()
    )
    fixture_provider = assigned_fixture_provider or cast(
        AssignedFixtureApplicationProvider, provider
    )
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
                Literal["small-mixed-v1", "small-mixed-v2"], outcome.fixture_slug
            ),
            fixture_version=cast(Literal[1, 2], outcome.fixture_version),
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
    return registry

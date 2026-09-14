"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, Field, ValidationError, field_validator

from kodi_lab import __version__
from kodi_lab.broker.docker_inventory import (
    MAX_MANAGED_CONTAINERS,
    ContainerHealthState,
    ContainerRuntimeState,
    DockerManagedContainerInventory,
    ManagedContainerInventoryProvider,
)
from kodi_lab.broker.errors import (
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    HandlerExecutionError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
    OperationError,
    RequestError,
)
from kodi_lab.broker.protocol import PROTOCOL_VERSION, Request, StrictModel


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


type OperationHandler = Callable[[BaseModel], BaseModel]
type OperationAuditBuilder = Callable[[BaseModel], dict[str, int]]


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
        audit_builder: Callable[[ResultT], dict[str, int]] | None = None,
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

    def audit_fields_for(self, name: str, result: BaseModel) -> dict[str, int]:
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
    inventory_provider: ManagedContainerInventoryProvider | None = None,
) -> OperationRegistry:
    """Build the exact Phase 2 operation registry."""

    registry = OperationRegistry()
    provider = inventory_provider or DockerManagedContainerInventory()
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

    def inventory_audit(result: ManagedContainerInventoryResult) -> dict[str, int]:
        return {"broker_inventory_count": len(result.containers)}

    registry.register(
        name="managed_container_inventory",
        params_model=EmptyParams,
        result_model=ManagedContainerInventoryResult,
        handler=managed_container_inventory,
        audit_builder=inventory_audit,
    )
    return registry

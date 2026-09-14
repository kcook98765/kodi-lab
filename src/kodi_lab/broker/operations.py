"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar, cast

from pydantic import BaseModel, Field, ValidationError

from kodi_lab import __version__
from kodi_lab.broker.errors import HandlerExecutionError, RequestError
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


type OperationHandler = Callable[[BaseModel], BaseModel]


@dataclass(frozen=True, slots=True)
class OperationDefinition:
    """One explicit operation and both sides of its schema."""

    name: str
    params_model: type[BaseModel]
    result_model: type[BaseModel]
    handler: OperationHandler
    requires_authorized_peer: bool = True


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
        )

    @property
    def names(self) -> tuple[str, ...]:
        """Return deterministic capability names."""

        return tuple(sorted(self._operations))

    def definition(self, name: str) -> OperationDefinition | None:
        """Return only an explicitly registered definition."""

        return self._operations.get(name)

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


def build_foundation_registry() -> OperationRegistry:
    """Build the exact non-Docker Phase 2 operation registry."""

    registry = OperationRegistry()
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
    return registry

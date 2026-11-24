"""Fixed broker operation registry and safe Phase 2 handlers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from kodi_lab import __version__
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
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerBusy,
    ReviewedWorkerError,
)
from kodi_lab.runtime.preview import PreviewAcknowledgement


class EmptyParams(StrictModel):
    """An operation that accepts no caller-controlled parameters."""


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

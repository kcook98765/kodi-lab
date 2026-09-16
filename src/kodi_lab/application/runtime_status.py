"""One bounded semantic aggregation path for managed runtime status."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskState,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import ClientError, RemoteBrokerError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


class ProvisionState(StrEnum):
    UNPROVISIONED = "unprovisioned"
    PROVISIONED = "provisioned"
    UNKNOWN = "unknown"


class SemanticJsonRpcState(StrEnum):
    NOT_CHECKED = "not_checked"
    UNAVAILABLE = "unavailable"
    RESPONSIVE = "responsive"
    PROTOCOL_ERROR = "protocol_error"


class SemanticUsabilityState(StrEnum):
    NOT_USABLE = "not_usable"
    CHECKING = "checking"
    USABLE = "usable"
    FAILED = "failed"


class ManagedRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_instance_id: UUID
    name: str = Field(max_length=256)
    kodi_version: str = Field(max_length=32)
    runtime_revision_id: UUID
    runtime_revision_number: int
    provision_state: ProvisionState
    container_state: ContainerRuntimeState
    process_state: KodiProcessState
    jsonrpc_state: SemanticJsonRpcState
    usability_state: SemanticUsabilityState
    readiness_state: ReadinessTaskState
    failure_code: str | None = Field(default=None, max_length=64)
    failure_detail: str | None = Field(default=None, max_length=256)
    allowed_actions: list[str] = Field(max_length=4)


class _StatusBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class ManagedRuntimeStatusService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _StatusBroker,
        readiness: ReadinessTaskRegistry,
        reconciliation: ManagedInstanceReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._readiness = readiness
        self._reconciliation = reconciliation

    def list_statuses(self) -> list[ManagedRuntimeStatus]:
        contexts = self._load_contexts()
        if not contexts:
            return []
        try:
            inventory = self._broker.managed_container_inventory()
        except ClientError as error:
            return [self._dependency_failure(context, error) for context in contexts]
        inventory_by_id = {
            UUID(item.managed_instance_id): item for item in inventory.containers
        }
        statuses = [
            self._aggregate(context, inventory_by_id.get(context[0]))
            for context in contexts
        ]
        if self._reconciliation is None:
            return statuses
        return [self._apply_reconciliation(status) for status in statuses]

    def get(self, instance_id: UUID) -> ManagedRuntimeStatus:
        for status in self.list_statuses():
            if status.managed_instance_id == instance_id:
                return status
        raise ManagedInstanceNotFoundError(instance_id)

    def _apply_reconciliation(
        self, status: ManagedRuntimeStatus
    ) -> ManagedRuntimeStatus:
        service = self._reconciliation
        assert service is not None
        reconciliation = service.status(status.managed_instance_id)
        blocked = {item.value for item in reconciliation.blocked_actions}
        return status.model_copy(
            update={
                "allowed_actions": [
                    action for action in status.allowed_actions if action not in blocked
                ]
            }
        )

    def _load_contexts(self) -> list[tuple[UUID, str, str, UUID, int]]:
        with self._session_factory() as session:
            targets = TargetRepository(session)
            revisions = RuntimeRevisionRepository(session)
            definitions = RuntimeDefinitionRepository(session)
            contexts: list[tuple[UUID, str, str, UUID, int]] = []
            for instance in ManagedInstanceRepository(session).list():
                target = targets.get(instance.target_id)
                revision = revisions.get(instance.runtime_revision_id)
                if target is None or revision is None:
                    continue
                definition = definitions.get(revision.runtime_definition_id)
                if definition is None:
                    continue
                contexts.append(
                    (
                        instance.target_id,
                        target.name,
                        definition.kodi_version,
                        revision.id,
                        revision.revision_number,
                    )
                )
            return contexts

    def _aggregate(
        self,
        context: tuple[UUID, str, str, UUID, int],
        container: ManagedContainerInventoryEntry | None,
    ) -> ManagedRuntimeStatus:
        instance_id = context[0]
        task = self._readiness.snapshot(instance_id)
        if container is None:
            return self._status(
                context,
                ProvisionState.UNPROVISIONED,
                ContainerRuntimeState.STOPPED,
                KodiProcessState.NOT_RUNNING,
                SemanticJsonRpcState.NOT_CHECKED,
                SemanticUsabilityState.NOT_USABLE,
                task.state,
                ["provision"],
            )
        if container.runtime_state is ContainerRuntimeState.STOPPED:
            return self._status(
                context,
                ProvisionState.PROVISIONED,
                container.runtime_state,
                KodiProcessState.NOT_RUNNING,
                SemanticJsonRpcState.NOT_CHECKED,
                SemanticUsabilityState.NOT_USABLE,
                task.state,
                ["start", "deprovision"],
            )
        if container.runtime_state is not ContainerRuntimeState.RUNNING:
            return self._status(
                context,
                ProvisionState.PROVISIONED,
                container.runtime_state,
                KodiProcessState.UNKNOWN_ERROR,
                SemanticJsonRpcState.NOT_CHECKED,
                SemanticUsabilityState.NOT_USABLE,
                task.state,
                [],
                failure_code="runtime_state_unavailable",
                failure_detail="Runtime state is transitional or unknown.",
            )
        try:
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
            )
        except ClientError as error:
            return self._status(
                context,
                ProvisionState.PROVISIONED,
                container.runtime_state,
                KodiProcessState.UNKNOWN_ERROR,
                SemanticJsonRpcState.NOT_CHECKED,
                SemanticUsabilityState.FAILED,
                task.state,
                ["stop"],
                failure_code=self._error_code(error),
                failure_detail=self._safe_detail(error),
            )

        jsonrpc = SemanticJsonRpcState.NOT_CHECKED
        usability = SemanticUsabilityState.NOT_USABLE
        failure_code: str | None = None
        failure_detail: str | None = None
        if task.state is ReadinessTaskState.CHECKING:
            usability = SemanticUsabilityState.CHECKING
        elif task.state is ReadinessTaskState.USABLE and task.result is not None:
            jsonrpc = SemanticJsonRpcState(task.result.jsonrpc_state.value)
            usability = SemanticUsabilityState.USABLE
        elif task.state in {ReadinessTaskState.FAILED, ReadinessTaskState.TIMED_OUT}:
            usability = SemanticUsabilityState.FAILED
            if task.result is not None:
                jsonrpc = SemanticJsonRpcState(task.result.jsonrpc_state.value)
                reason = task.result.failure_reason
                observed = task.result.last_observed_failure
                failure_code = None if reason is None else reason.value
                failure_detail = (
                    None
                    if observed is None
                    else f"Readiness failed: {observed.value.replace('_', ' ')}."
                )
            else:
                failure_code = "readiness_internal_failure"
                failure_detail = "Readiness could not be completed."
        return self._status(
            context,
            ProvisionState.PROVISIONED,
            container.runtime_state,
            observation.process_state,
            jsonrpc,
            usability,
            task.state,
            ["stop"],
            failure_code=failure_code,
            failure_detail=failure_detail,
        )

    def _dependency_failure(
        self, context: tuple[UUID, str, str, UUID, int], error: ClientError
    ) -> ManagedRuntimeStatus:
        return self._status(
            context,
            ProvisionState.UNKNOWN,
            ContainerRuntimeState.UNKNOWN,
            KodiProcessState.UNKNOWN_ERROR,
            SemanticJsonRpcState.NOT_CHECKED,
            SemanticUsabilityState.FAILED,
            self._readiness.snapshot(context[0]).state,
            [],
            failure_code=self._error_code(error),
            failure_detail=self._safe_detail(error),
        )

    @staticmethod
    def _status(
        context: tuple[UUID, str, str, UUID, int],
        provision: ProvisionState,
        container: ContainerRuntimeState,
        process: KodiProcessState,
        jsonrpc: SemanticJsonRpcState,
        usability: SemanticUsabilityState,
        readiness: ReadinessTaskState,
        actions: list[str],
        *,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> ManagedRuntimeStatus:
        instance_id, name, kodi_version, revision_id, revision_number = context
        return ManagedRuntimeStatus(
            managed_instance_id=instance_id,
            name=name,
            kodi_version=kodi_version,
            runtime_revision_id=revision_id,
            runtime_revision_number=revision_number,
            provision_state=provision,
            container_state=container,
            process_state=process,
            jsonrpc_state=jsonrpc,
            usability_state=usability,
            readiness_state=readiness,
            failure_code=failure_code,
            failure_detail=failure_detail,
            allowed_actions=actions,
        )

    @staticmethod
    def _error_code(error: ClientError) -> str:
        return (
            error.code if isinstance(error, RemoteBrokerError) else "broker_unavailable"
        )

    @staticmethod
    def _safe_detail(error: ClientError) -> str:
        if isinstance(error, RemoteBrokerError):
            return error.safe_message[:256]
        return "The runtime service is unavailable."

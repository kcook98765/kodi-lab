"""Administrator record of later containment for a pre-0065 execution ambiguity."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationOperationKind,
)
from kodi_lab.auth.models import ADMIN_KEY
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import InstanceRuntimeState, InstanceType, TargetType
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    HistoricalWorkspaceExecutionContainmentResolutionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    RuntimeContainerArtifactRow,
    TargetRow,
    WorkspaceExecutionRecoveryOperationRow,
)


class HistoricalContainmentError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ContainmentInventoryBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...


class HistoricalContainmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_id: UUID
    request_id: UUID
    execution_operation_id: UUID
    managed_instance_id: UUID
    stop_operation_id: UUID
    observed_runtime_state_revision: int
    state: str = "completed"
    proof: str = "broker_stopped"
    original_execution_state: str = "ambiguous"


class HistoricalWorkspaceExecutionContainmentService:
    """Record one post-execution stopped observation without changing execution."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: ContainmentInventoryBroker,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._broker = broker
        self._clock = clock
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    def resolve(
        self,
        *,
        request_id: UUID,
        execution_operation_id: UUID,
        managed_instance_id: UUID,
        expected_runtime_state_revision: int,
    ) -> HistoricalContainmentResult:
        if expected_runtime_state_revision < 0:
            raise HistoricalContainmentError("containment_not_allowed")
        with self._sessions() as session:
            prior = self._prior(session, request_id, execution_operation_id)
            if prior is not None:
                if prior.managed_instance_id != str(managed_instance_id):
                    raise HistoricalContainmentError("request_id_conflict")
                return self._result(prior)
        try:
            with self._sessions() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                prior = self._prior(session, request_id, execution_operation_id)
                if prior is not None:
                    if prior.managed_instance_id != str(managed_instance_id):
                        raise HistoricalContainmentError("request_id_conflict")
                    return self._result(prior)
                status = self._reconciliation.status(managed_instance_id)
                if (
                    not status.blocking
                    or status.operation_kind
                    is not ReconciliationOperationKind.MCP_WORKSPACE_EXECUTION
                    or status.operation_uuid != execution_operation_id
                ):
                    raise HistoricalContainmentError("containment_not_allowed")
                execution = session.get(
                    McpWorkspaceExecutionOperationRow, str(execution_operation_id)
                )
                target = session.get(TargetRow, str(managed_instance_id))
                instance = session.get(ManagedInstanceRow, str(managed_instance_id))
                if execution is None or target is None or instance is None:
                    raise HistoricalContainmentError("containment_not_allowed")
                workspace = session.get(AgentWorkspaceRow, execution.workspace_id)
                deployment = session.get(
                    McpWorkspaceDeploymentOperationRow, execution.deployment_id
                )
                recovery = session.scalar(
                    select(WorkspaceExecutionRecoveryOperationRow).where(
                        WorkspaceExecutionRecoveryOperationRow.execution_operation_id
                        == execution.id
                    )
                )
                artifact = session.get(
                    RuntimeContainerArtifactRow, instance.runtime_revision_id
                )
                if (
                    execution.target_id != str(managed_instance_id)
                    or execution.state != "ambiguous"
                    or not execution.invocation_dispatched
                    or execution.execution_classification != "timeout"
                    or execution.failure_code != "execution_ambiguous"
                    or execution.execution_kind is not None
                    or execution.original_container_id is not None
                    or execution.original_runtime_state_revision is not None
                    or execution.completed_at is None
                    or target.target_type is not TargetType.MANAGED
                    or instance.instance_type is not InstanceType.DISPOSABLE
                    or instance.runtime_state is not InstanceRuntimeState.STOPPED
                    or instance.runtime_state_revision
                    != expected_runtime_state_revision
                    or artifact is None
                    or workspace is None
                    or workspace.development_source_id
                    != execution.development_source_id
                    or deployment is None
                    or deployment.id != execution.deployment_id
                    or deployment.workspace_id != execution.workspace_id
                    or deployment.target_id != execution.target_id
                    or deployment.generation != execution.generation
                    or deployment.state != "succeeded"
                    or deployment.target_class != "internal_lab"
                    or deployment.source_manifest_digest is None
                    or deployment.verified_destination_digest is None
                    or (
                        recovery is not None
                        and recovery.state == "completed"
                        and recovery.outcome == "recovered"
                    )
                    or session.get(
                        ManagedInstanceMutationLeaseRow, str(managed_instance_id)
                    )
                    is not None
                ):
                    raise HistoricalContainmentError("containment_not_allowed")
                lifecycle = session.scalar(
                    select(ManagedInstanceLifecycleOperationRow)
                    .where(
                        ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                        == str(managed_instance_id)
                    )
                    .order_by(
                        ManagedInstanceLifecycleOperationRow.requested_at.desc(),
                        ManagedInstanceLifecycleOperationRow.id.desc(),
                    )
                    .limit(1)
                )
                if (
                    lifecycle is None
                    or lifecycle.kind.value != "stop"
                    or lifecycle.state.value != "succeeded"
                    or lifecycle.resulting_state != "stopped"
                    or lifecycle.completed_at is None
                    or lifecycle.completed_at <= execution.completed_at
                ):
                    raise HistoricalContainmentError("containment_not_allowed")
                try:
                    inventory = self._broker.managed_container_inventory()
                    matches = [
                        item
                        for item in inventory.containers
                        if item.managed_instance_id == str(managed_instance_id)
                    ]
                except Exception as error:
                    raise HistoricalContainmentError(
                        "containment_not_allowed"
                    ) from error
                if (
                    len(matches) != 1
                    or matches[0].runtime_state is not ContainerRuntimeState.STOPPED
                    or matches[0].image_id != artifact.image_id
                    or instance.runtime_state_revision
                    != expected_runtime_state_revision
                ):
                    raise HistoricalContainmentError("containment_not_allowed")
                now = (
                    self._clock()
                    .astimezone(UTC)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                row = HistoricalWorkspaceExecutionContainmentResolutionRow(
                    id=str(uuid4()),
                    request_id=str(request_id),
                    execution_operation_id=str(execution_operation_id),
                    managed_instance_id=str(managed_instance_id),
                    administrator_key=ADMIN_KEY,
                    stop_operation_id=lifecycle.id,
                    observed_runtime_state_revision=expected_runtime_state_revision,
                    state="completed",
                    proof="broker_stopped",
                    requested_at=now,
                    completed_at=now,
                )
                session.add(row)
                session.commit()
                return self._result(row)
        except IntegrityError as error:
            raise HistoricalContainmentError("containment_not_allowed") from error

    @staticmethod
    def _prior(
        session: Session, request_id: UUID, execution_operation_id: UUID
    ) -> HistoricalWorkspaceExecutionContainmentResolutionRow | None:
        by_request = session.scalar(
            select(HistoricalWorkspaceExecutionContainmentResolutionRow).where(
                HistoricalWorkspaceExecutionContainmentResolutionRow.request_id
                == str(request_id)
            )
        )
        if by_request is not None:
            if by_request.execution_operation_id != str(execution_operation_id):
                raise HistoricalContainmentError("request_id_conflict")
            return by_request
        return session.scalar(
            select(HistoricalWorkspaceExecutionContainmentResolutionRow).where(
                HistoricalWorkspaceExecutionContainmentResolutionRow.execution_operation_id
                == str(execution_operation_id)
            )
        )

    @staticmethod
    def _result(
        row: HistoricalWorkspaceExecutionContainmentResolutionRow,
    ) -> HistoricalContainmentResult:
        return HistoricalContainmentResult(
            resolution_id=UUID(row.id),
            request_id=UUID(row.request_id),
            execution_operation_id=UUID(row.execution_operation_id),
            managed_instance_id=UUID(row.managed_instance_id),
            stop_operation_id=UUID(row.stop_operation_id),
            observed_runtime_state_revision=row.observed_runtime_state_revision,
        )

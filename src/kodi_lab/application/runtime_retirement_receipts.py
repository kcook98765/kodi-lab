"""Exact deprovision witnesses for a later single-session atomic handoff.

No inventory-only reconciliation, resource mutation, grant or new public route.
Prepared scope proves intent. Only an owned successful lifecycle terminalization
and its exact broker removal reply can publish confirmed retirement.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    InstanceType,
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import ManagedRuntimeRetirementRow
from kodi_lab.storage.repositories import (
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
)

MAX_RETIREMENTS = 256


class RuntimeRetirementReceiptError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeRetirementScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    managed_instance_id: UUID
    runtime_revision_id: UUID
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RuntimeRetirementReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    scope: RuntimeRetirementScope
    confirmation: Literal["not_confirmed", "confirmed"]
    prepared_at: datetime
    confirmed_at: datetime | None
    operation_state: ManagedInstanceLifecycleOperationState
    settles_uncertain_create: Literal[False] = False


def require_retirement_scope(session: Session, scope: RuntimeRetirementScope) -> None:
    instance = ManagedInstanceRepository(session).get(scope.managed_instance_id)
    artifact = RuntimeContainerArtifactRepository(session).get(
        scope.runtime_revision_id
    )
    if (
        instance is None
        or instance.instance_type is not InstanceType.DISPOSABLE
        or instance.runtime_revision_id != scope.runtime_revision_id
        or artifact is None
        or artifact.image_id != scope.image_id
    ):
        raise RuntimeRetirementReceiptError("runtime_retirement_scope_changed")
    if (
        session.scalar(select(func.count()).select_from(ManagedRuntimeRetirementRow))
        or 0
    ) >= MAX_RETIREMENTS:
        raise RuntimeRetirementReceiptError("runtime_retirement_history_capacity")


def prepare_retirement_receipt(
    session: Session,
    operation_id: UUID,
    scope: RuntimeRetirementScope,
) -> None:
    operation = ManagedInstanceLifecycleOperationRepository(session).get(operation_id)
    if (
        operation is None
        or operation.kind is not ManagedInstanceLifecycleKind.DEPROVISION
        or operation.state is not ManagedInstanceLifecycleOperationState.RUNNING
        or operation.original_managed_instance_id != scope.managed_instance_id
    ):
        raise RuntimeRetirementReceiptError("runtime_retirement_operation_changed")
    session.add(
        ManagedRuntimeRetirementRow(
            operation_id=str(operation_id),
            original_managed_instance_id=str(scope.managed_instance_id),
            runtime_revision_id=str(scope.runtime_revision_id),
            container_id=scope.container_id,
            image_id=scope.image_id,
            prepared_at=timestamp(operation.started_at),
        )
    )
    session.flush()


def confirm_retirement_receipt(
    session: Session,
    operation: ManagedInstanceLifecycleOperation,
    scope: RuntimeRetirementScope,
) -> None:
    if operation.state is not ManagedInstanceLifecycleOperationState.SUCCEEDED:
        return
    row = session.get(ManagedRuntimeRetirementRow, str(operation.id))
    if (
        row is None
        or row.confirmed_at is not None
        or operation.kind is not ManagedInstanceLifecycleKind.DEPROVISION
        or operation.original_managed_instance_id
        != UUID(row.original_managed_instance_id)
        or operation.resulting_state is not ManagedInstanceSemanticState.UNPROVISIONED
        or operation.completed_at is None
        or (
            row.original_managed_instance_id,
            row.runtime_revision_id,
            row.container_id,
            row.image_id,
        )
        != (
            str(scope.managed_instance_id),
            str(scope.runtime_revision_id),
            scope.container_id,
            scope.image_id,
        )
    ):
        raise RuntimeRetirementReceiptError("runtime_retirement_confirmation_invalid")
    row.confirmed_at = timestamp(operation.completed_at)
    session.flush()


def retirement_receipt_in_session(
    session: Session,
    operation_id: UUID,
) -> RuntimeRetirementReceipt:
    row = session.get(ManagedRuntimeRetirementRow, str(operation_id))
    operation = ManagedInstanceLifecycleOperationRepository(session).get(operation_id)
    if row is None:
        raise RuntimeRetirementReceiptError("runtime_retirement_receipt_not_found")
    if (
        operation is None
        or operation.kind is not ManagedInstanceLifecycleKind.DEPROVISION
        or operation.original_managed_instance_id
        != UUID(row.original_managed_instance_id)
    ):
        raise RuntimeRetirementReceiptError("runtime_retirement_receipt_invalid")
    if row.confirmed_at is not None and (
        operation.state is not ManagedInstanceLifecycleOperationState.SUCCEEDED
        or operation.resulting_state is not ManagedInstanceSemanticState.UNPROVISIONED
        or operation.completed_at is None
        or timestamp(operation.completed_at) != row.confirmed_at
    ):
        raise RuntimeRetirementReceiptError("runtime_retirement_receipt_invalid")
    return RuntimeRetirementReceipt.model_validate(
        dict(
            operation_id=row.operation_id,
            scope=dict(
                managed_instance_id=row.original_managed_instance_id,
                runtime_revision_id=row.runtime_revision_id,
                container_id=row.container_id,
                image_id=row.image_id,
            ),
            confirmation="not_confirmed" if row.confirmed_at is None else "confirmed",
            prepared_at=row.prepared_at,
            confirmed_at=row.confirmed_at,
            operation_state=operation.state,
        )
    )


def read_retirement_receipt(
    sessions: sessionmaker[Session],
    operation_id: UUID,
) -> RuntimeRetirementReceipt:
    with sessions() as session:
        return retirement_receipt_in_session(session, operation_id)

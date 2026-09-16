"""Unified read-only projection over typed managed-instance operation histories."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.domain import ManagedInstanceLifecycleKind
from kodi_lab.storage.repositories import (
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceRepository,
    ResetOperationRepository,
    SnapshotCreateOperationRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRestoreOperationRepository,
)


class ManagedInstanceActivityKind(StrEnum):
    LIFECYCLE = "lifecycle"
    RESET = "reset"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SNAPSHOT_DELETE = "snapshot_delete"
    INSTANCE_DELETE = "instance_delete"


class ManagedInstanceActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ManagedInstanceActivityKind
    operation_uuid: UUID
    original_managed_instance_id: UUID
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    state: str = Field(max_length=32)
    summary: str = Field(max_length=256)
    reconciliation_required: bool
    snapshot_uuid: UUID | None = None
    lifecycle_kind: ManagedInstanceLifecycleKind | None = None
    preserve_snapshots: bool | None = None


_SOURCE_TIE_BREAK = {
    ManagedInstanceActivityKind.LIFECYCLE: 6,
    ManagedInstanceActivityKind.RESET: 5,
    ManagedInstanceActivityKind.SNAPSHOT_CREATE: 4,
    ManagedInstanceActivityKind.SNAPSHOT_RESTORE: 3,
    ManagedInstanceActivityKind.SNAPSHOT_DELETE: 2,
    ManagedInstanceActivityKind.INSTANCE_DELETE: 1,
}


def _outcome(value: str) -> str:
    return value.replace("_", " ")


class ManagedInstanceActivityService:
    """Merge retained typed histories without persisting duplicate activity rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_live_instance(
        self, instance_id: UUID, *, limit: int | None = None
    ) -> list[ManagedInstanceActivityItem]:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is None:
                raise ManagedInstanceNotFoundError(instance_id)
        return self.list_for_original_instance(instance_id, limit=limit)

    def list_for_original_instance(
        self, instance_id: UUID, *, limit: int | None = None
    ) -> list[ManagedInstanceActivityItem]:
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError("activity limit must be between 1 and 1000")
        with self._session_factory() as session:
            result = self._project(session, instance_id)
        result.sort(
            key=lambda item: (
                item.requested_at,
                item.operation_uuid.int,
                _SOURCE_TIE_BREAK[item.kind],
            ),
            reverse=True,
        )
        return result if limit is None else result[:limit]

    @staticmethod
    def _project(
        session: Session, instance_id: UUID
    ) -> list[ManagedInstanceActivityItem]:
        result: list[ManagedInstanceActivityItem] = []
        for lifecycle in ManagedInstanceLifecycleOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.LIFECYCLE,
                    operation_uuid=lifecycle.id,
                    original_managed_instance_id=instance_id,
                    requested_at=lifecycle.requested_at,
                    started_at=lifecycle.started_at,
                    completed_at=lifecycle.completed_at,
                    state=lifecycle.state.value,
                    summary=(
                        f"{lifecycle.kind.value.capitalize()} "
                        f"{_outcome(lifecycle.state.value)}"
                    ),
                    reconciliation_required=lifecycle.reconciliation_required,
                    lifecycle_kind=lifecycle.kind,
                )
            )
        for reset in ResetOperationRepository(session).list_for_instance(instance_id):
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.RESET,
                    operation_uuid=reset.id,
                    original_managed_instance_id=instance_id,
                    requested_at=reset.requested_at,
                    started_at=reset.started_at,
                    completed_at=reset.completed_at,
                    state=reset.state.value,
                    summary=f"Reset {_outcome(reset.state.value)}",
                    reconciliation_required=reset.reconciliation_required,
                )
            )
        for create in SnapshotCreateOperationRepository(session).list_for_instance(
            instance_id
        ):
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.SNAPSHOT_CREATE,
                    operation_uuid=create.id,
                    original_managed_instance_id=instance_id,
                    requested_at=create.requested_at,
                    started_at=create.started_at,
                    completed_at=create.completed_at,
                    state=create.state.value,
                    summary=f"Snapshot creation {_outcome(create.state.value)}",
                    reconciliation_required=create.reconciliation_required,
                    snapshot_uuid=create.snapshot_id,
                )
            )
        for restore in SnapshotRestoreOperationRepository(session).list_for_instance(
            instance_id
        ):
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.SNAPSHOT_RESTORE,
                    operation_uuid=restore.id,
                    original_managed_instance_id=instance_id,
                    requested_at=restore.requested_at,
                    started_at=restore.started_at,
                    completed_at=restore.completed_at,
                    state=restore.state.value,
                    summary=f"Snapshot restore {_outcome(restore.state.value)}",
                    reconciliation_required=restore.reconciliation_required,
                    snapshot_uuid=restore.snapshot_id,
                )
            )
        for deletion in SnapshotDeleteOperationRepository(session).list_for_instance(
            instance_id
        ):
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.SNAPSHOT_DELETE,
                    operation_uuid=deletion.id,
                    original_managed_instance_id=instance_id,
                    requested_at=deletion.requested_at,
                    started_at=deletion.started_at,
                    completed_at=deletion.completed_at,
                    state=deletion.state.value,
                    summary=f"Snapshot deletion {_outcome(deletion.state.value)}",
                    reconciliation_required=deletion.reconciliation_required,
                    snapshot_uuid=deletion.snapshot_id,
                )
            )
        for instance_delete in ManagedInstanceDeleteOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            if instance_delete.state.value == "succeeded":
                summary = (
                    "Instance deletion preserved Snapshots"
                    if instance_delete.preserve_snapshots
                    else "Instance deletion destroyed Snapshots"
                )
            else:
                summary = f"Instance deletion {_outcome(instance_delete.state.value)}"
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.INSTANCE_DELETE,
                    operation_uuid=instance_delete.id,
                    original_managed_instance_id=instance_id,
                    requested_at=instance_delete.requested_at,
                    started_at=instance_delete.started_at,
                    completed_at=instance_delete.completed_at,
                    state=instance_delete.state.value,
                    summary=summary,
                    reconciliation_required=instance_delete.reconciliation_required,
                    preserve_snapshots=instance_delete.preserve_snapshots,
                )
            )
        return result

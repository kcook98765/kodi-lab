"""Unified read-only projection over typed managed-instance operation histories."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.domain import (
    AddonZipInstallationOperationState,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    ManagedInstanceLifecycleKind,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationOperationRepository,
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceRepository,
    ResetOperationRepository,
    SnapshotCreateOperationRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRestoreOperationRepository,
)


class ManagedInstanceActivityKind(StrEnum):
    ADDON_ZIP_INSTALLATION = "addon_zip_installation"
    DEVELOPMENT_SOURCE_ATTACHMENT = "development_source_attachment"
    DEVELOPMENT_SOURCE_DEPLOYMENT = "development_source_deployment"
    DEVELOPMENT_SOURCE_ENABLEMENT = "development_source_enablement"
    DEVELOPMENT_SOURCE_SMOKE_TEST = "development_source_smoke_test"
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
    development_source_attachment_kind: (
        DevelopmentSourceAttachmentOperationKind | None
    ) = None
    addon_zip_artifact_uuid: UUID | None = None


_SOURCE_TIE_BREAK = {
    ManagedInstanceActivityKind.ADDON_ZIP_INSTALLATION: 11,
    ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_SMOKE_TEST: 10,
    ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ENABLEMENT: 9,
    ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_DEPLOYMENT: 8,
    ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ATTACHMENT: 7,
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
        for operation in DevelopmentSourceAttachmentOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            if operation.state is DevelopmentSourceAttachmentOperationState.SUCCEEDED:
                summary = (
                    "Development source attached"
                    if operation.kind is DevelopmentSourceAttachmentOperationKind.ATTACH
                    else "Development source detached"
                )
            else:
                summary = (
                    "Development source attachment failed"
                    if operation.kind is DevelopmentSourceAttachmentOperationKind.ATTACH
                    else "Development source detachment failed"
                )
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ATTACHMENT,
                    operation_uuid=operation.id,
                    original_managed_instance_id=instance_id,
                    requested_at=operation.requested_at,
                    started_at=None,
                    completed_at=operation.completed_at,
                    state=operation.state.value,
                    summary=summary,
                    reconciliation_required=False,
                    development_source_attachment_kind=operation.kind,
                )
            )
        for deployment in DevelopmentSourceDeploymentOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            if (
                deployment.state is DevelopmentSourceDeploymentOperationState.SUCCEEDED
                and deployment.result_classification is not None
            ):
                summary = (
                    "Development source copy "
                    f"{deployment.result_classification.value.replace('_', ' ')}"
                )
            else:
                summary = (
                    "Development source copy "
                    f"{deployment.state.value.replace('_', ' ')}"
                )
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
                    operation_uuid=deployment.id,
                    original_managed_instance_id=instance_id,
                    requested_at=deployment.requested_at,
                    started_at=deployment.started_at,
                    completed_at=deployment.completed_at,
                    state=deployment.state.value,
                    summary=summary,
                    reconciliation_required=deployment.reconciliation_required,
                )
            )
        for enablement in DevelopmentSourceEnablementOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            if (
                enablement.result_classification
                is DevelopmentSourceEnablementResult.ENABLED
            ):
                summary = "Development source add-on enabled"
            elif (
                enablement.result_classification
                is DevelopmentSourceEnablementResult.ALREADY_ENABLED
            ):
                summary = "Add-on already enabled"
            elif (
                enablement.result_classification
                is DevelopmentSourceEnablementResult.DEPENDENCY_BLOCKED
            ):
                summary = "Enablement blocked by dependency"
            elif (
                enablement.state
                is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
            ):
                summary = "Enablement requires reconciliation"
            else:
                summary = "Development source enablement failed"
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ENABLEMENT,
                    operation_uuid=enablement.id,
                    original_managed_instance_id=instance_id,
                    requested_at=enablement.requested_at,
                    started_at=enablement.started_at,
                    completed_at=enablement.completed_at,
                    state=enablement.state.value,
                    summary=summary,
                    reconciliation_required=enablement.reconciliation_required,
                )
            )
        for smoke in DevelopmentSourceSmokeTestOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            summary = {
                DevelopmentSourceSmokeTestOverallResult.PASSED: "Smoke test passed",
                DevelopmentSourceSmokeTestOverallResult.EXECUTION_FAILED: (
                    "Smoke execution failed"
                ),
                DevelopmentSourceSmokeTestOverallResult.TIMEOUT: (
                    "Smoke test timed out"
                ),
                DevelopmentSourceSmokeTestOverallResult.DEPENDENCY_BLOCKED: (
                    "Smoke blocked by dependency"
                ),
                DevelopmentSourceSmokeTestOverallResult.SOURCE_TREE_CHANGED: (
                    "Smoke changed deployed source tree"
                ),
                DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE: (
                    "Smoke test unsupported add-on type"
                ),
                DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED: (
                    "Smoke requires reconciliation"
                ),
                None: (
                    "Smoke test queued"
                    if smoke.state is DevelopmentSourceSmokeTestOperationState.QUEUED
                    else "Smoke test precondition failed"
                ),
            }[smoke.overall_result]
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_SMOKE_TEST,
                    operation_uuid=smoke.id,
                    original_managed_instance_id=instance_id,
                    requested_at=smoke.requested_at,
                    started_at=smoke.started_at,
                    completed_at=smoke.completed_at,
                    state=smoke.state.value,
                    summary=summary,
                    reconciliation_required=smoke.reconciliation_required,
                )
            )
        for installation in AddonZipInstallationOperationRepository(
            session
        ).list_for_original_instance(instance_id):
            if installation.state is AddonZipInstallationOperationState.SUCCEEDED:
                summary = "Uploaded ZIP installed and recognized disabled"
            elif (
                installation.state
                is AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
            ):
                summary = "Uploaded ZIP installation requires reconciliation"
            else:
                summary = (
                    f"Uploaded ZIP installation {_outcome(installation.state.value)}"
                )
            result.append(
                ManagedInstanceActivityItem(
                    kind=ManagedInstanceActivityKind.ADDON_ZIP_INSTALLATION,
                    operation_uuid=installation.id,
                    original_managed_instance_id=instance_id,
                    requested_at=installation.requested_at,
                    started_at=installation.started_at,
                    completed_at=installation.completed_at,
                    state=installation.state.value,
                    summary=summary,
                    reconciliation_required=installation.reconciliation_required,
                    addon_zip_artifact_uuid=installation.artifact_id,
                )
            )
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

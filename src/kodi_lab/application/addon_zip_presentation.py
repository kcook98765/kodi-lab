"""Safe uploaded-ZIP artifact and operation projections for Web."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    AddonZipImportOperation,
    AddonZipImportOperationPhase,
    AddonZipImportOperationState,
    AddonZipInstallationOperationPhase,
    AddonZipInstallationOperationState,
    AddonZipOrigin,
    AddonZipValidationState,
)
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationOperationRepository,
)


class AddonZipArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    logical_label: str = Field(max_length=256)
    addon_id: str = Field(max_length=128)
    addon_name: str = Field(max_length=256)
    addon_version: str = Field(max_length=128)
    compressed_size: int = Field(ge=1)
    total_uncompressed_bytes: int = Field(ge=0)
    member_count: int = Field(ge=1)
    origin: AddonZipOrigin
    validation_state: AddonZipValidationState
    imported_at: datetime
    latest_installation_state: AddonZipInstallationOperationState | None = None
    latest_installation_phase: AddonZipInstallationOperationPhase | None = None


class AddonZipImportStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    artifact_id: UUID
    logical_label: str = Field(max_length=256)
    state: AddonZipImportOperationState
    phase: AddonZipImportOperationPhase
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)


class AddonZipPresentationService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_artifacts(self) -> list[AddonZipArtifactSummary]:
        with self._session_factory() as session:
            artifacts = AddonZipArtifactRepository(session).list()
            operations = AddonZipInstallationOperationRepository(session)
            result: list[AddonZipArtifactSummary] = []
            for artifact in artifacts:
                if artifact.origin is not AddonZipOrigin.UPLOADED_ZIP:
                    continue
                latest = next(
                    (item for item in operations.list_for_artifact(artifact.id)),
                    None,
                )
                result.append(
                    AddonZipArtifactSummary(
                        artifact_id=artifact.id,
                        logical_label=artifact.logical_label,
                        addon_id=artifact.addon_id,
                        addon_name=artifact.addon_name,
                        addon_version=artifact.addon_version,
                        compressed_size=artifact.compressed_size,
                        total_uncompressed_bytes=artifact.total_uncompressed_bytes,
                        member_count=artifact.member_count,
                        origin=artifact.origin,
                        validation_state=artifact.validation_state,
                        imported_at=artifact.imported_at,
                        latest_installation_state=(
                            None if latest is None else latest.state
                        ),
                        latest_installation_phase=(
                            None if latest is None else latest.phase
                        ),
                    )
                )
            return result

    @staticmethod
    def import_status(operation: AddonZipImportOperation) -> AddonZipImportStatus:
        return AddonZipImportStatus(
            operation_id=operation.id,
            artifact_id=operation.artifact_id,
            logical_label=operation.logical_label,
            state=operation.state,
            phase=operation.phase,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
        )

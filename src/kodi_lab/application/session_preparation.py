"""One stored preparation snapshot; no runtime probe, grant or mutation."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionStatus, _status
from kodi_lab.application.managed_capability_prerequisites import (
    CapabilityPrerequisitesUnavailable,
    ManagedCapabilityPrerequisites,
    ManagedCapabilityPrerequisiteService,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    DeploymentPreflightResult,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.models import ManagedInstanceRow
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)


class SessionPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_revision: Literal[1] = 1
    assessment_scope: Literal["stored_session_preparation"] = (
        "stored_session_preparation"
    )
    session: DevelopmentSessionStatus
    checked_at: datetime
    source_name: str = Field(max_length=120)
    addon_id: str = Field(max_length=128)
    workspace_generation: int = Field(ge=0)
    deployment: DeploymentPreflightResult
    capabilities: ManagedCapabilityPrerequisites
    current_container: Literal["not_observed"] = "not_observed"
    native_readiness: Literal["unknown"] = "unknown"
    addon_enabled: Literal["unknown"] = "unknown"
    agent_authority: Literal["not_assessed"] = "not_assessed"
    reservation_created: Literal[False] = False


class SessionPreparationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions
        self._preflight = WorkspaceDeploymentPreflightService(sessions)
        self._capabilities = ManagedCapabilityPrerequisiteService(sessions)

    def inspect(
        self, *, expected_generation: int | None = None
    ) -> SessionPreparation | None:
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 1
        ):
            raise DevelopmentSessionConflict("development_session_generation_conflict")
        with self._sessions() as session:
            # Explicit DEFERRED BEGIN keeps the first SELECT's SQLite snapshot
            # across all three projections without taking the writer slot.
            session.connection().exec_driver_sql("BEGIN")
            current = active_session(session)
            if current is None:
                if expected_generation is not None:
                    raise DevelopmentSessionConflict(
                        "development_session_generation_conflict"
                    )
                return None
            if (
                expected_generation is not None
                and current.generation != expected_generation
            ):
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            instance = session.get(ManagedInstanceRow, current.target_id)
            workspace = AgentWorkspaceRepository(session).get(
                UUID(current.workspace_id)
            )
            source = DevelopmentSourceRepository(session).get(UUID(current.source_id))
            if (
                instance is None
                or instance.runtime_revision_id != current.runtime_revision_id
                or workspace is None
                or source is None
                or str(workspace.development_source_id) != current.source_id
                or workspace.source_addon_id != source.addon_id
            ):
                raise DevelopmentSessionConflict("development_session_identity_changed")
            deployment = self._preflight.inspect(
                DeploymentPreflightInput(
                    workspace_id=UUID(current.workspace_id),
                    expected_generation=workspace.generation,
                    target_id=UUID(current.target_id),
                    deployment_mode="direct",
                ),
                read_session=session,
            )
            try:
                capabilities = self._capabilities.inspect(
                    UUID(current.target_id), read_session=session
                )
            except CapabilityPrerequisitesUnavailable as error:
                raise DevelopmentSessionConflict(
                    "development_session_identity_changed"
                ) from error
            if (
                deployment.workspace_id != UUID(current.workspace_id)
                or deployment.source_id != UUID(current.source_id)
                or deployment.target_id != UUID(current.target_id)
                or deployment.current_generation != workspace.generation
                or capabilities.target_id != UUID(current.target_id)
                or capabilities.runtime_revision_id != UUID(current.runtime_revision_id)
            ):
                raise DevelopmentSessionConflict("development_session_identity_changed")
            return SessionPreparation(
                session=_status(current),
                checked_at=datetime.now(UTC),
                source_name=source.logical_name,
                addon_id=source.addon_id,
                workspace_generation=workspace.generation,
                deployment=deployment,
                capabilities=capabilities,
            )

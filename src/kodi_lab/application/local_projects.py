"""Web-admin local project onboarding over existing source/workspace authority."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.mcp_workspace_mutations import (
    McpWorkspaceCreationService,
    McpWorkspaceMutationError,
)
from kodi_lab.development.projects import LocalProjectError, LocalProjectResolver
from kodi_lab.development.sources import DevelopmentSourceValidationError
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.models import (
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpDevelopmentRequestRow,
    McpPresetPermissionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

_REQUIRED = frozenset({"workspace.edit", "workspace.inspect"})


class LocalProjectSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_id: UUID
    project: str = Field(min_length=1, max_length=1024)


class LocalDevelopmentClient(BaseModel):
    id: UUID
    display_name: str


class LocalProjectReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_id: UUID
    source_id: UUID
    workspace_id: UUID
    request_id: UUID
    creation_operation_id: UUID
    addon_id: str
    name: str
    version: str
    generation: int
    state: Literal["preparing", "ready", "needs_review"]
    message: str
    next_url: str


def _request_id(client_id: UUID, source_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"kodi-lab:local-project:{client_id}:{source_id}")


class LocalProjectOnboardingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        development_root: Path | None,
        workspaces: AgentWorkspaceService,
        creation: McpWorkspaceCreationService,
    ) -> None:
        self._sessions, self._workspaces, self._creation = (
            sessions,
            workspaces,
            creation,
        )
        self._resolver = LocalProjectResolver(development_root)

    @staticmethod
    def _require_client(session: Session, client_id: UUID) -> McpClientRow:
        client = session.get(McpClientRow, str(client_id))
        permissions = (
            set()
            if client is None
            else set(
                session.scalars(
                    select(McpPresetPermissionRow.permission).where(
                        McpPresetPermissionRow.preset_id == client.preset_id
                    )
                )
            )
        )
        if client is None or not client.enabled or not permissions >= _REQUIRED:
            raise LocalProjectError(
                "development_client_ineligible",
                "Select an enabled development client with workspace "
                "inspection/editing permissions in Agents.",
            )
        return client

    def clients(self) -> list[LocalDevelopmentClient]:
        with self._sessions() as session:
            result = []
            for client in session.scalars(
                select(McpClientRow)
                .where(McpClientRow.enabled)
                .order_by(McpClientRow.display_name)
                .limit(101)
            ):
                try:
                    self._require_client(session, UUID(client.id))
                except LocalProjectError:
                    continue
                result.append(
                    LocalDevelopmentClient(
                        id=UUID(client.id), display_name=client.display_name
                    )
                )
            return result

    def projects(self) -> list[str]:
        """Web-only root-relative choices; no MCP filesystem discovery endpoint."""
        try:
            discovered = self._resolver.discover()
            with self._sessions() as session:
                registered = [
                    s.relative_locator
                    for s in DevelopmentSourceRepository(session).list()
                ]
            return sorted(set(discovered + registered))
        except (OSError, ValueError) as error:
            raise self._safe_error(error) from error

    @staticmethod
    def _safe_error(error: Exception) -> LocalProjectError:
        if isinstance(error, LocalProjectError):
            return error
        if isinstance(error, DevelopmentSourceValidationError):
            return LocalProjectError(
                str(error.code), f"Check the project's addon.xml: {error}"
            )
        return LocalProjectError(
            "project_unavailable",
            "The project directory is missing, inaccessible or unsafe. Check its "
            "location and read permissions inside the approved development root.",
        )

    def onboard(self, selection: LocalProjectSelection) -> LocalProjectReadiness:
        try:
            observation = self._resolver.resolve(selection.project)
        except (OSError, ValueError) as error:
            raise self._safe_error(error) from error
        now = datetime.now(UTC)
        try:
            with self._sessions() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._require_client(session, selection.client_id)
                sources = DevelopmentSourceRepository(session)
                source = sources.get_by_relative_locator(observation.relative_locator)
                identity = sources.get_by_addon_id(observation.addon_id)
                if (source is not None and source.addon_id != observation.addon_id) or (
                    identity is not None
                    and (source is None or identity.id != source.id)
                ):
                    raise LocalProjectError(
                        "project_source_conflict",
                        "This add-on ID is registered at another location, or this "
                        "location now declares another ID. Review Sources in Advanced; "
                        "the existing registration and workspaces were preserved.",
                    )
                if source is None:
                    # IDs are valid logical names except for the domain's shorter
                    # logical-name bound. Include a UUID on the rare name collision.
                    source_id = uuid4()
                    name = observation.addon_id[:120]
                    if sources.get_by_logical_name(name) is not None:
                        name = f"{name[:82]} {source_id}"
                    source = DevelopmentSource(
                        id=source_id,
                        logical_name=name,
                        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                        relative_locator=observation.relative_locator,
                        addon_id=observation.addon_id,
                        observed_name=observation.observed_name,
                        observed_version=observation.observed_version,
                        extension_types=observation.extension_types,
                        registered_at=now,
                        validated_at=now,
                    )
                    sources.create(source)
                key = {
                    "client_id": str(selection.client_id),
                    "development_source_id": str(source.id),
                }
                if session.get(McpClientDevelopmentSourceGrantRow, key) is None:
                    session.add(
                        McpClientDevelopmentSourceGrantRow(
                            **key,
                            created_at=now.isoformat(timespec="microseconds").replace(
                                "+00:00", "Z"
                            ),
                        )
                    )
                    McpAdministrationService._event(
                        session,
                        "development_source_granted",
                        now,
                        client_id=selection.client_id,
                        development_source_id=source.id,
                    )
                session.commit()
            allocation = self._creation.create(
                client_id=selection.client_id,
                request_id=_request_id(selection.client_id, source.id),
                source_id=source.id,
                reuse_project=True,
            )
        except McpWorkspaceMutationError as error:
            message = {
                "project_workspaces_ambiguous": (
                    "More than one workspace belongs to this client and project. "
                    "Choose the intended workspace in Advanced; none was replaced."
                ),
                "workspace_quota_exceeded": (
                    "Workspace capacity is busy or full. Review Workspaces and active "
                    "operations, then select the same project again; "
                    "existing data was kept."
                ),
                "source_not_found_or_not_authorized": (
                    "The development client's access changed. Review it in Agents "
                    "before selecting this project again."
                ),
            }.get(
                error.code,
                "The workspace outcome needs review. Select this project to recover "
                "its original operation; do not create a replacement workspace.",
            )
            raise LocalProjectError(error.code, message) from error
        except SQLAlchemyError as error:
            raise LocalProjectError(
                "project_onboarding_uncertain",
                "Onboarding could not be confirmed. Select this same project to "
                "recover its saved setup; do not create a replacement registration.",
            ) from error
        return self.status(selection.client_id, allocation.workspace_id)

    def status(self, client_id: UUID, workspace_id: UUID) -> LocalProjectReadiness:
        with self._sessions() as session:
            self._require_client(session, client_id)
            binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
            if (
                binding is None
                or binding.client_id != str(client_id)
                or session.get(
                    McpClientDevelopmentSourceGrantRow,
                    {
                        "client_id": str(client_id),
                        "development_source_id": binding.development_source_id,
                    },
                )
                is None
            ):
                raise LocalProjectError(
                    "project_not_authorized",
                    "This workspace is not authorized for the selected client. Review "
                    "its exact project access in Agents.",
                )
            original = session.scalar(
                select(McpDevelopmentRequestRow).where(
                    McpDevelopmentRequestRow.client_id == str(client_id),
                    McpDevelopmentRequestRow.workspace_id == str(workspace_id),
                    McpDevelopmentRequestRow.operation_kind == "workspace_create",
                )
            )
            request_id = (
                _request_id(client_id, UUID(binding.development_source_id))
                if original is None
                else UUID(original.request_id)
            )
        workspace = self._workspaces.get(workspace_id)
        operation = self._workspaces.creation_status_for_workspace(workspace_id)
        if (
            operation is None
            or operation.workspace_id != workspace.id
            or operation.development_source_id != workspace.development_source_id
            or binding.development_source_id != str(workspace.development_source_id)
            or (
                original is not None
                and (
                    original.creation_operation_id != str(operation.operation_id)
                    or original.development_source_id
                    != str(workspace.development_source_id)
                )
            )
        ):
            raise LocalProjectError(
                "project_operation_missing",
                "Review this workspace's original creation history in Advanced.",
            )
        ready = (
            workspace.state.value == "ready"
            and workspace.bytes_state.value == "present"
            and operation.state.value == "succeeded"
        )
        pending = workspace.state.value == "creating" and operation.state.value in {
            "queued",
            "running",
        }
        state: Literal["preparing", "ready", "needs_review"] = (
            "ready" if ready else "preparing" if pending else "needs_review"
        )
        return LocalProjectReadiness(
            client_id=client_id,
            source_id=workspace.development_source_id,
            workspace_id=workspace_id,
            request_id=request_id,
            creation_operation_id=operation.operation_id,
            addon_id=workspace.source_addon_id,
            name=workspace.source_observed_name,
            version=workspace.source_observed_version,
            generation=workspace.generation,
            state=state,
            message=(
                "Project workspace is ready. Existing workspace edits are kept; "
                "later source changes require the existing source scan/capture review. "
                "Choose Kodi next."
            )
            if ready
            else (
                "Preparing a verified workspace. "
                "Refresh status to follow the same operation."
            )
            if pending
            else (
                "Workspace preparation needs review. Open the saved workspace history; "
                "selecting again keeps this original operation and its data."
            ),
            next_url=f"/development-session/retained?workspace_id={workspace_id}"
            if ready
            else f"/agent-workspaces/{workspace_id}",
        )

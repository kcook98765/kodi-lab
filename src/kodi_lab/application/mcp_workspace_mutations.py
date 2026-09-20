"""Replay-safe MCP workspace allocation over the Phase 9 creation service."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceService,
)
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspaceCreationOperation,
    AgentWorkspaceState,
    DevelopmentSourceKind,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpDevelopmentRequestRow,
    McpPresetPermissionRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceCreationOperationRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)

DEVELOPMENT_REQUEST_RETENTION = timedelta(days=30)
DEVELOPMENT_REQUEST_MAX_PER_CLIENT = 10_000
DEVELOPMENT_REQUEST_MAX_GLOBAL = 100_000
MCP_PRESENT_WORKSPACES_PER_CLIENT = 4
MCP_PRESENT_WORKSPACES_GLOBAL = 32
MCP_CREATING_WORKSPACES_PER_CLIENT = 1
WORKSPACE_CREATE_OPERATION_KIND = "workspace_create"


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fingerprint(source_id: UUID) -> str:
    normalized = f"{WORKSPACE_CREATE_OPERATION_KIND}\x00{source_id}"
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


class McpWorkspaceMutationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    workspace_id: UUID
    source_id: UUID
    creation_operation_id: UUID
    creation_operation_state: str = Field(max_length=32)
    lifecycle_state: str = Field(max_length=32)
    binding_classification: str = "mcp_created"
    created_at: datetime


class _ReplayDetected(Exception):
    pass


class McpWorkspaceCreationService:
    """Atomically bind one MCP client to exactly one queued Phase 9 workspace."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        workspaces: AgentWorkspaceService,
        submit_creation: Callable[[UUID], None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        retention: timedelta = DEVELOPMENT_REQUEST_RETENTION,
        max_per_client: int = DEVELOPMENT_REQUEST_MAX_PER_CLIENT,
        max_global: int = DEVELOPMENT_REQUEST_MAX_GLOBAL,
    ) -> None:
        if retention <= timedelta(0) or max_per_client < 1 or max_global < 1:
            raise ValueError("development request bounds must be positive")
        self._sessions = session_factory
        self._workspaces = workspaces
        self._submit_creation = submit_creation
        self._clock = clock
        self._retention = retention
        self._max_per_client = max_per_client
        self._max_global = max_global

    def create(
        self, *, client_id: UUID, request_id: UUID, source_id: UUID
    ) -> WorkspaceCreateResult:
        fingerprint = _fingerprint(source_id)

        def bind(
            session: Session,
            workspace: AgentWorkspace,
            operation: AgentWorkspaceCreationOperation,
        ) -> None:
            now = self._clock()
            cutoff = _to_text(now - self._retention)
            session.execute(
                delete(McpDevelopmentRequestRow).where(
                    McpDevelopmentRequestRow.created_at < cutoff
                )
            )
            existing = self._find(session, client_id, request_id)
            if existing is not None:
                if existing.parameter_fingerprint != fingerprint:
                    raise McpWorkspaceMutationError("request_id_conflict")
                raise _ReplayDetected
            self._require_current_authority(session, client_id, source_id)
            self._require_request_capacity(session, client_id)
            self._require_workspace_capacity(session, client_id)
            timestamp = _to_text(now)
            session.add(
                McpAgentWorkspaceBindingRow(
                    workspace_id=str(workspace.id),
                    client_id=str(client_id),
                    development_source_id=str(source_id),
                    binding_origin="mcp_created",
                    created_at=timestamp,
                )
            )
            session.add(
                McpDevelopmentRequestRow(
                    id=str(uuid4()),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    operation_kind=WORKSPACE_CREATE_OPERATION_KIND,
                    parameter_fingerprint=fingerprint,
                    development_source_id=str(source_id),
                    workspace_id=str(workspace.id),
                    creation_operation_id=str(operation.id),
                    state="allocated",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            session.flush()

        try:
            queued = self._workspaces.queue_creation(
                source_id, transaction_hook=bind, immediate=True
            )
        except _ReplayDetected:
            return self._replay(client_id, request_id, source_id, fingerprint)
        except McpWorkspaceMutationError:
            raise
        except AgentWorkspaceConflictError as error:
            existing = self._load(client_id, request_id)
            if existing is not None:
                return self._replay(client_id, request_id, source_id, fingerprint)
            raise McpWorkspaceMutationError("workspace_quota_exceeded") from error
        except SQLAlchemyError as error:
            existing = self._load(client_id, request_id)
            if existing is not None:
                return self._replay(client_id, request_id, source_id, fingerprint)
            raise McpWorkspaceMutationError("workspace_creation_ambiguous") from error

        # Submission is deliberately after the atomic database commit.  A crash
        # here leaves one queued operation that executor startup will recover.
        with suppress(Exception):
            self._submit_creation(queued.operation_id)
        row = self._load(client_id, request_id)
        if row is None:
            raise McpWorkspaceMutationError("workspace_creation_ambiguous")
        return self._result(row)

    def _replay(
        self,
        client_id: UUID,
        request_id: UUID,
        source_id: UUID,
        fingerprint: str,
    ) -> WorkspaceCreateResult:
        row = self._load(client_id, request_id)
        if row is None:
            raise McpWorkspaceMutationError("workspace_creation_ambiguous")
        if (
            row.development_source_id != str(source_id)
            or row.parameter_fingerprint != fingerprint
        ):
            raise McpWorkspaceMutationError("request_id_conflict")
        return self._result(row)

    def _result(self, row: McpDevelopmentRequestRow) -> WorkspaceCreateResult:
        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(UUID(row.workspace_id))
            operation = AgentWorkspaceCreationOperationRepository(session).get(
                UUID(row.creation_operation_id)
            )
            binding = session.get(McpAgentWorkspaceBindingRow, row.workspace_id)
        if (
            workspace is None
            or operation is None
            or binding is None
            or binding.client_id != row.client_id
            or binding.development_source_id != row.development_source_id
            or binding.binding_origin != "mcp_created"
            or operation.workspace_id != workspace.id
        ):
            raise McpWorkspaceMutationError("workspace_creation_ambiguous")
        return WorkspaceCreateResult(
            request_id=UUID(row.request_id),
            workspace_id=workspace.id,
            source_id=workspace.development_source_id,
            creation_operation_id=operation.id,
            creation_operation_state=operation.state.value,
            lifecycle_state=workspace.state.value,
            created_at=workspace.created_at,
        )

    def _require_current_authority(
        self, session: Session, client_id: UUID, source_id: UUID
    ) -> None:
        client = session.get(McpClientRow, str(client_id))
        grant = session.get(
            McpClientDevelopmentSourceGrantRow,
            {
                "client_id": str(client_id),
                "development_source_id": str(source_id),
            },
        )
        source = DevelopmentSourceRepository(session).get(source_id)
        permission = None
        if client is not None:
            permission = session.get(
                McpPresetPermissionRow,
                {"preset_id": client.preset_id, "permission": "workspace.edit"},
            )
        if (
            client is None
            or not client.enabled
            or permission is None
            or grant is None
            or source is None
        ):
            raise McpWorkspaceMutationError("source_not_found_or_not_authorized")
        if source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY:
            raise McpWorkspaceMutationError("source_not_found_or_not_authorized")

    def _require_request_capacity(self, session: Session, client_id: UUID) -> None:
        client_count = session.scalar(
            select(func.count())
            .select_from(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.client_id == str(client_id))
        )
        global_count = session.scalar(
            select(func.count()).select_from(McpDevelopmentRequestRow)
        )
        if (client_count or 0) >= self._max_per_client or (
            global_count or 0
        ) >= self._max_global:
            raise McpWorkspaceMutationError("workspace_creation_ambiguous")

    @staticmethod
    def _require_workspace_capacity(session: Session, client_id: UUID) -> None:
        present = (
            select(func.count())
            .select_from(McpAgentWorkspaceBindingRow)
            .join(
                AgentWorkspaceRow,
                AgentWorkspaceRow.id == McpAgentWorkspaceBindingRow.workspace_id,
            )
            .where(AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT)
        )
        client_present = session.scalar(
            present.where(McpAgentWorkspaceBindingRow.client_id == str(client_id))
        )
        global_present = session.scalar(present)
        creating = session.scalar(
            present.where(
                McpAgentWorkspaceBindingRow.client_id == str(client_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.CREATING,
            )
        )
        if (
            (client_present or 0) >= MCP_PRESENT_WORKSPACES_PER_CLIENT
            or (global_present or 0) >= MCP_PRESENT_WORKSPACES_GLOBAL
            or (creating or 0) >= MCP_CREATING_WORKSPACES_PER_CLIENT
        ):
            raise McpWorkspaceMutationError("workspace_quota_exceeded")

    def _load(
        self, client_id: UUID, request_id: UUID
    ) -> McpDevelopmentRequestRow | None:
        with self._sessions() as session:
            row = self._find(session, client_id, request_id)
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _find(
        session: Session, client_id: UUID, request_id: UUID
    ) -> McpDevelopmentRequestRow | None:
        return session.scalar(
            select(McpDevelopmentRequestRow).where(
                McpDevelopmentRequestRow.client_id == str(client_id),
                McpDevelopmentRequestRow.request_id == str(request_id),
                McpDevelopmentRequestRow.operation_kind
                == WORKSPACE_CREATE_OPERATION_KIND,
            )
        )

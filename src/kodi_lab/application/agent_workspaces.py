"""Asynchronous creation and bounded read-only use of protected workspaces."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import DevelopmentSourceNotFoundError
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceCreationOperation,
    AgentWorkspaceCreationPhase,
    AgentWorkspaceCreationState,
    AgentWorkspaceState,
    DevelopmentSourceKind,
    GlobalHeavyOperationKind,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceCreationOperationRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
)
from kodi_lab.workspace import (
    AgentWorkspaceStorageError,
    create_workspace_storage,
    inspect_workspace_directory,
    read_workspace_text,
    workspace_baseline_published,
)

WORKSPACE_CREATION_LEASE_DURATION = timedelta(minutes=30)


class AgentWorkspaceError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class AgentWorkspaceNotFoundError(AgentWorkspaceError):
    def __init__(self, workspace_id: UUID) -> None:
        super().__init__("agent_workspace_not_found", "Agent workspace was not found.")
        self.workspace_id = workspace_id


class AgentWorkspaceConflictError(AgentWorkspaceError):
    pass


class AgentWorkspaceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    development_source_id: UUID
    source_logical_name: str = Field(max_length=120)
    source_addon_id: str = Field(max_length=128)
    source_observed_name: str = Field(max_length=256)
    source_observed_version: str = Field(max_length=128)
    state: AgentWorkspaceState
    generation: int = Field(ge=0)
    created_at: datetime
    captured_at: datetime | None


class AgentWorkspaceCreationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    workspace_id: UUID
    development_source_id: UUID
    state: AgentWorkspaceCreationState
    phase: AgentWorkspaceCreationPhase
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None


class AgentWorkspaceTreeEntryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=255)
    entry_type: str
    size: int | None = Field(default=None, ge=0)


class AgentWorkspaceFileRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=512)
    encoding: str = "utf-8"
    content: str


def _workspace_read(workspace: AgentWorkspace) -> AgentWorkspaceRead:
    return AgentWorkspaceRead(
        id=workspace.id,
        development_source_id=workspace.development_source_id,
        source_logical_name=workspace.source_logical_name,
        source_addon_id=workspace.source_addon_id,
        source_observed_name=workspace.source_observed_name,
        source_observed_version=workspace.source_observed_version,
        state=workspace.state,
        generation=workspace.generation,
        created_at=workspace.created_at,
        captured_at=workspace.captured_at,
    )


def _creation_status(
    operation: AgentWorkspaceCreationOperation,
) -> AgentWorkspaceCreationStatus:
    return AgentWorkspaceCreationStatus(
        operation_id=operation.id,
        workspace_id=operation.workspace_id,
        development_source_id=operation.development_source_id,
        state=operation.state,
        phase=operation.phase,
        requested_at=operation.requested_at,
        started_at=operation.started_at,
        completed_at=operation.completed_at,
        failure_code=operation.failure_code,
    )


@dataclass(frozen=True, slots=True)
class AgentWorkspaceCreationLease:
    operation_id: UUID
    workspace_id: UUID
    source_id: UUID
    source_locator: str
    source_addon_id: str
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AgentWorkspaceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        development_root: Path | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._development_root = development_root
        self._clock = clock
        self._uuid_factory = uuid_factory

    def queue_creation(self, source_id: UUID) -> AgentWorkspaceCreationStatus:
        with self._session_factory() as session:
            source = DevelopmentSourceRepository(session).get(source_id)
        if source is None:
            raise DevelopmentSourceNotFoundError(source_id)
        if source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY:
            raise AgentWorkspaceError(
                "agent_workspace_source_unsupported",
                "Only a registered local-directory source is eligible.",
            )
        if self._development_root is None:
            raise AgentWorkspaceError(
                "agent_workspace_source_unavailable",
                "Development sources are unavailable.",
            )
        now = self._clock()
        workspace = AgentWorkspace(
            id=self._uuid_factory(),
            development_source_id=source.id,
            source_addon_id=source.addon_id,
            source_logical_name=source.logical_name,
            source_observed_name=source.observed_name,
            source_observed_version=source.observed_version,
            state=AgentWorkspaceState.CREATING,
            baseline_artifact_id=self._uuid_factory(),
            baseline_manifest_digest=None,
            generation=0,
            current_tree_digest=None,
            capture_started_at=now,
            captured_at=None,
            created_at=now,
            updated_at=now,
        )
        operation = AgentWorkspaceCreationOperation(
            id=self._uuid_factory(),
            workspace_id=workspace.id,
            development_source_id=source.id,
            requested_at=now,
            state=AgentWorkspaceCreationState.QUEUED,
            phase=AgentWorkspaceCreationPhase.QUEUED,
        )
        try:
            with self._session_factory.begin() as session:
                AgentWorkspaceRepository(session).create(workspace)
                AgentWorkspaceCreationOperationRepository(session).create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_CREATION,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise AgentWorkspaceConflictError(
                "agent_workspace_creation_conflict",
                "Another global-heavy operation is active.",
            ) from error
        return _creation_status(operation)

    def acquire(self, operation_id: UUID) -> AgentWorkspaceCreationLease | None:
        with self._session_factory() as session:
            operation = AgentWorkspaceCreationOperationRepository(session).get(
                operation_id
            )
            if (
                operation is None
                or operation.state is not AgentWorkspaceCreationState.QUEUED
            ):
                return None
            source = DevelopmentSourceRepository(session).get(
                operation.development_source_id
            )
        if source is None:
            raise AgentWorkspaceConflictError(
                "agent_workspace_source_missing",
                "Workspace source history is inconsistent.",
            )
        token = secrets.token_urlsafe(32)
        lease = AgentWorkspaceCreationLease(
            operation.id,
            operation.workspace_id,
            source.id,
            source.relative_locator,
            source.addon_id,
            token,
        )
        now = self._clock()
        with self._session_factory.begin() as session:
            acquired = AgentWorkspaceCreationOperationRepository(session).acquire(
                operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=now + WORKSPACE_CREATION_LEASE_DURATION,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation.id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=now + WORKSPACE_CREATION_LEASE_DURATION,
            )
            if not acquired or not heavy:
                raise AgentWorkspaceConflictError(
                    "agent_workspace_authority_lost",
                    "Workspace creation authority was lost.",
                )
        return lease

    def run(self, lease: AgentWorkspaceCreationLease) -> None:
        if self._development_root is None:
            raise AgentWorkspaceError(
                "agent_workspace_source_unavailable",
                "Development sources are unavailable.",
            )

        phase_map = {
            "capturing": AgentWorkspaceCreationPhase.CAPTURING,
            "verifying_source": AgentWorkspaceCreationPhase.VERIFYING_SOURCE,
            "publishing_baseline": AgentWorkspaceCreationPhase.PUBLISHING_BASELINE,
            "creating_tree": AgentWorkspaceCreationPhase.CREATING_TREE,
            "verifying_tree": AgentWorkspaceCreationPhase.VERIFYING_TREE,
        }

        def checkpoint(value: str) -> None:
            with self._session_factory.begin() as session:
                if not AgentWorkspaceCreationOperationRepository(session).update_phase(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    phase=phase_map[value],
                ):
                    raise AgentWorkspaceConflictError(
                        "agent_workspace_authority_lost",
                        "Workspace creation authority was lost.",
                    )

        try:
            result = create_workspace_storage(
                self._data_root,
                lease.workspace_id,
                self._development_root,
                lease.source_locator,
                lease.source_addon_id,
                checkpoint=checkpoint,
            )
            now = self._clock()
            with self._session_factory.begin() as session:
                workspaces = AgentWorkspaceRepository(session)
                operations = AgentWorkspaceCreationOperationRepository(session)
                if (
                    not workspaces.finish_ready(
                        lease.workspace_id,
                        manifest_digest=result.manifest_digest,
                        tree_digest=result.tree_digest,
                        now=now,
                    )
                    or not operations.finish(
                        lease.operation_id,
                        token_digest=lease.token_digest,
                        state=AgentWorkspaceCreationState.SUCCEEDED,
                        now=now,
                        failure_code=None,
                    )
                    or not GlobalHeavyOperationLeaseRepository(session).release(
                        operation_id=lease.operation_id, token_digest=lease.token_digest
                    )
                ):
                    raise AgentWorkspaceConflictError(
                        "agent_workspace_finalization_failed",
                        "Workspace creation finalization was uncertain.",
                    )
        except Exception as error:
            published = workspace_baseline_published(
                self._data_root, lease.workspace_id
            )
            state = (
                AgentWorkspaceCreationState.RECONCILIATION_REQUIRED
                if published
                else AgentWorkspaceCreationState.FAILED
            )
            code = (
                error.code
                if isinstance(
                    error,
                    (
                        DevelopmentTreeError,
                        AgentWorkspaceStorageError,
                        AgentWorkspaceError,
                    ),
                )
                else "agent_workspace_creation_failed"
            )
            now = self._clock()
            with self._session_factory.begin() as session:
                AgentWorkspaceRepository(session).finish_failed(
                    lease.workspace_id, reconciliation_required=published, now=now
                )
                AgentWorkspaceCreationOperationRepository(session).finish(
                    lease.operation_id,
                    token_digest=lease.token_digest,
                    state=state,
                    now=now,
                    failure_code=code[:128],
                )
                GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id, token_digest=lease.token_digest
                )

    def get(self, workspace_id: UUID) -> AgentWorkspaceRead:
        with self._session_factory() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise AgentWorkspaceNotFoundError(workspace_id)
        return _workspace_read(workspace)

    def list_workspaces(self) -> list[AgentWorkspaceRead]:
        with self._session_factory() as session:
            workspaces = AgentWorkspaceRepository(session).list()
        return [_workspace_read(workspace) for workspace in workspaces]

    def creation_status(self, operation_id: UUID) -> AgentWorkspaceCreationStatus:
        with self._session_factory() as session:
            operation = AgentWorkspaceCreationOperationRepository(session).get(
                operation_id
            )
        if operation is None:
            raise AgentWorkspaceError(
                "agent_workspace_creation_not_found",
                "Workspace creation was not found.",
            )
        return _creation_status(operation)

    def _require_ready(self, workspace_id: UUID) -> AgentWorkspace:
        with self._session_factory() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise AgentWorkspaceNotFoundError(workspace_id)
        if workspace.state is not AgentWorkspaceState.READY:
            raise AgentWorkspaceConflictError(
                "agent_workspace_not_ready", "Workspace is not ready for inspection."
            )
        return workspace

    def inspect_directory(
        self, workspace_id: UUID, relative_path: str
    ) -> list[AgentWorkspaceTreeEntryRead]:
        self._require_ready(workspace_id)
        return [
            AgentWorkspaceTreeEntryRead(
                name=item.name, entry_type=item.entry_type, size=item.size
            )
            for item in inspect_workspace_directory(
                self._data_root, workspace_id, relative_path
            )
        ]

    def read_file(
        self, workspace_id: UUID, relative_path: str
    ) -> AgentWorkspaceFileRead:
        self._require_ready(workspace_id)
        return AgentWorkspaceFileRead(
            path=relative_path,
            content=read_workspace_text(self._data_root, workspace_id, relative_path),
        )

    def queued(self) -> list[AgentWorkspaceCreationOperation]:
        with self._session_factory() as session:
            return AgentWorkspaceCreationOperationRepository(session).list_queued()


class AgentWorkspaceCreationExecutor:
    def __init__(self, service: AgentWorkspaceService) -> None:
        self._service = service
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agent-workspace"
        )

    def submit(self, operation_id: UUID) -> None:
        self._pool.submit(self._run, operation_id)

    def start(self) -> None:
        for operation in self._service.queued():
            self.submit(operation.id)

    def _run(self, operation_id: UUID) -> None:
        lease = self._service.acquire(operation_id)
        if lease is not None:
            self._service.run(lease)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)

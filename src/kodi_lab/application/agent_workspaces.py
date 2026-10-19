"""Asynchronous creation and bounded read-only use of protected workspaces."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import DevelopmentSourceNotFoundError
from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    DevelopmentTreeManifest,
    capture_development_tree,
)
from kodi_lab.development.launch import ManifestLaunchEntry, manifest_launch_entries
from kodi_lab.development.sources import (
    CopiedSmokeUnsupportedReason,
    copied_video_plugin_unsupported_reason,
)
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspaceCreationOperation,
    AgentWorkspaceCreationPhase,
    AgentWorkspaceCreationState,
    AgentWorkspaceLifecycleEvent,
    AgentWorkspaceLifecycleEventKind,
    AgentWorkspaceMutationEvent,
    AgentWorkspaceMutationKind,
    AgentWorkspacePromotionOperationState,
    AgentWorkspaceState,
    DevelopmentSourceKind,
    GlobalHeavyOperationKind,
    PromotionVerificationState,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceCreationOperationRepository,
    AgentWorkspaceLifecycleEventRepository,
    AgentWorkspaceMutationEventRepository,
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    PromotionVerificationOperationRepository,
)
from kodi_lab.workspace import (
    AgentWorkspaceReconciliationRequired,
    AgentWorkspaceStorageError,
    create_workspace_storage,
    derive_workspace_diff,
    inspect_workspace_directory,
    mutate_workspace_text,
    read_workspace_text,
    validate_workspace_structure,
    workspace_baseline_published,
    workspace_coordination_lock,
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
    bytes_state: AgentWorkspaceBytesState
    generation: int = Field(ge=0)
    created_at: datetime
    captured_at: datetime | None
    promoted_at: datetime | None
    abandoned_at: datetime | None
    cleaned_at: datetime | None
    promotion_outcome: AgentWorkspacePromotionOperationState | None = None
    verification_outcome: PromotionVerificationState | None = None


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


class AgentWorkspaceMutationRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    path: str = Field(max_length=512)
    operation: Literal["created", "replaced", "patched"]
    generation: int = Field(ge=1)


class AgentWorkspaceActivityRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    generation: int = Field(ge=1)
    operation: Literal["created", "replaced", "patched"]
    path: str = Field(max_length=512)
    occurred_at: datetime


class AgentWorkspaceLifecycleActivityRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    operation: AgentWorkspaceLifecycleEventKind
    cleanup_operation_id: UUID | None
    failure_code: str | None
    occurred_at: datetime


class AgentWorkspaceChangeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=512)
    change_kind: Literal["added", "modified"]
    baseline_size: int | None = Field(default=None, ge=0)
    current_size: int = Field(ge=0)
    before_preview: str | None
    after_preview: str
    before_truncated: bool
    after_truncated: bool


class AgentWorkspaceChangesRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    generation: int = Field(ge=0)
    identity_status: Literal["stable", "identity_changed"]
    changes: list[AgentWorkspaceChangeRead]


class AgentWorkspaceStructuralChangeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(max_length=512)
    change_kind: Literal["added", "modified"]


class AgentWorkspaceStructuralValidationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    development_source_id: UUID
    observed_generation: int = Field(ge=0)
    validated_at: datetime
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    extension_types: tuple[str, ...] = Field(max_length=64)
    launch_entries: tuple[ManifestLaunchEntry, ...] = ()
    fixed_smoke_eligible: bool | None = None
    fixed_smoke_unsupported_reason: CopiedSmokeUnsupportedReason | None = None
    addon_id_classification: Literal["unchanged", "changed"]
    version_classification: Literal["unchanged", "changed"]
    extension_type_classification: Literal["unchanged", "changed"]
    promotable: bool
    promotable_classification: Literal["promotable", "addon_identity_changed"]
    changes: tuple[AgentWorkspaceStructuralChangeRead, ...]


def _workspace_read(
    workspace: AgentWorkspace,
    *,
    promotion_outcome: AgentWorkspacePromotionOperationState | None = None,
    verification_outcome: PromotionVerificationState | None = None,
) -> AgentWorkspaceRead:
    return AgentWorkspaceRead(
        id=workspace.id,
        development_source_id=workspace.development_source_id,
        source_logical_name=workspace.source_logical_name,
        source_addon_id=workspace.source_addon_id,
        source_observed_name=workspace.source_observed_name,
        source_observed_version=workspace.source_observed_version,
        state=workspace.state,
        bytes_state=workspace.bytes_state,
        generation=workspace.generation,
        created_at=workspace.created_at,
        captured_at=workspace.captured_at,
        promoted_at=workspace.promoted_at,
        abandoned_at=workspace.abandoned_at,
        cleaned_at=workspace.cleaned_at,
        promotion_outcome=promotion_outcome,
        verification_outcome=verification_outcome,
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

    def capture_registered_source_manifest(
        self, source_id: UUID
    ) -> DevelopmentTreeManifest:
        """Capture one approved source for Web-only external baselining."""

        if self._development_root is None:
            raise AgentWorkspaceError(
                "agent_workspace_source_unavailable",
                "Development sources are unavailable.",
            )
        with self._session_factory() as session:
            source = DevelopmentSourceRepository(session).get(source_id)
        if (
            source is None
            or source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY
        ):
            raise AgentWorkspaceError(
                "agent_workspace_source_unsupported",
                "Only a registered local-directory source is eligible.",
            )
        try:
            return capture_development_tree(
                self._development_root, source.relative_locator, reject_vcs=True
            )
        except DevelopmentTreeError as error:
            raise AgentWorkspaceError(
                "agent_workspace_source_unavailable",
                "Development source could not be proven safe.",
            ) from error

    def queue_creation(
        self,
        source_id: UUID,
        *,
        transaction_hook: Callable[
            [Session, AgentWorkspace, AgentWorkspaceCreationOperation], None
        ]
        | None = None,
        immediate: bool = False,
    ) -> AgentWorkspaceCreationStatus:
        """Allocate one creation operation, optionally with atomic extra state.

        The hook is intentionally persistence-only.  It runs in the same
        transaction as the workspace, creation operation, and global-heavy
        reservation, before any executor can begin copying bytes.
        """
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
            bytes_state=AgentWorkspaceBytesState.PRESENT,
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
            with self._session_factory() as session:
                if immediate:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                AgentWorkspaceRepository(session).create(workspace)
                AgentWorkspaceCreationOperationRepository(session).create(operation)
                if transaction_hook is not None:
                    transaction_hook(session, workspace, operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_CREATION,
                    operation_id=operation.id,
                )
                session.commit()
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
                        observed_version=result.observed_version,
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
            promotion = AgentWorkspacePromotionOperationRepository(
                session
            ).latest_for_workspace(workspace_id)
            verification = PromotionVerificationOperationRepository(
                session
            ).latest_for_workspace(workspace_id)
        if workspace is None:
            raise AgentWorkspaceNotFoundError(workspace_id)
        return _workspace_read(
            workspace,
            promotion_outcome=None if promotion is None else promotion.state,
            verification_outcome=None if verification is None else verification.state,
        )

    def list_workspaces(self) -> list[AgentWorkspaceRead]:
        with self._session_factory() as session:
            workspaces = AgentWorkspaceRepository(session).list()
            results = []
            for workspace in workspaces:
                promotion = AgentWorkspacePromotionOperationRepository(
                    session
                ).latest_for_workspace(workspace.id)
                verification = PromotionVerificationOperationRepository(
                    session
                ).latest_for_workspace(workspace.id)
                results.append(
                    _workspace_read(
                        workspace,
                        promotion_outcome=None
                        if promotion is None
                        else promotion.state,
                        verification_outcome=None
                        if verification is None
                        else verification.state,
                    )
                )
        return results

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

    def creation_status_for_workspace(
        self, workspace_id: UUID
    ) -> AgentWorkspaceCreationStatus | None:
        with self._session_factory() as session:
            operation = AgentWorkspaceCreationOperationRepository(
                session
            ).get_for_workspace(workspace_id)
        return None if operation is None else _creation_status(operation)

    def _require_ready(self, workspace_id: UUID) -> AgentWorkspace:
        with self._session_factory() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise AgentWorkspaceNotFoundError(workspace_id)
        if workspace.bytes_state is AgentWorkspaceBytesState.CLEANED:
            raise AgentWorkspaceConflictError(
                "workspace_bytes_cleaned",
                "Workspace bytes have been permanently cleaned.",
            )
        if workspace.state is not AgentWorkspaceState.READY:
            raise AgentWorkspaceConflictError(
                "agent_workspace_not_ready", "Workspace is not ready for inspection."
            )
        return workspace

    def abandon(self, workspace_id: UUID) -> AgentWorkspaceRead:
        try:
            with workspace_coordination_lock(
                self._data_root, workspace_id, exclusive=True, blocking=False
            ):
                now = self._clock()
                with self._session_factory.begin() as session:
                    workspaces = AgentWorkspaceRepository(session)
                    workspace = workspaces.get(workspace_id)
                    if workspace is None:
                        raise AgentWorkspaceNotFoundError(workspace_id)
                    if workspace.state is AgentWorkspaceState.ABANDONED:
                        return _workspace_read(workspace)
                    if workspace.state is AgentWorkspaceState.RECONCILIATION_REQUIRED:
                        raise AgentWorkspaceConflictError(
                            "workspace_reconciliation_blocked",
                            "Workspace reconciliation is required before abandonment.",
                        )
                    if workspace.state is not AgentWorkspaceState.READY:
                        raise AgentWorkspaceConflictError(
                            "workspace_abandonment_not_allowed",
                            "Only a ready workspace may be abandoned.",
                        )
                    active_plan = AgentWorkspacePromotionPlanRepository(
                        session
                    ).active_for_workspace(workspace_id, now=now)
                    active_operation = AgentWorkspacePromotionOperationRepository(
                        session
                    ).active_for_workspace(workspace_id)
                    active_verification = PromotionVerificationOperationRepository(
                        session
                    ).blocking_for_workspace(workspace_id)
                    creation = AgentWorkspaceCreationOperationRepository(
                        session
                    ).get_for_workspace(workspace_id)
                    if (
                        active_plan is not None
                        or active_operation is not None
                        or active_verification
                        or creation is None
                        or creation.state
                        in {
                            AgentWorkspaceCreationState.QUEUED,
                            AgentWorkspaceCreationState.RUNNING,
                            AgentWorkspaceCreationState.RECONCILIATION_REQUIRED,
                        }
                    ):
                        raise AgentWorkspaceConflictError(
                            "workspace_promotion_active",
                            "Workspace has active or unresolved work.",
                        )
                    if not workspaces.abandon(workspace_id, now=now):
                        raise AgentWorkspaceConflictError(
                            "workspace_abandonment_conflict",
                            "Workspace changed before abandonment could be published.",
                        )
                    AgentWorkspaceLifecycleEventRepository(session).create(
                        AgentWorkspaceLifecycleEvent(
                            id=self._uuid_factory(),
                            workspace_id=workspace_id,
                            kind=AgentWorkspaceLifecycleEventKind.ABANDONED,
                            occurred_at=now,
                        )
                    )
        except AgentWorkspaceStorageError as error:
            if error.code == "agent_workspace_busy":
                raise AgentWorkspaceConflictError(
                    "workspace_activity_active",
                    "Workspace activity is already in progress.",
                ) from error
            raise
        return self.get(workspace_id)

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

    def _mutation_workspace(
        self, workspace_id: UUID, expected_generation: int
    ) -> AgentWorkspace:
        workspace = self._require_ready(workspace_id)
        with self._session_factory.begin() as session:
            active_plan = AgentWorkspacePromotionPlanRepository(
                session
            ).active_for_workspace(workspace_id, now=self._clock())
            active_operation = AgentWorkspacePromotionOperationRepository(
                session
            ).active_for_workspace(workspace_id)
        if active_plan is not None or active_operation is not None:
            raise AgentWorkspaceConflictError(
                "agent_workspace_promotion_active",
                "Workspace has an active promotion review or execution.",
            )
        if workspace.generation != expected_generation:
            raise AgentWorkspaceConflictError(
                "agent_workspace_generation_conflict",
                "Workspace generation is stale.",
            )
        return workspace

    def _mark_reconciliation(self, workspace: AgentWorkspace) -> None:
        with self._session_factory.begin() as session:
            marked = AgentWorkspaceRepository(session).require_reconciliation(
                workspace.id,
                expected_generation=workspace.generation,
                now=self._clock(),
            )
            if not marked:
                raise AgentWorkspaceConflictError(
                    "agent_workspace_reconciliation_publication_failed",
                    "Workspace reconciliation state could not be published.",
                )

    def _mutate(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        relative_path: str,
        operation: Literal["create", "replace", "patch"],
        content: str,
        expected_text: str | None = None,
    ) -> AgentWorkspaceMutationRead:
        workspace = self._mutation_workspace(workspace_id, expected_generation)
        if workspace.current_tree_digest is None:
            raise AgentWorkspaceConflictError(
                "agent_workspace_not_ready", "Workspace is not ready for mutation."
            )
        try:
            with (
                mutate_workspace_text(
                    self._data_root,
                    workspace_id,
                    relative_path,
                    operation=operation,
                    content=content,
                    expected_tree_digest=workspace.current_tree_digest,
                    expected_text=expected_text,
                ) as result,
                self._session_factory.begin() as session,
            ):
                repository = AgentWorkspaceRepository(session)
                if not repository.publish_mutation(
                    workspace_id,
                    expected_generation=expected_generation,
                    expected_tree_digest=workspace.current_tree_digest,
                    tree_digest=result.tree_digest,
                    now=self._clock(),
                ):
                    raise AgentWorkspaceConflictError(
                        "agent_workspace_generation_conflict",
                        "Workspace generation changed before publication.",
                    )
                AgentWorkspaceMutationEventRepository(session).create(
                    AgentWorkspaceMutationEvent(
                        id=self._uuid_factory(),
                        workspace_id=workspace_id,
                        generation=expected_generation + 1,
                        kind=AgentWorkspaceMutationKind(operation),
                        relative_path=relative_path,
                        occurred_at=self._clock(),
                    )
                )
        except AgentWorkspaceReconciliationRequired:
            self._mark_reconciliation(workspace)
            raise
        except AgentWorkspaceStorageError as error:
            if error.code in {
                "agent_workspace_integrity_changed",
                "agent_workspace_tree_unsafe",
            }:
                with self._session_factory() as session:
                    current = AgentWorkspaceRepository(session).get(workspace_id)
                if current is not None and current.generation != expected_generation:
                    raise AgentWorkspaceConflictError(
                        "agent_workspace_generation_conflict",
                        "Workspace generation is stale.",
                    ) from error
                self._mark_reconciliation(workspace)
            raise
        names: dict[str, Literal["created", "replaced", "patched"]] = {
            "create": "created",
            "replace": "replaced",
            "patch": "patched",
        }
        return AgentWorkspaceMutationRead(
            workspace_id=workspace_id,
            path=relative_path,
            operation=names[operation],
            generation=expected_generation + 1,
        )

    def create_text_file(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        relative_path: str,
        content: str,
    ) -> AgentWorkspaceMutationRead:
        return self._mutate(
            workspace_id,
            expected_generation=expected_generation,
            relative_path=relative_path,
            operation="create",
            content=content,
        )

    def replace_text_file(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        relative_path: str,
        content: str,
    ) -> AgentWorkspaceMutationRead:
        return self._mutate(
            workspace_id,
            expected_generation=expected_generation,
            relative_path=relative_path,
            operation="replace",
            content=content,
        )

    def apply_exact_patch(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        relative_path: str,
        expected_text: str,
        replacement_text: str,
    ) -> AgentWorkspaceMutationRead:
        return self._mutate(
            workspace_id,
            expected_generation=expected_generation,
            relative_path=relative_path,
            operation="patch",
            content=replacement_text,
            expected_text=expected_text,
        )

    def list_changes(self, workspace_id: UUID) -> AgentWorkspaceChangesRead:
        workspace = self._require_ready(workspace_id)
        for attempt in range(2):
            if (
                workspace.baseline_manifest_digest is None
                or workspace.current_tree_digest is None
            ):
                raise AgentWorkspaceConflictError(
                    "agent_workspace_not_ready",
                    "Workspace is not ready for inspection.",
                )
            try:
                result = derive_workspace_diff(
                    self._data_root,
                    workspace_id,
                    expected_baseline_digest=workspace.baseline_manifest_digest,
                    expected_tree_digest=workspace.current_tree_digest,
                    expected_addon_id=workspace.source_addon_id,
                )
                break
            except AgentWorkspaceStorageError:
                with self._session_factory() as session:
                    current = AgentWorkspaceRepository(session).get(workspace_id)
                if (
                    current is not None
                    and current.state is AgentWorkspaceState.READY
                    and (
                        current.generation != workspace.generation
                        or current.current_tree_digest != workspace.current_tree_digest
                    )
                    and attempt == 0
                ):
                    workspace = current
                    continue
                self._mark_reconciliation(workspace)
                raise
        else:
            raise AgentWorkspaceConflictError(
                "agent_workspace_generation_conflict",
                "Workspace changed during change inspection.",
            )
        return AgentWorkspaceChangesRead(
            workspace_id=workspace_id,
            generation=workspace.generation,
            identity_status=(
                "identity_changed" if result.identity_changed else "stable"
            ),
            changes=[
                AgentWorkspaceChangeRead(
                    path=item.path,
                    change_kind=item.change_kind,
                    baseline_size=item.baseline_size,
                    current_size=item.current_size,
                    before_preview=item.before_preview,
                    after_preview=item.after_preview,
                    before_truncated=item.before_truncated,
                    after_truncated=item.after_truncated,
                )
                for item in result.changes
            ],
        )

    def validate_structure(
        self, workspace_id: UUID, *, expected_generation: int
    ) -> AgentWorkspaceStructuralValidationRead:
        """Validate one exact generation without adopting or changing its bytes."""

        workspace = self._mutation_workspace(workspace_id, expected_generation)
        if (
            workspace.baseline_manifest_digest is None
            or workspace.current_tree_digest is None
        ):
            raise AgentWorkspaceConflictError(
                "agent_workspace_not_ready", "Workspace is not ready for validation."
            )
        try:
            result = validate_workspace_structure(
                self._data_root,
                workspace_id,
                expected_baseline_digest=workspace.baseline_manifest_digest,
                expected_tree_digest=workspace.current_tree_digest,
            )
        except AgentWorkspaceStorageError as error:
            if error.code == "agent_workspace_busy":
                raise
            with self._session_factory() as session:
                current = AgentWorkspaceRepository(session).get(workspace_id)
            if current is not None and (
                current.generation != expected_generation
                or current.current_tree_digest != workspace.current_tree_digest
            ):
                raise AgentWorkspaceConflictError(
                    "agent_workspace_generation_conflict",
                    "Workspace generation is stale.",
                ) from error
            self._mark_reconciliation(workspace)
            raise
        with self._session_factory() as session:
            current = AgentWorkspaceRepository(session).get(workspace_id)
        if (
            current is None
            or current.state is not AgentWorkspaceState.READY
            or current.generation != expected_generation
            or current.current_tree_digest != result.tree_digest
        ):
            raise AgentWorkspaceConflictError(
                "agent_workspace_generation_conflict",
                "Workspace generation changed during validation.",
            )
        baseline = result.baseline_addon
        observed = result.current_addon
        if (
            baseline.addon_id != workspace.source_addon_id
            or baseline.observed_version
            != (workspace.active_observed_version or workspace.source_observed_version)
        ):
            self._mark_reconciliation(workspace)
            raise AgentWorkspaceStorageError(
                "agent_workspace_integrity_changed",
                "Workspace baseline identity is inconsistent.",
            )
        addon_changed = observed.addon_id != baseline.addon_id
        version_changed = observed.observed_version != baseline.observed_version
        extension_changed = observed.extension_types != baseline.extension_types
        smoke_reason = copied_video_plugin_unsupported_reason(observed)
        return AgentWorkspaceStructuralValidationRead(
            workspace_id=workspace_id,
            development_source_id=workspace.development_source_id,
            observed_generation=expected_generation,
            validated_at=self._clock(),
            addon_id=observed.addon_id,
            addon_version=observed.observed_version,
            extension_types=observed.extension_types,
            launch_entries=manifest_launch_entries(observed),
            fixed_smoke_eligible=smoke_reason is None,
            fixed_smoke_unsupported_reason=smoke_reason,
            addon_id_classification="changed" if addon_changed else "unchanged",
            version_classification="changed" if version_changed else "unchanged",
            extension_type_classification=(
                "changed" if extension_changed else "unchanged"
            ),
            promotable=not addon_changed,
            promotable_classification=(
                "addon_identity_changed" if addon_changed else "promotable"
            ),
            changes=tuple(
                AgentWorkspaceStructuralChangeRead(
                    path=item.path, change_kind=item.change_kind
                )
                for item in result.changes
            ),
        )

    def list_activity(self, workspace_id: UUID) -> list[AgentWorkspaceActivityRead]:
        self.get(workspace_id)
        with self._session_factory() as session:
            events = AgentWorkspaceMutationEventRepository(session).list_for_workspace(
                workspace_id
            )
        names: dict[
            AgentWorkspaceMutationKind, Literal["created", "replaced", "patched"]
        ] = {
            AgentWorkspaceMutationKind.CREATE: "created",
            AgentWorkspaceMutationKind.REPLACE: "replaced",
            AgentWorkspaceMutationKind.PATCH: "patched",
        }
        return [
            AgentWorkspaceActivityRead(
                event_id=event.id,
                generation=event.generation,
                operation=names[event.kind],
                path=event.relative_path,
                occurred_at=event.occurred_at,
            )
            for event in events
        ]

    def list_lifecycle_activity(
        self, workspace_id: UUID
    ) -> list[AgentWorkspaceLifecycleActivityRead]:
        self.get(workspace_id)
        with self._session_factory() as session:
            events = AgentWorkspaceLifecycleEventRepository(session).list_for_workspace(
                workspace_id
            )
        return [
            AgentWorkspaceLifecycleActivityRead(
                event_id=event.id,
                operation=event.kind,
                cleanup_operation_id=event.cleanup_operation_id,
                failure_code=event.failure_code,
                occurred_at=event.occurred_at,
            )
            for event in events
        ]

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

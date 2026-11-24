"""Repositories for external targets, workspaces and reviewed runtimes."""

import json
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspaceCleanupOperation,
    AgentWorkspaceCleanupPhase,
    AgentWorkspaceCleanupState,
    AgentWorkspaceCreationOperation,
    AgentWorkspaceCreationPhase,
    AgentWorkspaceCreationState,
    AgentWorkspaceLifecycleEvent,
    AgentWorkspaceMutationEvent,
    AgentWorkspaceState,
    DevelopmentSource,
    GlobalHeavyOperationKind,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeRecipeInstallationState,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationEvidence,
    Target,
)
from kodi_lab.storage.models import (
    AgentWorkspaceCleanupOperationRow,
    AgentWorkspaceCreationOperationRow,
    AgentWorkspaceLifecycleEventRow,
    AgentWorkspaceMutationEventRow,
    AgentWorkspaceRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRecipeInstallationRow,
    RuntimeRevisionRow,
    RuntimeValidationEvidenceRow,
    TargetRow,
)


class EntityNotFoundError(LookupError):
    """Raised when an update target no longer exists."""


def _target_from_row(row: TargetRow) -> Target:
    return Target(
        id=UUID(row.id),
        name=row.name,
        target_type=row.target_type,
        kodi_version=row.kodi_version,
        availability=row.availability,
        health=row.health,
        connection_state=row.connection_state,
    )


def _development_source_from_row(row: DevelopmentSourceRow) -> DevelopmentSource:
    extension_types = json.loads(row.extension_types_json)
    if not isinstance(extension_types, list) or not all(
        isinstance(value, str) for value in extension_types
    ):
        raise ValueError("stored development-source extension metadata is invalid")
    return DevelopmentSource(
        id=UUID(row.id),
        logical_name=row.logical_name,
        source_kind=row.source_kind,
        relative_locator=row.relative_locator,
        addon_id=row.addon_id,
        observed_name=row.observed_name,
        observed_version=row.observed_version,
        extension_types=tuple(extension_types),
        registered_at=_timestamp_from_text(row.registered_at),
        validated_at=_timestamp_from_text(row.validated_at),
    )


def _agent_workspace_from_row(row: AgentWorkspaceRow) -> AgentWorkspace:
    return AgentWorkspace(
        id=UUID(row.id),
        development_source_id=UUID(row.development_source_id),
        source_addon_id=row.source_addon_id,
        source_logical_name=row.source_logical_name,
        source_observed_name=row.source_observed_name,
        source_observed_version=row.source_observed_version,
        state=row.state,
        bytes_state=row.bytes_state,
        baseline_artifact_id=UUID(row.baseline_artifact_id),
        baseline_manifest_digest=row.baseline_manifest_digest,
        creation_baseline_digest=row.creation_baseline_digest,
        active_baseline_artifact_id=(
            None
            if row.active_baseline_artifact_id is None
            else UUID(row.active_baseline_artifact_id)
        ),
        active_capture_id=(
            None if row.active_capture_id is None else UUID(row.active_capture_id)
        ),
        active_observed_version=row.active_observed_version,
        generation=row.generation,
        current_tree_digest=row.current_tree_digest,
        capture_started_at=_timestamp_from_text(row.capture_started_at),
        captured_at=None
        if row.captured_at is None
        else _timestamp_from_text(row.captured_at),
        created_at=_timestamp_from_text(row.created_at),
        updated_at=_timestamp_from_text(row.updated_at),
        promoted_at=None
        if row.promoted_at is None
        else _timestamp_from_text(row.promoted_at),
        abandoned_at=None
        if row.abandoned_at is None
        else _timestamp_from_text(row.abandoned_at),
        cleaned_at=None
        if row.cleaned_at is None
        else _timestamp_from_text(row.cleaned_at),
    )


def _agent_workspace_cleanup_from_row(
    row: AgentWorkspaceCleanupOperationRow,
) -> AgentWorkspaceCleanupOperation:
    return AgentWorkspaceCleanupOperation(
        id=UUID(row.id),
        workspace_id=UUID(row.workspace_id),
        state=row.state,
        phase=row.phase,
        requested_at=_timestamp_from_text(row.requested_at),
        deletion_started=row.deletion_started,
        expected_device=row.expected_device,
        expected_inode=row.expected_inode,
        started_at=None
        if row.started_at is None
        else _timestamp_from_text(row.started_at),
        completed_at=None
        if row.completed_at is None
        else _timestamp_from_text(row.completed_at),
        failure_code=row.failure_code,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=None
        if row.lease_expires_at is None
        else _timestamp_from_text(row.lease_expires_at),
    )


def _agent_workspace_lifecycle_from_row(
    row: AgentWorkspaceLifecycleEventRow,
) -> AgentWorkspaceLifecycleEvent:
    return AgentWorkspaceLifecycleEvent(
        id=UUID(row.id),
        workspace_id=UUID(row.workspace_id),
        kind=row.kind,
        occurred_at=_timestamp_from_text(row.occurred_at),
        cleanup_operation_id=None
        if row.cleanup_operation_id is None
        else UUID(row.cleanup_operation_id),
        failure_code=row.failure_code,
    )


def _agent_workspace_creation_from_row(
    row: AgentWorkspaceCreationOperationRow,
) -> AgentWorkspaceCreationOperation:
    return AgentWorkspaceCreationOperation(
        id=UUID(row.id),
        workspace_id=UUID(row.workspace_id),
        development_source_id=UUID(row.development_source_id),
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        started_at=None
        if row.started_at is None
        else _timestamp_from_text(row.started_at),
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        failure_code=row.failure_code,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=(
            None
            if row.lease_expires_at is None
            else _timestamp_from_text(row.lease_expires_at)
        ),
    )


def _agent_workspace_mutation_from_row(
    row: AgentWorkspaceMutationEventRow,
) -> AgentWorkspaceMutationEvent:
    return AgentWorkspaceMutationEvent(
        id=UUID(row.id),
        workspace_id=UUID(row.workspace_id),
        generation=row.generation,
        kind=row.kind,
        relative_path=row.relative_path,
        occurred_at=_timestamp_from_text(row.occurred_at),
    )


def _timestamp_to_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _definition_from_row(row: RuntimeDefinitionRow) -> RuntimeDefinition:
    return RuntimeDefinition(
        id=UUID(row.id),
        slug=row.slug,
        kodi_version=row.kodi_version,
        support_tier=RuntimeSupportTier(row.support_tier),
    )


def _revision_from_row(row: RuntimeRevisionRow) -> RuntimeRevision:
    return RuntimeRevision(
        id=UUID(row.id),
        runtime_definition_id=UUID(row.runtime_definition_id),
        revision_number=row.revision_number,
        source_kind=row.source_kind,
        source_reference=row.source_reference,
        content_digest=row.content_digest,
        manifest_reference=row.manifest_reference,
        manifest_digest=row.manifest_digest,
        built_at=_timestamp_from_text(row.built_at),
        build_log_reference=row.build_log_reference,
        approval_state=row.approval_state,
        approved_at=(
            None if row.approved_at is None else _timestamp_from_text(row.approved_at)
        ),
        approval_note=row.approval_note,
        requires_trusted_build_confirmation=row.requires_trusted_build_confirmation,
        trusted_build_confirmed_at=(
            None
            if row.trusted_build_confirmed_at is None
            else _timestamp_from_text(row.trusted_build_confirmed_at)
        ),
    )


class DevelopmentSourceRepository:
    """Create/read-only persistence for global development-source registrations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, source: DevelopmentSource) -> None:
        self._session.add(
            DevelopmentSourceRow(
                id=str(source.id),
                logical_name=source.logical_name,
                source_kind=source.source_kind,
                relative_locator=source.relative_locator,
                addon_id=source.addon_id,
                observed_name=source.observed_name,
                observed_version=source.observed_version,
                extension_types_json=json.dumps(
                    list(source.extension_types), separators=(",", ":")
                ),
                registered_at=_timestamp_to_text(source.registered_at),
                validated_at=_timestamp_to_text(source.validated_at),
            )
        )
        self._session.flush()

    def get(self, source_id: UUID) -> DevelopmentSource | None:
        row = self._session.get(DevelopmentSourceRow, str(source_id))
        return None if row is None else _development_source_from_row(row)

    def get_by_logical_name(self, logical_name: str) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.logical_name == logical_name
            )
        )
        return None if row is None else _development_source_from_row(row)

    def get_by_relative_locator(
        self, relative_locator: str
    ) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.relative_locator == relative_locator
            )
        )
        return None if row is None else _development_source_from_row(row)

    def get_by_addon_id(self, addon_id: str) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.addon_id == addon_id
            )
        )
        return None if row is None else _development_source_from_row(row)

    def list(self) -> list[DevelopmentSource]:
        rows = self._session.scalars(
            select(DevelopmentSourceRow).order_by(
                DevelopmentSourceRow.logical_name, DevelopmentSourceRow.id
            )
        )
        return [_development_source_from_row(row) for row in rows]


class AgentWorkspaceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, workspace: AgentWorkspace) -> None:
        self._session.add(
            AgentWorkspaceRow(
                id=str(workspace.id),
                development_source_id=str(workspace.development_source_id),
                source_addon_id=workspace.source_addon_id,
                source_logical_name=workspace.source_logical_name,
                source_observed_name=workspace.source_observed_name,
                source_observed_version=workspace.source_observed_version,
                state=workspace.state,
                bytes_state=workspace.bytes_state,
                baseline_artifact_id=str(workspace.baseline_artifact_id),
                baseline_manifest_digest=workspace.baseline_manifest_digest,
                creation_baseline_digest=workspace.creation_baseline_digest,
                active_baseline_artifact_id=(
                    None
                    if workspace.active_baseline_artifact_id is None
                    else str(workspace.active_baseline_artifact_id)
                ),
                active_capture_id=(
                    None
                    if workspace.active_capture_id is None
                    else str(workspace.active_capture_id)
                ),
                active_observed_version=workspace.active_observed_version,
                generation=workspace.generation,
                current_tree_digest=workspace.current_tree_digest,
                capture_started_at=_timestamp_to_text(workspace.capture_started_at),
                captured_at=(
                    None
                    if workspace.captured_at is None
                    else _timestamp_to_text(workspace.captured_at)
                ),
                created_at=_timestamp_to_text(workspace.created_at),
                updated_at=_timestamp_to_text(workspace.updated_at),
                promoted_at=None,
                abandoned_at=None,
                cleaned_at=None,
            )
        )
        self._session.flush()

    def get(self, workspace_id: UUID) -> AgentWorkspace | None:
        row = self._session.get(AgentWorkspaceRow, str(workspace_id))
        return None if row is None else _agent_workspace_from_row(row)

    def list(self) -> list[AgentWorkspace]:
        rows = self._session.scalars(
            select(AgentWorkspaceRow).order_by(
                AgentWorkspaceRow.created_at.desc(), AgentWorkspaceRow.id
            )
        )
        return [_agent_workspace_from_row(row) for row in rows]

    def finish_ready(
        self,
        workspace_id: UUID,
        *,
        manifest_digest: str,
        tree_digest: str,
        observed_version: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.CREATING,
            )
            .values(
                state=AgentWorkspaceState.READY,
                baseline_manifest_digest=manifest_digest,
                creation_baseline_digest=manifest_digest,
                active_baseline_artifact_id=AgentWorkspaceRow.baseline_artifact_id,
                active_observed_version=observed_version,
                current_tree_digest=tree_digest,
                captured_at=_timestamp_to_text(now),
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish_failed(
        self, workspace_id: UUID, *, reconciliation_required: bool, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.CREATING,
            )
            .values(
                state=(
                    AgentWorkspaceState.RECONCILIATION_REQUIRED
                    if reconciliation_required
                    else AgentWorkspaceState.ABANDONED
                ),
                abandoned_at=(
                    None if reconciliation_required else _timestamp_to_text(now)
                ),
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def publish_mutation(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        expected_tree_digest: str,
        tree_digest: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                AgentWorkspaceRow.generation == expected_generation,
                AgentWorkspaceRow.current_tree_digest == expected_tree_digest,
            )
            .values(
                generation=expected_generation + 1,
                current_tree_digest=tree_digest,
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def require_reconciliation(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                AgentWorkspaceRow.generation == expected_generation,
            )
            .values(
                state=AgentWorkspaceState.RECONCILIATION_REQUIRED,
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_promoted(
        self,
        workspace_id: UUID,
        *,
        expected_generation: int,
        expected_tree_digest: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                AgentWorkspaceRow.generation == expected_generation,
                AgentWorkspaceRow.current_tree_digest == expected_tree_digest,
            )
            .values(
                state=AgentWorkspaceState.PROMOTED,
                promoted_at=_timestamp_to_text(now),
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def abandon(self, workspace_id: UUID, *, now: datetime) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT,
            )
            .values(
                state=AgentWorkspaceState.ABANDONED,
                abandoned_at=_timestamp_to_text(now),
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_bytes_cleaned(self, workspace_id: UUID, *, now: datetime) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT,
                AgentWorkspaceRow.state.in_(
                    (AgentWorkspaceState.ABANDONED, AgentWorkspaceState.PROMOTED)
                ),
            )
            .values(
                bytes_state=AgentWorkspaceBytesState.CLEANED,
                cleaned_at=_timestamp_to_text(now),
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_cleanup_reconciliation(self, workspace_id: UUID, *, now: datetime) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceRow)
            .where(
                AgentWorkspaceRow.id == str(workspace_id),
                AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT,
                AgentWorkspaceRow.state.in_(
                    (AgentWorkspaceState.ABANDONED, AgentWorkspaceState.PROMOTED)
                ),
            )
            .values(
                state=AgentWorkspaceState.RECONCILIATION_REQUIRED,
                updated_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1


class AgentWorkspaceCleanupOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: AgentWorkspaceCleanupOperation) -> None:
        self._session.add(
            AgentWorkspaceCleanupOperationRow(
                id=str(operation.id),
                workspace_id=str(operation.workspace_id),
                state=operation.state,
                phase=operation.phase,
                requested_at=_timestamp_to_text(operation.requested_at),
                deletion_started=operation.deletion_started,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> AgentWorkspaceCleanupOperation | None:
        row = self._session.get(AgentWorkspaceCleanupOperationRow, str(operation_id))
        return None if row is None else _agent_workspace_cleanup_from_row(row)

    def get_for_workspace(
        self, workspace_id: UUID
    ) -> AgentWorkspaceCleanupOperation | None:
        row = self._session.scalar(
            select(AgentWorkspaceCleanupOperationRow)
            .where(AgentWorkspaceCleanupOperationRow.workspace_id == str(workspace_id))
            .order_by(
                AgentWorkspaceCleanupOperationRow.requested_at.desc(),
                AgentWorkspaceCleanupOperationRow.id.desc(),
            )
            .limit(1)
        )
        return None if row is None else _agent_workspace_cleanup_from_row(row)

    def list_queued(self) -> list[AgentWorkspaceCleanupOperation]:
        rows = self._session.scalars(
            select(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.QUEUED
            )
            .order_by(AgentWorkspaceCleanupOperationRow.requested_at)
        )
        return [_agent_workspace_cleanup_from_row(row) for row in rows]

    def list_active(self) -> list[AgentWorkspaceCleanupOperation]:
        rows = self._session.scalars(
            select(AgentWorkspaceCleanupOperationRow).where(
                AgentWorkspaceCleanupOperationRow.state.in_(
                    (
                        AgentWorkspaceCleanupState.QUEUED,
                        AgentWorkspaceCleanupState.RUNNING,
                    )
                )
            )
        )
        return [_agent_workspace_cleanup_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.id == str(operation_id),
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.QUEUED,
            )
            .values(
                state=AgentWorkspaceCleanupState.RUNNING,
                phase=AgentWorkspaceCleanupPhase.VALIDATING,
                started_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def start_deletion(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        device: int,
        inode: int,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.id == str(operation_id),
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.RUNNING,
                AgentWorkspaceCleanupOperationRow.lease_token_digest == token_digest,
                AgentWorkspaceCleanupOperationRow.deletion_started.is_(False),
            )
            .values(
                phase=AgentWorkspaceCleanupPhase.DELETING,
                deletion_started=True,
                expected_device=device,
                expected_inode=inode,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: AgentWorkspaceCleanupPhase,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.id == str(operation_id),
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.RUNNING,
                AgentWorkspaceCleanupOperationRow.lease_token_digest == token_digest,
            )
            .values(phase=phase)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: AgentWorkspaceCleanupState,
        now: datetime,
        failure_code: str | None,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.id == str(operation_id),
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.RUNNING,
                AgentWorkspaceCleanupOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=state,
                phase=AgentWorkspaceCleanupPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_code=failure_code,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def recover_stale(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        resume: bool,
    ) -> bool:
        values: dict[str, object] = {
            "lease_token_digest": None,
            "lease_expires_at": None,
        }
        if resume:
            values.update(
                state=AgentWorkspaceCleanupState.QUEUED,
                phase=AgentWorkspaceCleanupPhase.QUEUED,
                started_at=None,
            )
        else:
            values.update(
                state=AgentWorkspaceCleanupState.FAILED,
                phase=AgentWorkspaceCleanupPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_code="workspace_cleanup_interrupted_before_delete",
            )
        result = self._session.execute(
            update(AgentWorkspaceCleanupOperationRow)
            .where(
                AgentWorkspaceCleanupOperationRow.id == str(operation_id),
                AgentWorkspaceCleanupOperationRow.state
                == AgentWorkspaceCleanupState.RUNNING,
                AgentWorkspaceCleanupOperationRow.lease_token_digest == token_digest,
                AgentWorkspaceCleanupOperationRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
            .values(**values)
        )
        return int(getattr(result, "rowcount", 0)) == 1


class AgentWorkspaceLifecycleEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: AgentWorkspaceLifecycleEvent) -> None:
        self._session.add(
            AgentWorkspaceLifecycleEventRow(
                id=str(event.id),
                workspace_id=str(event.workspace_id),
                kind=event.kind,
                occurred_at=_timestamp_to_text(event.occurred_at),
                cleanup_operation_id=None
                if event.cleanup_operation_id is None
                else str(event.cleanup_operation_id),
                failure_code=event.failure_code,
            )
        )
        self._session.flush()

    def list_for_workspace(
        self, workspace_id: UUID
    ) -> list[AgentWorkspaceLifecycleEvent]:
        rows = self._session.scalars(
            select(AgentWorkspaceLifecycleEventRow)
            .where(AgentWorkspaceLifecycleEventRow.workspace_id == str(workspace_id))
            .order_by(
                AgentWorkspaceLifecycleEventRow.occurred_at,
                AgentWorkspaceLifecycleEventRow.id,
            )
        )
        return [_agent_workspace_lifecycle_from_row(row) for row in rows]


class AgentWorkspaceMutationEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: AgentWorkspaceMutationEvent) -> None:
        self._session.add(
            AgentWorkspaceMutationEventRow(
                id=str(event.id),
                workspace_id=str(event.workspace_id),
                generation=event.generation,
                kind=event.kind,
                relative_path=event.relative_path,
                occurred_at=_timestamp_to_text(event.occurred_at),
            )
        )
        self._session.flush()

    def list_for_workspace(
        self, workspace_id: UUID
    ) -> list[AgentWorkspaceMutationEvent]:
        rows = self._session.scalars(
            select(AgentWorkspaceMutationEventRow)
            .where(AgentWorkspaceMutationEventRow.workspace_id == str(workspace_id))
            .order_by(
                AgentWorkspaceMutationEventRow.generation.desc(),
                AgentWorkspaceMutationEventRow.id.desc(),
            )
        )
        return [_agent_workspace_mutation_from_row(row) for row in rows]


class AgentWorkspaceCreationOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: AgentWorkspaceCreationOperation) -> None:
        self._session.add(
            AgentWorkspaceCreationOperationRow(
                id=str(operation.id),
                workspace_id=str(operation.workspace_id),
                development_source_id=str(operation.development_source_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> AgentWorkspaceCreationOperation | None:
        row = self._session.get(AgentWorkspaceCreationOperationRow, str(operation_id))
        return None if row is None else _agent_workspace_creation_from_row(row)

    def get_for_workspace(
        self, workspace_id: UUID
    ) -> AgentWorkspaceCreationOperation | None:
        row = self._session.scalar(
            select(AgentWorkspaceCreationOperationRow).where(
                AgentWorkspaceCreationOperationRow.workspace_id == str(workspace_id)
            )
        )
        return None if row is None else _agent_workspace_creation_from_row(row)

    def list_queued(self) -> list[AgentWorkspaceCreationOperation]:
        rows = self._session.scalars(
            select(AgentWorkspaceCreationOperationRow)
            .where(
                AgentWorkspaceCreationOperationRow.state
                == AgentWorkspaceCreationState.QUEUED
            )
            .order_by(AgentWorkspaceCreationOperationRow.requested_at)
        )
        return [_agent_workspace_creation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCreationOperationRow)
            .where(
                AgentWorkspaceCreationOperationRow.id == str(operation_id),
                AgentWorkspaceCreationOperationRow.state
                == AgentWorkspaceCreationState.QUEUED,
                AgentWorkspaceCreationOperationRow.lease_token_digest.is_(None),
            )
            .values(
                state=AgentWorkspaceCreationState.RUNNING,
                phase=AgentWorkspaceCreationPhase.CAPTURING,
                started_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: AgentWorkspaceCreationPhase,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCreationOperationRow)
            .where(
                AgentWorkspaceCreationOperationRow.id == str(operation_id),
                AgentWorkspaceCreationOperationRow.state
                == AgentWorkspaceCreationState.RUNNING,
                AgentWorkspaceCreationOperationRow.lease_token_digest == token_digest,
            )
            .values(phase=phase)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: AgentWorkspaceCreationState,
        now: datetime,
        failure_code: str | None,
    ) -> bool:
        result = self._session.execute(
            update(AgentWorkspaceCreationOperationRow)
            .where(
                AgentWorkspaceCreationOperationRow.id == str(operation_id),
                AgentWorkspaceCreationOperationRow.state
                == AgentWorkspaceCreationState.RUNNING,
                AgentWorkspaceCreationOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=state,
                phase=AgentWorkspaceCreationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_code=failure_code,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1


class TargetRepository:
    """Persistence operations for normalized targets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, target: Target) -> None:
        self._session.add(
            TargetRow(
                id=str(target.id),
                name=target.name,
                target_type=target.target_type,
                kodi_version=target.kodi_version,
                availability=target.availability,
                health=target.health,
                connection_state=target.connection_state,
            )
        )
        self._session.flush()

    def get(self, target_id: UUID) -> Target | None:
        row = self._session.get(TargetRow, str(target_id))
        return None if row is None else _target_from_row(row)

    def list(self) -> list[Target]:
        rows = self._session.scalars(
            select(TargetRow).order_by(TargetRow.name, TargetRow.id)
        )
        return [_target_from_row(row) for row in rows]

    def update(self, target: Target) -> None:
        row = self._session.get(TargetRow, str(target.id))
        if row is None:
            raise EntityNotFoundError(f"target {target.id} does not exist")
        if row.target_type is not target.target_type:
            raise ValueError("target class is immutable; create a new target UUID")
        row.name = target.name
        row.kodi_version = target.kodi_version
        row.availability = target.availability
        row.health = target.health
        row.connection_state = target.connection_state
        self._session.flush()

    def delete(self, target_id: UUID) -> bool:
        row = self._session.get(TargetRow, str(target_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True


class RuntimeDefinitionRepository:
    """Persistence operations for logical runtime definitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, definition: RuntimeDefinition) -> None:
        self._session.add(
            RuntimeDefinitionRow(
                id=str(definition.id),
                slug=definition.slug,
                kodi_version=definition.kodi_version,
                support_tier=definition.support_tier.value,
            )
        )
        self._session.flush()

    def get(self, definition_id: UUID) -> RuntimeDefinition | None:
        row = self._session.get(RuntimeDefinitionRow, str(definition_id))
        return None if row is None else _definition_from_row(row)

    def get_by_slug(self, slug: str) -> RuntimeDefinition | None:
        row = self._session.scalar(
            select(RuntimeDefinitionRow).where(RuntimeDefinitionRow.slug == slug)
        )
        return None if row is None else _definition_from_row(row)

    def list(self) -> list[RuntimeDefinition]:
        rows = self._session.scalars(
            select(RuntimeDefinitionRow).order_by(
                RuntimeDefinitionRow.slug, RuntimeDefinitionRow.id
            )
        )
        return [_definition_from_row(row) for row in rows]

    def transition_support_tier(
        self, definition_id: UUID, support_tier: RuntimeSupportTier
    ) -> RuntimeDefinition:
        row = self._session.get(RuntimeDefinitionRow, str(definition_id))
        if row is None:
            raise EntityNotFoundError(
                f"runtime definition {definition_id} does not exist"
            )
        row.support_tier = support_tier.value
        self._session.flush()
        return _definition_from_row(row)


class RuntimeRevisionRepository:
    """Creation, lookup, and explicit lifecycle changes for immutable revisions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, revision: RuntimeRevision) -> None:
        self._session.add(
            RuntimeRevisionRow(
                id=str(revision.id),
                runtime_definition_id=str(revision.runtime_definition_id),
                revision_number=revision.revision_number,
                source_kind=revision.source_kind,
                source_reference=revision.source_reference,
                content_digest=revision.content_digest,
                manifest_reference=revision.manifest_reference,
                manifest_digest=revision.manifest_digest,
                built_at=_timestamp_to_text(revision.built_at),
                build_log_reference=revision.build_log_reference,
                approval_state=revision.approval_state,
                approved_at=(
                    None
                    if revision.approved_at is None
                    else _timestamp_to_text(revision.approved_at)
                ),
                approval_note=revision.approval_note,
                requires_trusted_build_confirmation=(
                    revision.requires_trusted_build_confirmation
                ),
                trusted_build_confirmed_at=(
                    None
                    if revision.trusted_build_confirmed_at is None
                    else _timestamp_to_text(revision.trusted_build_confirmed_at)
                ),
            )
        )
        self._session.flush()

    def get(self, revision_id: UUID) -> RuntimeRevision | None:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        return None if row is None else _revision_from_row(row)

    def list_for_definition(self, definition_id: UUID) -> list[RuntimeRevision]:
        rows = self._session.scalars(
            select(RuntimeRevisionRow)
            .where(RuntimeRevisionRow.runtime_definition_id == str(definition_id))
            .order_by(RuntimeRevisionRow.revision_number, RuntimeRevisionRow.id)
        )
        return [_revision_from_row(row) for row in rows]

    def newest_approved(self, definition_id: UUID) -> RuntimeRevision | None:
        row = self._session.scalar(
            select(RuntimeRevisionRow)
            .where(
                RuntimeRevisionRow.runtime_definition_id == str(definition_id),
                RuntimeRevisionRow.approval_state == "approved",
            )
            .order_by(RuntimeRevisionRow.revision_number.desc())
            .limit(1)
        )
        return None if row is None else _revision_from_row(row)

    def list_for_definition_descending(
        self, definition_id: UUID
    ) -> list[RuntimeRevision]:
        rows = self._session.scalars(
            select(RuntimeRevisionRow)
            .where(RuntimeRevisionRow.runtime_definition_id == str(definition_id))
            .order_by(RuntimeRevisionRow.revision_number.desc(), RuntimeRevisionRow.id)
        )
        return [_revision_from_row(row) for row in rows]

    def approve(
        self, revision_id: UUID, *, approved_at: datetime, note: str | None = None
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.approve(approved_at=approved_at, note=note)
        normalized_approved_at = updated.approved_at
        assert normalized_approved_at is not None
        row.approval_state = updated.approval_state
        row.approved_at = _timestamp_to_text(normalized_approved_at)
        row.approval_note = updated.approval_note
        self._session.flush()
        return updated

    def revoke_approval(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_approval()
        row.approval_state = updated.approval_state
        row.approved_at = None
        row.approval_note = None
        self._session.flush()
        return updated

    def confirm_trusted_build(
        self, revision_id: UUID, *, confirmed_at: datetime
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.confirm_trusted_build(confirmed_at=confirmed_at)
        confirmed = updated.trusted_build_confirmed_at
        assert confirmed is not None
        row.trusted_build_confirmed_at = _timestamp_to_text(confirmed)
        self._session.flush()
        return updated

    def revoke_trusted_build_confirmation(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_trusted_build_confirmation()
        row.trusted_build_confirmed_at = None
        self._session.flush()
        return updated

    def _require_revision(
        self, revision_id: UUID
    ) -> tuple[RuntimeRevision, RuntimeRevisionRow]:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        if row is None:
            raise EntityNotFoundError(f"runtime revision {revision_id} does not exist")
        return _revision_from_row(row), row


class RuntimeContainerArtifactRepository:
    """Create/read-only persistence for immutable container artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, artifact: RuntimeContainerArtifact) -> None:
        self._session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(artifact.runtime_revision_id),
                image_id=artifact.image_id,
            )
        )
        self._session.flush()

    def get(self, runtime_revision_id: UUID) -> RuntimeContainerArtifact | None:
        row = self._session.get(RuntimeContainerArtifactRow, str(runtime_revision_id))
        if row is None:
            return None
        return RuntimeContainerArtifact(
            runtime_revision_id=UUID(row.runtime_revision_id),
            image_id=row.image_id,
        )


class RuntimeExecutionProfileRepository:
    """Create/read-only persistence for immutable execution profiles."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, profile: RuntimeExecutionProfile) -> None:
        self._session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(profile.runtime_revision_id),
                runtime_kind=profile.runtime_kind,
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
                python_bytecode_policy=profile.python_bytecode_policy,
                expected_application_name=profile.expected_application_name,
                expected_kodi_version=profile.expected_kodi_version,
                expected_version_tag=profile.expected_version_tag,
                expected_prerelease_ordinal=profile.expected_prerelease_ordinal,
                jsonrpc_port=profile.jsonrpc_port,
                jsonrpc_path=profile.jsonrpc_path,
                process_observation=profile.process_observation,
                readiness_probe=profile.readiness_probe,
                readiness_timeout_seconds=profile.readiness_timeout_seconds,
                poll_interval_seconds=profile.poll_interval_seconds,
                http_timeout_seconds=profile.http_timeout_seconds,
            )
        )
        self._session.flush()

    def get(self, runtime_revision_id: UUID) -> RuntimeExecutionProfile | None:
        row = self._session.get(RuntimeExecutionProfileRow, str(runtime_revision_id))
        if row is None:
            return None
        return RuntimeExecutionProfile(
            runtime_revision_id=UUID(row.runtime_revision_id),
            runtime_kind=row.runtime_kind,
            runtime_uid=row.runtime_uid,
            runtime_gid=row.runtime_gid,
            python_bytecode_policy=row.python_bytecode_policy,
            expected_application_name=row.expected_application_name,
            expected_kodi_version=row.expected_kodi_version,
            expected_version_tag=row.expected_version_tag,
            expected_prerelease_ordinal=row.expected_prerelease_ordinal,
            jsonrpc_port=row.jsonrpc_port,
            jsonrpc_path=row.jsonrpc_path,
            process_observation=row.process_observation,
            readiness_probe=row.readiness_probe,
            readiness_timeout_seconds=row.readiness_timeout_seconds,
            poll_interval_seconds=row.poll_interval_seconds,
            http_timeout_seconds=row.http_timeout_seconds,
        )


class RuntimeValidationEvidenceRepository:
    """Append-only persistence and effective evidence retrieval."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, evidence: RuntimeValidationEvidence) -> None:
        self._session.add(
            RuntimeValidationEvidenceRow(
                id=str(evidence.id),
                runtime_revision_id=str(evidence.runtime_revision_id),
                contract_version=evidence.contract_version,
                result=evidence.result,
                started_at=_timestamp_to_text(evidence.started_at),
                completed_at=_timestamp_to_text(evidence.completed_at),
                evidence_reference=evidence.evidence_reference,
                summary=evidence.summary,
                recorded_at=_timestamp_to_text(evidence.recorded_at),
            )
        )
        self._session.flush()

    def list_for_revision(self, revision_id: UUID) -> list[RuntimeValidationEvidence]:
        rows = self._session.scalars(
            select(RuntimeValidationEvidenceRow)
            .where(RuntimeValidationEvidenceRow.runtime_revision_id == str(revision_id))
            .order_by(
                RuntimeValidationEvidenceRow.recorded_at,
                RuntimeValidationEvidenceRow.id,
            )
        )
        return [self._from_row(row) for row in rows]

    def latest_for_contract(
        self, revision_id: UUID, contract_version: str
    ) -> RuntimeValidationEvidence | None:
        row = self._session.scalar(
            select(RuntimeValidationEvidenceRow)
            .where(
                RuntimeValidationEvidenceRow.runtime_revision_id == str(revision_id),
                RuntimeValidationEvidenceRow.contract_version == contract_version,
            )
            .order_by(
                RuntimeValidationEvidenceRow.recorded_at.desc(),
                RuntimeValidationEvidenceRow.id.desc(),
            )
            .limit(1)
        )
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: RuntimeValidationEvidenceRow) -> RuntimeValidationEvidence:
        return RuntimeValidationEvidence(
            id=UUID(row.id),
            runtime_revision_id=UUID(row.runtime_revision_id),
            contract_version=row.contract_version,
            result=row.result,
            started_at=_timestamp_from_text(row.started_at),
            completed_at=_timestamp_from_text(row.completed_at),
            evidence_reference=row.evidence_reference,
            summary=row.summary,
            recorded_at=_timestamp_from_text(row.recorded_at),
        )


class RuntimeRecipeInstallationRepository:
    """Only exact state transitions for one reviewed installation attempt."""

    _NEXT: ClassVar[
        dict[RuntimeRecipeInstallationState, RuntimeRecipeInstallationState]
    ] = {
        RuntimeRecipeInstallationState.PENDING: (
            RuntimeRecipeInstallationState.ACQUIRING
        ),
        RuntimeRecipeInstallationState.ACQUIRING: (
            RuntimeRecipeInstallationState.ASSEMBLING
        ),
        RuntimeRecipeInstallationState.ASSEMBLING: (
            RuntimeRecipeInstallationState.VALIDATING
        ),
        RuntimeRecipeInstallationState.VALIDATING: RuntimeRecipeInstallationState.READY,
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        operation_id: UUID,
        recipe_id: str,
        recipe_revision: int,
        recipe_digest: str,
        generation: str,
        now: datetime,
    ) -> RuntimeRecipeInstallationRow:
        row = RuntimeRecipeInstallationRow(
            id=str(operation_id),
            recipe_id=recipe_id,
            recipe_revision=recipe_revision,
            recipe_digest=recipe_digest,
            generation=generation,
            state=RuntimeRecipeInstallationState.PENDING,
            created_at=_timestamp_to_text(now),
            updated_at=_timestamp_to_text(now),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def create_failed_before_start(
        self,
        *,
        operation_id: UUID,
        recipe_id: str,
        recipe_revision: int,
        recipe_digest: str,
        generation: str,
        code: str,
        now: datetime,
    ) -> RuntimeRecipeInstallationRow:
        """Record an accepted worker that failed before it could enter pending."""

        timestamp = _timestamp_to_text(now)
        row = RuntimeRecipeInstallationRow(
            id=str(operation_id),
            recipe_id=recipe_id,
            recipe_revision=recipe_revision,
            recipe_digest=recipe_digest,
            generation=generation,
            state=RuntimeRecipeInstallationState.FAILED,
            created_at=timestamp,
            updated_at=timestamp,
            completed_at=timestamp,
            failure_code=code,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, operation_id: UUID) -> RuntimeRecipeInstallationRow | None:
        return self._session.get(RuntimeRecipeInstallationRow, str(operation_id))

    def active(self, generation: str) -> RuntimeRecipeInstallationRow | None:
        return self._session.scalar(
            select(RuntimeRecipeInstallationRow)
            .where(
                RuntimeRecipeInstallationRow.generation == generation,
                RuntimeRecipeInstallationRow.state.in_(
                    (
                        RuntimeRecipeInstallationState.PENDING,
                        RuntimeRecipeInstallationState.ACQUIRING,
                        RuntimeRecipeInstallationState.ASSEMBLING,
                        RuntimeRecipeInstallationState.VALIDATING,
                    )
                ),
            )
            .limit(1)
        )

    def latest_ready(self, generation: str) -> RuntimeRecipeInstallationRow | None:
        return self._session.scalar(
            select(RuntimeRecipeInstallationRow)
            .where(
                RuntimeRecipeInstallationRow.generation == generation,
                RuntimeRecipeInstallationRow.state
                == RuntimeRecipeInstallationState.READY,
            )
            .order_by(RuntimeRecipeInstallationRow.completed_at.desc())
            .limit(1)
        )

    def ready_for_revision(
        self, revision_id: UUID
    ) -> RuntimeRecipeInstallationRow | None:
        return self._session.scalar(
            select(RuntimeRecipeInstallationRow)
            .where(
                RuntimeRecipeInstallationRow.runtime_revision_id == str(revision_id),
                RuntimeRecipeInstallationRow.state
                == RuntimeRecipeInstallationState.READY,
            )
            .limit(1)
        )

    def advance(
        self,
        operation_id: UUID,
        *,
        expected: RuntimeRecipeInstallationState,
        now: datetime,
        image_id: str | None = None,
        attestation_json: str | None = None,
    ) -> None:
        row = self.get(operation_id)
        if row is None or row.state is not expected:
            raise ValueError("runtime recipe installation phase differs")
        if (
            expected not in self._NEXT
            or expected is RuntimeRecipeInstallationState.VALIDATING
        ):
            raise ValueError("runtime recipe installation transition is invalid")
        row.state = self._NEXT[expected]
        row.updated_at = _timestamp_to_text(now)
        if image_id is not None:
            row.image_id = image_id
        if attestation_json is not None:
            row.attestation_json = attestation_json
        self._session.flush()

    def ready(
        self,
        operation_id: UUID,
        *,
        revision_id: UUID,
        image_id: str,
        attestation_json: str,
        validation_json: str,
        now: datetime,
    ) -> None:
        row = self.get(operation_id)
        if row is None or row.state is not RuntimeRecipeInstallationState.VALIDATING:
            raise ValueError("runtime recipe installation is not validating")
        row.state = RuntimeRecipeInstallationState.READY
        row.runtime_revision_id = str(revision_id)
        row.image_id = image_id
        row.attestation_json = attestation_json
        row.validation_json = validation_json
        row.updated_at = _timestamp_to_text(now)
        row.completed_at = row.updated_at
        self._session.flush()

    def fail(self, operation_id: UUID, *, code: str, now: datetime) -> None:
        row = self.get(operation_id)
        if row is None or row.state in (
            RuntimeRecipeInstallationState.READY,
            RuntimeRecipeInstallationState.FAILED,
        ):
            raise ValueError("runtime recipe installation is terminal")
        row.state = RuntimeRecipeInstallationState.FAILED
        row.failure_code = code
        row.updated_at = _timestamp_to_text(now)
        row.completed_at = row.updated_at
        self._session.flush()


class GlobalHeavyOperationLeaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(self, *, kind: GlobalHeavyOperationKind, operation_id: UUID) -> None:
        self._session.add(
            GlobalHeavyOperationLeaseRow(
                singleton_id=1, operation_kind=kind, operation_id=str(operation_id)
            )
        )
        self._session.flush()

    def claim(
        self,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(GlobalHeavyOperationLeaseRow)
            .where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest.is_(None),
            )
            .values(
                lease_token_digest=token_digest,
                acquired_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def renew(
        self,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(GlobalHeavyOperationLeaseRow)
            .where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
                GlobalHeavyOperationLeaseRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release(self, *, operation_id: UUID, token_digest: str) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release_for_operation(self, operation_id: UUID) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def current(self) -> GlobalHeavyOperationLeaseRow | None:
        return self._session.get(GlobalHeavyOperationLeaseRow, 1)

    def release_expired_exact(
        self,
        *,
        kind: GlobalHeavyOperationKind,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_kind == kind,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
                GlobalHeavyOperationLeaseRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

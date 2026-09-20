"""Bounded system retention for MCP-owned protected workspaces."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupService,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceService,
)
from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState
from kodi_lab.storage.models import (
    AgentWorkspaceCleanupOperationRow,
    AgentWorkspaceRow,
    McpAgentWorkspaceBindingRow,
    McpClientRow,
    McpSecurityEventRow,
)
from kodi_lab.workspace import AgentWorkspaceStorageError, workspace_coordination_lock

TERMINAL_BYTE_RETENTION = timedelta(days=7)
DISABLED_CLIENT_QUARANTINE = timedelta(days=30)
RETENTION_SWEEP_INTERVAL = timedelta(minutes=5)
RETENTION_MAX_INSPECTED = 32
RETENTION_MAX_TRANSITIONS = 2
RETENTION_MAX_CLEANUPS = 2


def _text(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _time(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


class McpWorkspaceRetentionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    owning_client_enabled: bool
    classification: str
    deadline_at: datetime | None
    automatic_cleanup_blocked: bool


class McpWorkspaceRetentionSweepResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inspected: int
    lifecycle_transitions: int
    cleanups_queued: int


class McpWorkspaceRetentionService:
    """Run a small deterministic sweep during ordinary safe application activity."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        workspaces: AgentWorkspaceService,
        cleanups: AgentWorkspaceCleanupService,
        submit_cleanup: Callable[[UUID], None],
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_inspected: int = RETENTION_MAX_INSPECTED,
        max_transitions: int = RETENTION_MAX_TRANSITIONS,
        max_cleanups: int = RETENTION_MAX_CLEANUPS,
    ) -> None:
        if min(max_inspected, max_transitions, max_cleanups) < 1:
            raise ValueError("retention sweep bounds must be positive")
        self._sessions = session_factory
        self._workspaces = workspaces
        self._cleanups = cleanups
        self._submit_cleanup = submit_cleanup
        self._data_root = data_root
        self._clock = clock
        self._max_inspected = max_inspected
        self._max_transitions = max_transitions
        self._max_cleanups = max_cleanups
        self._lock = threading.Lock()
        self._last_sweep: datetime | None = None
        self._cursor: str | None = None

    def projection(self, workspace_id: UUID) -> McpWorkspaceRetentionProjection:
        with self._sessions() as session:
            row = session.execute(
                select(AgentWorkspaceRow, McpClientRow)
                .join(
                    McpAgentWorkspaceBindingRow,
                    McpAgentWorkspaceBindingRow.workspace_id == AgentWorkspaceRow.id,
                )
                .join(
                    McpClientRow,
                    McpClientRow.id == McpAgentWorkspaceBindingRow.client_id,
                )
                .where(AgentWorkspaceRow.id == str(workspace_id))
            ).one_or_none()
            latest_cleanup = session.scalar(
                select(AgentWorkspaceCleanupOperationRow)
                .where(
                    AgentWorkspaceCleanupOperationRow.workspace_id == str(workspace_id)
                )
                .order_by(
                    AgentWorkspaceCleanupOperationRow.requested_at.desc(),
                    AgentWorkspaceCleanupOperationRow.id.desc(),
                )
                .limit(1)
            )
        if row is None:
            raise KeyError("workspace retention binding not found")
        workspace, client = row
        if latest_cleanup is not None and latest_cleanup.state.value in {
            "failed",
            "reconciliation_required",
        }:
            return McpWorkspaceRetentionProjection(
                owning_client_enabled=client.enabled,
                classification="manual_attention_required",
                deadline_at=None,
                automatic_cleanup_blocked=True,
            )
        if workspace.bytes_state is AgentWorkspaceBytesState.CLEANED:
            return McpWorkspaceRetentionProjection(
                owning_client_enabled=client.enabled,
                classification="bytes_cleaned",
                deadline_at=None,
                automatic_cleanup_blocked=False,
            )
        if workspace.state is AgentWorkspaceState.RECONCILIATION_REQUIRED:
            return McpWorkspaceRetentionProjection(
                owning_client_enabled=client.enabled,
                classification="manual_attention_required",
                deadline_at=None,
                automatic_cleanup_blocked=True,
            )
        if workspace.state is AgentWorkspaceState.READY:
            disabled_at = _time(client.disabled_at)
            if client.enabled or disabled_at is None:
                return McpWorkspaceRetentionProjection(
                    owning_client_enabled=client.enabled,
                    classification="active",
                    deadline_at=None,
                    automatic_cleanup_blocked=False,
                )
            return McpWorkspaceRetentionProjection(
                owning_client_enabled=False,
                classification="disabled_client_quarantine",
                deadline_at=disabled_at + DISABLED_CLIENT_QUARANTINE,
                automatic_cleanup_blocked=False,
            )
        if workspace.state in {
            AgentWorkspaceState.ABANDONED,
            AgentWorkspaceState.PROMOTED,
        }:
            terminal_at = _time(
                workspace.abandoned_at
                if workspace.state is AgentWorkspaceState.ABANDONED
                else workspace.promoted_at
            )
            blocker = self._cleanups.eligibility_code(UUID(workspace.id))
            return McpWorkspaceRetentionProjection(
                owning_client_enabled=client.enabled,
                classification=(
                    "manual_attention_required"
                    if blocker is not None
                    else "terminal_cleanup_pending"
                ),
                deadline_at=(
                    None
                    if terminal_at is None
                    else terminal_at + TERMINAL_BYTE_RETENTION
                ),
                automatic_cleanup_blocked=blocker is not None,
            )
        return McpWorkspaceRetentionProjection(
            owning_client_enabled=client.enabled,
            classification="manual_attention_required",
            deadline_at=None,
            automatic_cleanup_blocked=True,
        )

    def sweep(self, *, force: bool = False) -> McpWorkspaceRetentionSweepResult:
        if not self._lock.acquire(blocking=False):
            return McpWorkspaceRetentionSweepResult(
                inspected=0, lifecycle_transitions=0, cleanups_queued=0
            )
        try:
            now = self._clock()
            if (
                not force
                and self._last_sweep is not None
                and now - self._last_sweep < RETENTION_SWEEP_INTERVAL
            ):
                return McpWorkspaceRetentionSweepResult(
                    inspected=0, lifecycle_transitions=0, cleanups_queued=0
                )
            self._last_sweep = now
            candidates = self._candidates(now)
            transitions = 0
            cleanups = 0
            for workspace_id in candidates:
                projection = self.projection(workspace_id)
                if projection.automatic_cleanup_blocked:
                    self._audit_once("retention_cleanup_blocked", workspace_id, now)
                    continue
                if projection.deadline_at is None or projection.deadline_at > now:
                    continue
                if projection.classification == "disabled_client_quarantine":
                    if transitions >= self._max_transitions:
                        continue
                    try:
                        self._workspaces.abandon(workspace_id)
                    except (AgentWorkspaceConflictError, AgentWorkspaceStorageError):
                        self._audit_once(
                            "retention_transition_blocked", workspace_id, now
                        )
                        continue
                    transitions += 1
                    self._audit_once("retention_auto_abandoned", workspace_id, now)
                    continue
                if projection.classification != "terminal_cleanup_pending":
                    continue
                if cleanups >= self._max_cleanups:
                    continue
                try:
                    with workspace_coordination_lock(
                        self._data_root,
                        workspace_id,
                        exclusive=True,
                        blocking=False,
                    ):
                        current = self.projection(workspace_id)
                        if (
                            current.classification != "terminal_cleanup_pending"
                            or current.deadline_at is None
                            or current.deadline_at > now
                        ):
                            continue
                        operation = self._cleanups.queue(workspace_id)
                except (AgentWorkspaceCleanupError, AgentWorkspaceStorageError):
                    self._audit_once("retention_cleanup_blocked", workspace_id, now)
                    continue
                cleanups += 1
                self._audit_once("retention_cleanup_queued", workspace_id, now)
                self._submit_cleanup(operation.operation_id)
            return McpWorkspaceRetentionSweepResult(
                inspected=len(candidates),
                lifecycle_transitions=transitions,
                cleanups_queued=cleanups,
            )
        finally:
            self._lock.release()

    def _candidates(self, now: datetime) -> list[UUID]:
        terminal_cutoff = _text(now - TERMINAL_BYTE_RETENTION)
        disabled_cutoff = _text(now - DISABLED_CLIENT_QUARANTINE)
        predicate = and_(
            AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT,
            or_(
                and_(
                    AgentWorkspaceRow.state == AgentWorkspaceState.ABANDONED,
                    AgentWorkspaceRow.abandoned_at <= terminal_cutoff,
                ),
                and_(
                    AgentWorkspaceRow.state == AgentWorkspaceState.PROMOTED,
                    AgentWorkspaceRow.promoted_at <= terminal_cutoff,
                ),
                and_(
                    AgentWorkspaceRow.state == AgentWorkspaceState.READY,
                    McpClientRow.enabled.is_(False),
                    McpClientRow.disabled_at <= disabled_cutoff,
                ),
            ),
        )
        with self._sessions() as session:
            statement = (
                select(AgentWorkspaceRow.id)
                .join(
                    McpAgentWorkspaceBindingRow,
                    McpAgentWorkspaceBindingRow.workspace_id == AgentWorkspaceRow.id,
                )
                .join(
                    McpClientRow,
                    McpClientRow.id == McpAgentWorkspaceBindingRow.client_id,
                )
                .where(predicate)
                .order_by(AgentWorkspaceRow.id)
                .limit(self._max_inspected)
            )
            if self._cursor is not None:
                statement = statement.where(AgentWorkspaceRow.id > self._cursor)
            values = list(session.scalars(statement).all())
            if not values and self._cursor is not None:
                self._cursor = None
                values = list(
                    session.scalars(
                        select(AgentWorkspaceRow.id)
                        .join(
                            McpAgentWorkspaceBindingRow,
                            McpAgentWorkspaceBindingRow.workspace_id
                            == AgentWorkspaceRow.id,
                        )
                        .join(
                            McpClientRow,
                            McpClientRow.id == McpAgentWorkspaceBindingRow.client_id,
                        )
                        .where(predicate)
                        .order_by(AgentWorkspaceRow.id)
                        .limit(self._max_inspected)
                    ).all()
                )
        if values:
            self._cursor = values[-1]
        return [UUID(value) for value in values]

    def _audit_once(self, kind: str, workspace_id: UUID, now: datetime) -> None:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(McpSecurityEventRow.id).where(
                    McpSecurityEventRow.event_kind == kind,
                    McpSecurityEventRow.workspace_id == str(workspace_id),
                )
            )
            if existing is not None:
                return
            binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
            session.add(
                McpSecurityEventRow(
                    id=str(uuid4()),
                    event_kind=kind,
                    actor="system",
                    client_id=None if binding is None else binding.client_id,
                    credential_id=None,
                    target_id=None,
                    development_source_id=(
                        None if binding is None else binding.development_source_id
                    ),
                    workspace_id=str(workspace_id),
                    preset_id=None,
                    occurred_at=_text(now),
                )
            )

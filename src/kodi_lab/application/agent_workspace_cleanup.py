"""Explicit durable cleanup of one exact protected workspace byte subtree."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceCleanupOperation,
    AgentWorkspaceCleanupPhase,
    AgentWorkspaceCleanupState,
    AgentWorkspaceCreationState,
    AgentWorkspaceLifecycleEvent,
    AgentWorkspaceLifecycleEventKind,
    AgentWorkspacePromotionOperationState,
    AgentWorkspaceState,
    GlobalHeavyOperationKind,
    PromotionVerificationState,
)
from kodi_lab.storage.models import AgentWorkspaceCaptureOperationRow
from kodi_lab.storage.repositories import (
    AgentWorkspaceCleanupOperationRepository,
    AgentWorkspaceCreationOperationRepository,
    AgentWorkspaceLifecycleEventRepository,
    AgentWorkspacePromotionOperationRepository,
    AgentWorkspacePromotionPlanRepository,
    AgentWorkspaceRepository,
    GlobalHeavyOperationLeaseRepository,
    PromotionVerificationOperationRepository,
)
from kodi_lab.workspace import (
    AgentWorkspaceReconciliationRequired,
    cleanup_workspace_storage,
    workspace_storage_absent,
    workspace_storage_identity,
)

CLEANUP_LEASE_DURATION = timedelta(minutes=30)


class AgentWorkspaceCleanupError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


class AgentWorkspaceCleanupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    workspace_id: UUID
    state: AgentWorkspaceCleanupState
    phase: AgentWorkspaceCleanupPhase
    deletion_started: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None


def _status(operation: AgentWorkspaceCleanupOperation) -> AgentWorkspaceCleanupStatus:
    return AgentWorkspaceCleanupStatus(
        operation_id=operation.id,
        workspace_id=operation.workspace_id,
        state=operation.state,
        phase=operation.phase,
        deletion_started=operation.deletion_started,
        requested_at=operation.requested_at,
        started_at=operation.started_at,
        completed_at=operation.completed_at,
        failure_code=operation.failure_code,
    )


@dataclass(frozen=True, slots=True)
class _CleanupLease:
    operation_id: UUID
    workspace_id: UUID
    token: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AgentWorkspaceCleanupService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        failure_injector: Callable[[str], None] = lambda _phase: None,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._failure_injector = failure_injector

    def _validate_eligibility(self, session: Session, workspace_id: UUID) -> None:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise AgentWorkspaceCleanupError(
                "agent_workspace_not_found", "Agent workspace was not found."
            )
        if workspace.bytes_state is AgentWorkspaceBytesState.CLEANED:
            return
        # Captured baselines may be named by validation/deployment/publication
        # history. V1 has no per-generation reference-aware byte cleanup.
        if (
            session.scalar(
                select(AgentWorkspaceCaptureOperationRow.id)
                .where(
                    AgentWorkspaceCaptureOperationRow.workspace_id == str(workspace_id)
                )
                .limit(1)
            )
            is not None
        ):
            raise AgentWorkspaceCleanupError(
                "workspace_capture_history_retained",
                "Workspace capture evidence must be retained.",
            )
        if workspace.state is AgentWorkspaceState.RECONCILIATION_REQUIRED:
            raise AgentWorkspaceCleanupError(
                "workspace_reconciliation_blocked",
                "Workspace reconciliation is required before cleanup.",
            )
        if workspace.state not in {
            AgentWorkspaceState.ABANDONED,
            AgentWorkspaceState.PROMOTED,
        }:
            raise AgentWorkspaceCleanupError(
                "workspace_cleanup_not_eligible",
                "Only an abandoned or promoted workspace may be cleaned.",
            )
        creation = AgentWorkspaceCreationOperationRepository(session).get_for_workspace(
            workspace_id
        )
        if creation is None or creation.state in {
            AgentWorkspaceCreationState.QUEUED,
            AgentWorkspaceCreationState.RUNNING,
            AgentWorkspaceCreationState.RECONCILIATION_REQUIRED,
        }:
            raise AgentWorkspaceCleanupError(
                "workspace_cleanup_creation_blocked",
                "Workspace creation is not safely terminal.",
            )
        plan = AgentWorkspacePromotionPlanRepository(session).active_for_workspace(
            workspace_id, now=self._clock()
        )
        promotions = AgentWorkspacePromotionOperationRepository(session)
        if (
            plan is not None
            or promotions.active_for_workspace(workspace_id) is not None
        ):
            raise AgentWorkspaceCleanupError(
                "workspace_cleanup_promotion_active",
                "Workspace promotion is active.",
            )
        if promotions.reconciliation_for_workspace(workspace_id):
            raise AgentWorkspaceCleanupError(
                "workspace_reconciliation_blocked",
                "Workspace promotion requires reconciliation.",
            )
        verifications = PromotionVerificationOperationRepository(session)
        if verifications.blocking_for_workspace(workspace_id):
            raise AgentWorkspaceCleanupError(
                "workspace_cleanup_verification_blocked",
                "Workspace verification is active or requires reconciliation.",
            )
        if workspace.state is AgentWorkspaceState.PROMOTED:
            promotion = promotions.latest_for_workspace(workspace_id)
            verification = verifications.latest_for_workspace(workspace_id)
            if (
                promotion is None
                or promotion.state
                is not AgentWorkspacePromotionOperationState.SUCCEEDED
                or verification is None
                or verification.state
                not in {
                    PromotionVerificationState.SUCCEEDED,
                    PromotionVerificationState.FAILED,
                    PromotionVerificationState.INCOMPLETE,
                }
            ):
                raise AgentWorkspaceCleanupError(
                    "workspace_cleanup_verification_blocked",
                    "Promoted workspace verification is not safely terminal.",
                )

    def eligibility_code(self, workspace_id: UUID) -> str | None:
        """Return a bounded blocker without changing cleanup state."""

        try:
            with self._session_factory() as session:
                self._validate_eligibility(session, workspace_id)
        except AgentWorkspaceCleanupError as error:
            return error.code
        return None

    def queue(self, workspace_id: UUID) -> AgentWorkspaceCleanupStatus:
        with self._session_factory.begin() as session:
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            if workspace is None:
                raise AgentWorkspaceCleanupError(
                    "agent_workspace_not_found", "Agent workspace was not found."
                )
            existing = AgentWorkspaceCleanupOperationRepository(
                session
            ).get_for_workspace(workspace_id)
            if (
                existing is not None
                and existing.state is not AgentWorkspaceCleanupState.FAILED
            ):
                return _status(existing)
            self._validate_eligibility(session, workspace_id)
            now = self._clock()
            operation = AgentWorkspaceCleanupOperation(
                id=self._uuid_factory(),
                workspace_id=workspace_id,
                state=AgentWorkspaceCleanupState.QUEUED,
                phase=AgentWorkspaceCleanupPhase.QUEUED,
                requested_at=now,
            )
            try:
                AgentWorkspaceCleanupOperationRepository(session).create(operation)
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_CLEANUP,
                    operation_id=operation.id,
                )
                AgentWorkspaceLifecycleEventRepository(session).create(
                    AgentWorkspaceLifecycleEvent(
                        id=self._uuid_factory(),
                        workspace_id=workspace_id,
                        kind=AgentWorkspaceLifecycleEventKind.CLEANUP_QUEUED,
                        occurred_at=now,
                        cleanup_operation_id=operation.id,
                    )
                )
            except IntegrityError as error:
                raise AgentWorkspaceCleanupError(
                    "workspace_cleanup_conflict",
                    "Another global-heavy operation is active.",
                ) from error
        return _status(operation)

    def get(self, operation_id: UUID) -> AgentWorkspaceCleanupStatus:
        with self._session_factory() as session:
            operation = AgentWorkspaceCleanupOperationRepository(session).get(
                operation_id
            )
        if operation is None:
            raise AgentWorkspaceCleanupError(
                "workspace_cleanup_not_found", "Workspace cleanup was not found."
            )
        return _status(operation)

    def acquire(self, operation_id: UUID) -> _CleanupLease | None:
        with self._session_factory() as session:
            operation = AgentWorkspaceCleanupOperationRepository(session).get(
                operation_id
            )
        if (
            operation is None
            or operation.state is not AgentWorkspaceCleanupState.QUEUED
        ):
            return None
        lease = _CleanupLease(
            operation.id, operation.workspace_id, secrets.token_urlsafe(32)
        )
        now = self._clock()
        with self._session_factory.begin() as session:
            if not AgentWorkspaceCleanupOperationRepository(session).acquire(
                operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + CLEANUP_LEASE_DURATION,
            ) or not GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=operation_id,
                token_digest=lease.digest,
                now=now,
                expires_at=now + CLEANUP_LEASE_DURATION,
            ):
                raise AgentWorkspaceCleanupError(
                    "workspace_cleanup_authority_lost",
                    "Workspace cleanup authority was lost.",
                )
        return lease

    def _terminal_event(
        self,
        session: Session,
        lease: _CleanupLease,
        kind: AgentWorkspaceLifecycleEventKind,
        now: datetime,
        failure_code: str | None = None,
    ) -> None:
        AgentWorkspaceLifecycleEventRepository(session).create(
            AgentWorkspaceLifecycleEvent(
                id=self._uuid_factory(),
                workspace_id=lease.workspace_id,
                kind=kind,
                occurred_at=now,
                cleanup_operation_id=lease.operation_id,
                failure_code=failure_code,
            )
        )

    def run(self, lease: _CleanupLease) -> None:
        deletion_started = False
        try:
            with self._session_factory() as session:
                self._validate_eligibility(session, lease.workspace_id)
                operation = AgentWorkspaceCleanupOperationRepository(session).get(
                    lease.operation_id
                )
                creation = AgentWorkspaceCreationOperationRepository(
                    session
                ).get_for_workspace(lease.workspace_id)
            if operation is None:
                raise AgentWorkspaceCleanupError(
                    "workspace_cleanup_not_found", "Workspace cleanup was not found."
                )
            if operation.deletion_started:
                deletion_started = True
                if (
                    operation.expected_device is None
                    or operation.expected_inode is None
                ):
                    raise AgentWorkspaceReconciliationRequired(
                        "workspace_cleanup_identity_missing",
                        "Workspace cleanup requires reconciliation.",
                    )
                identity = (operation.expected_device, operation.expected_inode)
            elif workspace_storage_absent(self._data_root, lease.workspace_id):
                if (
                    creation is None
                    or creation.state is not AgentWorkspaceCreationState.FAILED
                ):
                    raise AgentWorkspaceReconciliationRequired(
                        "workspace_cleanup_storage_missing",
                        "Workspace cleanup requires reconciliation.",
                    )
                self._finish_success(lease)
                return
            else:
                self._failure_injector("before_deletion_marker")
                identity = workspace_storage_identity(
                    self._data_root, lease.workspace_id
                )
                with self._session_factory.begin() as session:
                    if not AgentWorkspaceCleanupOperationRepository(
                        session
                    ).start_deletion(
                        lease.operation_id,
                        token_digest=lease.digest,
                        device=identity[0],
                        inode=identity[1],
                    ):
                        raise AgentWorkspaceCleanupError(
                            "workspace_cleanup_authority_lost",
                            "Workspace cleanup authority was lost.",
                        )
                deletion_started = True
            self._failure_injector("before_delete")
            cleanup_workspace_storage(
                self._data_root, lease.workspace_id, expected_identity=identity
            )
            with self._session_factory.begin() as session:
                if not AgentWorkspaceCleanupOperationRepository(session).set_phase(
                    lease.operation_id,
                    token_digest=lease.digest,
                    phase=AgentWorkspaceCleanupPhase.VERIFYING,
                ):
                    raise AgentWorkspaceCleanupError(
                        "workspace_cleanup_authority_lost",
                        "Workspace cleanup authority was lost.",
                    )
            self._failure_injector("before_finalize")
            if not workspace_storage_absent(self._data_root, lease.workspace_id):
                raise AgentWorkspaceReconciliationRequired(
                    "workspace_cleanup_incomplete",
                    "Workspace cleanup requires reconciliation.",
                )
            self._finish_success(lease)
        except Exception as error:
            self._finish_failure(
                lease,
                error,
                reconciliation=(
                    deletion_started
                    or isinstance(error, AgentWorkspaceReconciliationRequired)
                ),
            )

    def _finish_success(self, lease: _CleanupLease) -> None:
        now = self._clock()
        with self._session_factory.begin() as session:
            if (
                not AgentWorkspaceRepository(session).mark_bytes_cleaned(
                    lease.workspace_id, now=now
                )
                or not AgentWorkspaceCleanupOperationRepository(session).finish(
                    lease.operation_id,
                    token_digest=lease.digest,
                    state=AgentWorkspaceCleanupState.SUCCEEDED,
                    now=now,
                    failure_code=None,
                )
                or not GlobalHeavyOperationLeaseRepository(session).release(
                    operation_id=lease.operation_id, token_digest=lease.digest
                )
            ):
                raise AgentWorkspaceCleanupError(
                    "workspace_cleanup_finalization_failed",
                    "Workspace cleanup finalization was uncertain.",
                )
            self._terminal_event(
                session,
                lease,
                AgentWorkspaceLifecycleEventKind.CLEANUP_SUCCEEDED,
                now,
            )

    def _finish_failure(
        self, lease: _CleanupLease, error: Exception, *, reconciliation: bool
    ) -> None:
        now = self._clock()
        code = getattr(error, "code", "workspace_cleanup_failed")[:128]
        state = (
            AgentWorkspaceCleanupState.RECONCILIATION_REQUIRED
            if reconciliation
            else AgentWorkspaceCleanupState.FAILED
        )
        kind = (
            AgentWorkspaceLifecycleEventKind.CLEANUP_RECONCILIATION_REQUIRED
            if reconciliation
            else AgentWorkspaceLifecycleEventKind.CLEANUP_FAILED
        )
        with self._session_factory.begin() as session:
            if reconciliation:
                AgentWorkspaceRepository(session).mark_cleanup_reconciliation(
                    lease.workspace_id, now=now
                )
            AgentWorkspaceCleanupOperationRepository(session).finish(
                lease.operation_id,
                token_digest=lease.digest,
                state=state,
                now=now,
                failure_code=code,
            )
            GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=lease.operation_id, token_digest=lease.digest
            )
            self._terminal_event(session, lease, kind, now, code)

    def queued(self) -> list[AgentWorkspaceCleanupOperation]:
        with self._session_factory() as session:
            return AgentWorkspaceCleanupOperationRepository(session).list_queued()

    def sweep_startup(self) -> None:
        now = self._clock()
        with self._session_factory() as session:
            active = AgentWorkspaceCleanupOperationRepository(session).list_active()
        for operation in active:
            if (
                operation.state is not AgentWorkspaceCleanupState.RUNNING
                or operation.lease_expires_at is None
                or operation.lease_expires_at > now
                or operation.lease_token_digest is None
            ):
                continue
            with self._session_factory.begin() as session:
                cleanup = AgentWorkspaceCleanupOperationRepository(session)
                heavy = GlobalHeavyOperationLeaseRepository(session)
                if not heavy.release_expired_exact(
                    kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_CLEANUP,
                    operation_id=operation.id,
                    token_digest=operation.lease_token_digest,
                    now=now,
                ) or not cleanup.recover_stale(
                    operation.id,
                    token_digest=operation.lease_token_digest,
                    now=now,
                    resume=operation.deletion_started,
                ):
                    raise AgentWorkspaceCleanupError(
                        "workspace_cleanup_recovery_failed",
                        "Workspace cleanup recovery could not be published.",
                    )
                if operation.deletion_started:
                    heavy.reserve(
                        kind=GlobalHeavyOperationKind.AGENT_WORKSPACE_CLEANUP,
                        operation_id=operation.id,
                    )
                else:
                    self._terminal_event(
                        session,
                        _CleanupLease(operation.id, operation.workspace_id, "recovery"),
                        AgentWorkspaceLifecycleEventKind.CLEANUP_FAILED,
                        now,
                        "workspace_cleanup_interrupted_before_delete",
                    )


class AgentWorkspaceCleanupExecutor:
    def __init__(self, service: AgentWorkspaceCleanupService) -> None:
        self._service = service
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agent-workspace-cleanup"
        )

    def submit(self, operation_id: UUID) -> None:
        self._pool.submit(self._run, operation_id)

    def start(self) -> None:
        self._service.sweep_startup()
        for operation in self._service.queued():
            self.submit(operation.id)

    def _run(self, operation_id: UUID) -> None:
        lease = self._service.acquire(operation_id)
        if lease is not None:
            self._service.run(lease)

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)

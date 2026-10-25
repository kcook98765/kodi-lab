"""Explicit source-baseline admission for the selected stopped disposable session.

This service queues existing typed work. It neither submits an executor nor
retries, repairs, attaches sources, enables add-ons or changes agent authority.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentOperationCoordinator,
    DevelopmentSourceDeploymentStatus,
)
from kodi_lab.domain import DevelopmentSourceDeploymentOperation
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
    require_workspace_session_admission,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    SessionSourceCopyAdmissionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceDeploymentOperationRepository

SOURCE_COPY_NAMESPACE = UUID("67d813f0-d49f-4d8b-8de5-6885992328c8")
MAX_ADMISSIONS = 1024
MAX_ADMISSIONS_PER_SESSION = 32
_SCOPE = (
    "target_id",
    "runtime_revision_id",
    "workspace_id",
    "source_id",
    "container_id",
)


class SessionSourceCopyReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    operation_id: UUID
    session_generation: int
    target_id: UUID
    runtime_revision_id: UUID
    workspace_id: UUID
    source_id: UUID
    container_id: str
    requested_at: str
    detail: Literal["retained", "not_retained"]
    operation: DevelopmentSourceDeploymentStatus | None


class _Replay(Exception):
    """Exit the queue's writer transaction without creating any new work."""


class SessionSourceCopyService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        coordinator: DevelopmentSourceDeploymentOperationCoordinator,
    ) -> None:
        self._sessions = sessions
        self._coordinator = coordinator

    def get(self, request_id: UUID) -> SessionSourceCopyReceipt | None:
        with self._sessions() as session:
            session.connection().exec_driver_sql("BEGIN")
            row = session.get(SessionSourceCopyAdmissionRow, str(request_id))
            return None if row is None else self._receipt(session, row)

    def admit(
        self, *, request_id: UUID, expected_generation: int
    ) -> SessionSourceCopyReceipt:
        if type(expected_generation) is not int or expected_generation < 1:
            raise DevelopmentSessionConflict("development_session_generation_conflict")
        prior = self.get(request_id)
        if prior is not None:
            self._require_replay_generation(prior, expected_generation)
            return prior
        with self._sessions() as session:
            current = self._selected(session, expected_generation)
            target_id, source_id = UUID(current.target_id), UUID(current.source_id)

        def precondition(session: Session) -> None:
            row = session.get(SessionSourceCopyAdmissionRow, str(request_id))
            if row is not None:
                self._require_replay_generation(
                    self._receipt(session, row), expected_generation
                )
                raise _Replay
            self._selected(session, expected_generation)
            total = session.scalar(
                select(func.count()).select_from(SessionSourceCopyAdmissionRow)
            )
            count = session.scalar(
                select(func.count())
                .select_from(SessionSourceCopyAdmissionRow)
                .where(
                    SessionSourceCopyAdmissionRow.session_generation
                    == expected_generation
                )
            )
            if (
                total is None
                or count is None
                or total >= MAX_ADMISSIONS
                or count >= MAX_ADMISSIONS_PER_SESSION
            ):
                raise DevelopmentSessionConflict("session_source_copy_history_full")

        def on_claim(
            session: Session, operation: DevelopmentSourceDeploymentOperation
        ) -> None:
            current = self._selected(session, expected_generation)
            if (
                str(operation.original_managed_instance_id) != current.target_id
                or str(operation.managed_instance_id) != current.target_id
                or str(operation.runtime_revision_id) != current.runtime_revision_id
                or str(operation.development_source_id) != current.source_id
                or operation.expected_container_id != current.container_id
            ):
                raise DevelopmentSessionConflict("development_session_identity_changed")
            session.add(
                SessionSourceCopyAdmissionRow(
                    request_id=str(request_id),
                    operation_id=str(operation.id),
                    session_generation=current.generation,
                    requested_at=operation.requested_at.strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                    **{name: getattr(current, name) for name in _SCOPE},
                )
            )
            session.flush()

        try:
            self._coordinator.queue(
                target_id,
                source_id,
                precondition=precondition,
                operation_id=uuid5(SOURCE_COPY_NAMESPACE, str(request_id)),
                on_claim=on_claim,
            )
        except (_Replay, DevelopmentSourceDeploymentError):
            # A peer may commit this exact request before legacy reconciliation
            # or active-operation checks run. Its receipt is authoritative.
            if self.get(request_id) is None:
                raise
        result = self.get(request_id)
        if result is None:
            raise DevelopmentSessionConflict("session_source_copy_receipt_missing")
        self._require_replay_generation(result, expected_generation)
        return result

    @staticmethod
    def _require_replay_generation(
        receipt: SessionSourceCopyReceipt, expected_generation: int
    ) -> None:
        if receipt.session_generation != expected_generation:
            raise DevelopmentSessionConflict("session_source_copy_request_conflict")

    @staticmethod
    def _selected(session: Session, expected_generation: int) -> DevelopmentSessionRow:
        current = active_session(session)
        if current is None or current.generation != expected_generation:
            raise DevelopmentSessionConflict("development_session_generation_conflict")
        require_workspace_session_admission(
            session,
            target_id=current.target_id,
            revision_id=current.runtime_revision_id,
            workspace_id=current.workspace_id,
            source_id=current.source_id,
        )
        workspace = session.get(AgentWorkspaceRow, current.workspace_id)
        if workspace is None or workspace.development_source_id != current.source_id:
            raise DevelopmentSessionConflict("development_session_identity_changed")
        if current.container_id is None:
            raise DevelopmentSessionConflict("development_session_requires_review")
        return current

    def _receipt(
        self, session: Session, row: SessionSourceCopyAdmissionRow
    ) -> SessionSourceCopyReceipt:
        original = session.get(DevelopmentSessionRow, row.session_generation)
        if original is None or any(
            getattr(original, name) != getattr(row, name) for name in _SCOPE
        ):
            raise DevelopmentSessionConflict(
                "session_source_copy_receipt_scope_changed"
            )
        if row.operation_id != str(uuid5(SOURCE_COPY_NAMESPACE, row.request_id)):
            raise DevelopmentSessionConflict(
                "session_source_copy_receipt_scope_changed"
            )
        operation = DevelopmentSourceDeploymentOperationRepository(session).get(
            UUID(row.operation_id)
        )
        if operation is not None and (
            str(operation.original_managed_instance_id) != row.target_id
            or (
                operation.managed_instance_id is not None
                and str(operation.managed_instance_id) != row.target_id
            )
            or str(operation.runtime_revision_id) != row.runtime_revision_id
            or str(operation.development_source_id) != row.source_id
            or operation.expected_container_id != row.container_id
            or operation.requested_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            != row.requested_at
        ):
            raise DevelopmentSessionConflict(
                "session_source_copy_receipt_scope_changed"
            )
        return SessionSourceCopyReceipt(
            request_id=UUID(row.request_id),
            operation_id=UUID(row.operation_id),
            session_generation=row.session_generation,
            target_id=UUID(row.target_id),
            runtime_revision_id=UUID(row.runtime_revision_id),
            workspace_id=UUID(row.workspace_id),
            source_id=UUID(row.source_id),
            container_id=row.container_id,
            requested_at=row.requested_at,
            detail="not_retained" if operation is None else "retained",
            operation=None
            if operation is None
            else self._coordinator.to_status(operation),
        )

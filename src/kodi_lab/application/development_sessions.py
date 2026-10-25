"""Web-owned durable reservation for one add-on and one disposable Kodi profile.

Reserving/closing never provisions, starts, stops, deletes, grants or repairs.
Existing lifecycle services own those separate actions. Generation history stays
outside disposable profiles, and no existing container can be implicitly adopted.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState, InstanceType
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


class DevelopmentSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_generation: int = Field(ge=0)
    target_id: UUID
    workspace_id: UUID


class DevelopmentSessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int
    request_id: UUID
    target_id: UUID
    runtime_revision_id: UUID
    workspace_id: UUID
    source_id: UUID
    container_id: str | None
    phase: Literal["reserved", "provisioning", "bound", "uncertain", "closed"]
    created_at: datetime
    updated_at: datetime


def _status(row: DevelopmentSessionRow) -> DevelopmentSessionStatus:
    return DevelopmentSessionStatus.model_validate(
        {key: getattr(row, key) for key in DevelopmentSessionStatus.model_fields}
    )


class DevelopmentSessionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        inventory: Callable[[], ManagedContainerInventoryResult],
        *,
        data_root: Path,
    ) -> None:
        self._sessions, self._inventory, self._data_root = (
            sessions,
            inventory,
            data_root,
        )

    def status(self) -> DevelopmentSessionStatus | None:
        with self._sessions() as session:
            row = active_session(session)
            return None if row is None else _status(row)

    @staticmethod
    def _idle(session: Session) -> None:
        if any(
            session.scalar(select(model).limit(1)) is not None
            for model in (
                ManagedStartReservationRow,
                ManagedProvisionReservationRow,
                ManagedInstanceMutationLeaseRow,
                GlobalHeavyOperationLeaseRow,
            )
        ):
            raise DevelopmentSessionConflict("development_session_busy")

    def _runtime(self, session: Session, target_id: UUID, revision_id: UUID) -> None:
        from kodi_lab.runtime.lab import KNOWN_SELECTORS, canonical_id

        if target_id not in {canonical_id(selector) for selector in KNOWN_SELECTORS}:
            raise DevelopmentSessionConflict("development_session_noncanonical_target")
        target = TargetRepository(session).get(target_id)
        revision = RuntimeRevisionRepository(session).get(revision_id)
        definition = (
            None
            if revision is None
            else RuntimeDefinitionRepository(session).get(
                revision.runtime_definition_id
            )
        )
        if target is None or revision is None or definition is None:
            raise DevelopmentSessionConflict("development_session_runtime_unavailable")
        eligibility = RuntimeRevisionEligibilityPolicy(
            data_root=self._data_root
        ).evaluate(
            session,
            target=target,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(include_legacy=True),
        )
        if not eligibility.eligible:
            raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    def reserve(self, operation: DevelopmentSessionRequest) -> DevelopmentSessionStatus:
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            prior = session.scalar(
                select(DevelopmentSessionRow).where(
                    DevelopmentSessionRow.request_id == str(operation.request_id)
                )
            )
            if prior is not None:
                if (prior.generation - 1, prior.target_id, prior.workspace_id) != (
                    operation.expected_generation,
                    str(operation.target_id),
                    str(operation.workspace_id),
                ):
                    raise DevelopmentSessionConflict(
                        "development_session_request_conflict"
                    )
                return _status(prior)
            current = active_session(session)
            generation = (
                session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0
            )
            if current is not None:
                raise DevelopmentSessionConflict("development_session_already_active")
            if generation >= 256:
                raise DevelopmentSessionConflict("development_session_history_capacity")
            if generation != operation.expected_generation:
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            self._idle(session)
            # Inventory is read while every broker provision/start reserves through
            # the same SQLite writer boundary. An in-flight dispatch is not empty.
            if self._inventory().containers:
                raise DevelopmentSessionConflict(
                    "development_session_existing_resource"
                )
            instance = ManagedInstanceRepository(session).get(operation.target_id)
            workspace = AgentWorkspaceRepository(session).get(operation.workspace_id)
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or workspace is None
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or source is None
                or source.addon_id != workspace.source_addon_id
            ):
                raise DevelopmentSessionConflict(
                    "development_session_selection_unavailable"
                )
            self._runtime(session, operation.target_id, instance.runtime_revision_id)
            now = timestamp(datetime.now(UTC))
            row = DevelopmentSessionRow(
                generation=generation + 1,
                request_id=str(operation.request_id),
                target_id=str(operation.target_id),
                runtime_revision_id=str(instance.runtime_revision_id),
                workspace_id=str(workspace.id),
                source_id=str(source.id),
                container_id=None,
                phase="reserved",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            if policy is None:
                policy = DevelopmentSessionPolicyRow(
                    id=1, active_generation=row.generation
                )
                session.add(policy)
            else:
                policy.active_generation = row.generation
            session.commit()
            return _status(row)

    def close_absent(self, expected_generation: int) -> DevelopmentSessionStatus:
        """Explicitly release only an absent, idle environment; retain its history."""
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = active_session(session)
            if current is None or current.generation != expected_generation:
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            self._idle(session)
            if self._inventory().containers:
                raise DevelopmentSessionConflict(
                    "development_session_existing_resource"
                )
            current.phase, current.updated_at = "closed", timestamp(datetime.now(UTC))
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.active_generation = None
            session.commit()
            return _status(current)

    def reconcile_absent_provision(self, target_id: UUID, operation_id: UUID) -> None:
        """Web review can clear an uncertain dispatch only after proven absence."""
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ManagedProvisionReservationRow, str(target_id))
            if (
                row is None
                or row.phase != "uncertain"
                or row.operation_id != str(operation_id)
            ):
                raise DevelopmentSessionConflict("development_session_requires_review")
            if (
                self._inventory().containers
                or session.scalar(select(ManagedInstanceMutationLeaseRow).limit(1))
                is not None
                or session.scalar(select(ManagedStartReservationRow).limit(1))
                is not None
                or session.scalar(select(GlobalHeavyOperationLeaseRow).limit(1))
                is not None
            ):
                raise DevelopmentSessionConflict("development_session_busy")
            current = (
                None
                if row.session_generation is None
                else session.get(DevelopmentSessionRow, row.session_generation)
            )
            if current is not None:
                if current.phase != "uncertain":
                    raise DevelopmentSessionConflict(
                        "development_session_requires_review"
                    )
                current.phase, current.container_id = "reserved", None
                current.updated_at = timestamp(datetime.now(UTC))
            session.delete(row)
            session.commit()

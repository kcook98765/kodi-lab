"""One-shot restore's selected-session admission and exact successor handoff.

Call only inside the original restore's checked writer/dual-lease transaction.
This is not admission to resume an interrupted or historical restore.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
    require_session_admission,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.managed_version_slots import require_unique_version_profile
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.retained_development import (
    RetainedContainer,
    require_retained_container,
    retained_targets,
)


def require_restore_selection(
    session: Session, target_id: str, revision_id: str
) -> DevelopmentSessionRow | None:
    """Destructive restore requires the selected bound profile, when selected."""
    current = active_session(session)
    if current is None:
        if retained_targets(session):
            raise DevelopmentSessionConflict("retained_selection_required")
        return None
    if (
        current.phase != "bound"
        or current.target_id != target_id
        or current.runtime_revision_id != revision_id
    ):
        raise DevelopmentSessionConflict("development_session_identity_changed")
    return current


@dataclass(frozen=True)
class _Selection:
    generation: int
    request_id: str
    target_id: str
    runtime_revision_id: str
    workspace_id: str
    source_id: str
    container_id: str | None
    phase: str

    @classmethod
    def read(cls, row: DevelopmentSessionRow) -> _Selection:
        return cls(**{key: getattr(row, key) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RestoreSessionBinding:
    selection: _Selection | None
    policy: tuple[bool, bool, str | None]
    latest: int
    workspace: tuple[int, str, str, str, str, str] | None

    @classmethod
    def read(cls, session: Session) -> RestoreSessionBinding:
        current = active_session(session)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        workspace_identity = None
        if current is not None:
            workspace = session.get(AgentWorkspaceRow, current.workspace_id)
            source = session.get(DevelopmentSourceRow, current.source_id)
            if (
                workspace is None
                or source is None
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or workspace.development_source_id != source.id
                or workspace.source_addon_id != source.addon_id
            ):
                raise DevelopmentSessionConflict(
                    "development_session_selection_unavailable"
                )
            workspace_identity = (
                workspace.generation,
                workspace.development_source_id,
                workspace.source_addon_id,
                source.source_kind,
                source.relative_locator,
                source.addon_id,
            )
        if (
            any(
                session.scalar(select(model).limit(1)) is not None
                for model in (
                    ManagedStartReservationRow,
                    ManagedProvisionReservationRow,
                )
            )
            or session.scalar(
                select(DevelopmentSessionReplacementReviewRow)
                .where(
                    DevelopmentSessionReplacementReviewRow.state == "open",
                    DevelopmentSessionReplacementReviewRow.successor_generation.is_(
                        None
                    ),
                )
                .limit(1)
            )
            is not None
        ):
            raise DevelopmentSessionConflict("development_session_busy")
        return cls(
            None if current is None else _Selection.read(current),
            (
                policy is not None,
                False if policy is None else policy.retain_profiles,
                None if policy is None else policy.retained_omega_target_id,
            ),
            session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0,
            workspace_identity,
        )

    @classmethod
    def capture(
        cls,
        session: Session,
        records: Sequence[RetainedContainer],
        *,
        target_id: str,
        revision_id: str,
        container_id: str,
    ) -> RestoreSessionBinding:
        current = require_restore_selection(session, target_id, revision_id)
        require_unique_version_profile(
            session, records, target_id=target_id, revision_id=revision_id
        )
        require_retained_container(
            session, records, target_id, observed_container_id=container_id
        )
        require_session_admission(
            session, records, target_id=target_id, revision_id=revision_id
        )
        binding = cls.read(session)
        if current is not None and (
            current.container_id != container_id or binding.latest >= 256
        ):
            raise DevelopmentSessionConflict("development_session_identity_changed")
        return binding

    def check(self, session: Session) -> None:
        if self.read(session) != self:
            raise DevelopmentSessionConflict("development_session_identity_changed")

    def handoff(
        self, session: Session, operation_id: UUID, now: datetime
    ) -> RestoreSessionBinding:
        self.check(session)
        if self.selection is None:
            return self
        old = session.get(DevelopmentSessionRow, self.selection.generation)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert old is not None and policy is not None
        if old.phase != "bound" or self.latest >= 256:
            raise DevelopmentSessionConflict("development_session_identity_changed")
        successor = DevelopmentSessionRow(
            generation=self.latest + 1,
            request_id=str(uuid5(operation_id, "snapshot-restore-successor")),
            target_id=old.target_id,
            runtime_revision_id=old.runtime_revision_id,
            workspace_id=old.workspace_id,
            source_id=old.source_id,
            container_id=None,
            phase="reserved",
            created_at=timestamp(now),
            updated_at=timestamp(now),
        )
        session.add(successor)
        session.flush()
        old.phase, old.updated_at = "closed", timestamp(now)
        policy.active_generation = successor.generation
        session.flush()
        return self.read(session)

    def bound(self, container_id: str) -> RestoreSessionBinding:
        if self.selection is None:
            return self
        return replace(
            self,
            selection=replace(self.selection, container_id=container_id, phase="bound"),
        )

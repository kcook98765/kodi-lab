"""Small Web composition over durable session bindings and existing lifecycle."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kodi_lab.application.development_sessions import (
    DevelopmentSessionService,
    DevelopmentSessionStatus,
)
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatus
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.models import DevelopmentSessionRow, ManagedInstanceRow
from kodi_lab.storage.repositories import ManagedInstanceRepository, TargetRepository


class SessionGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)


class SessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latest_generation: int
    current: DevelopmentSessionStatus | None


class SessionTargetChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: UUID
    name: str
    kodi_version: str


class DevelopmentSessionWorkflow:
    def __init__(
        self,
        sessions: DevelopmentSessionService,
        lifecycle: ManagedRuntimeLifecycleService,
    ) -> None:
        self._sessions, self._lifecycle = sessions, lifecycle

    def view(self) -> SessionView:
        from kodi_lab.application.development_sessions import _status

        with self._sessions._sessions() as session:
            current = active_session(session)
            return SessionView(
                latest_generation=session.scalar(
                    select(func.max(DevelopmentSessionRow.generation))
                )
                or 0,
                current=None if current is None else _status(current),
            )

    def choices(self) -> tuple[SessionTargetChoice, ...]:
        from kodi_lab.runtime.lab import KNOWN_SELECTORS, canonical_id

        result = []
        with self._sessions._sessions() as session:
            for target_id in sorted(
                {canonical_id(selector) for selector in KNOWN_SELECTORS}, key=str
            ):
                instance = ManagedInstanceRepository(session).get(target_id)
                target = TargetRepository(session).get(target_id)
                if instance is None or target is None or target.kodi_version is None:
                    continue
                try:
                    self._sessions._runtime(
                        session, target_id, instance.runtime_revision_id
                    )
                except DevelopmentSessionConflict:
                    continue
                result.append(
                    SessionTargetChoice(
                        target_id=target_id,
                        name=target.name,
                        kodi_version=target.kodi_version,
                    )
                )
        return tuple(sorted(result, key=lambda item: (item.kodi_version, item.name)))

    def _binding(
        self, session: Session, generation: int, phase: str, target_id: UUID
    ) -> DevelopmentSessionRow:
        current = active_session(session)
        instance = session.get(ManagedInstanceRow, str(target_id))
        if (
            current is None
            or current.generation != generation
            or current.phase != phase
            or current.target_id != str(target_id)
            or instance is None
            or instance.runtime_revision_id != current.runtime_revision_id
        ):
            raise DevelopmentSessionConflict("development_session_identity_changed")
        return current

    def _confirm_container(self, generation: int, target_id: UUID) -> None:
        with self._sessions._sessions() as session:
            current = self._binding(session, generation, "bound", target_id)
            containers = self._sessions._inventory().containers
            if (
                len(containers) != 1
                or containers[0].managed_instance_id != current.target_id
                or containers[0].container_id != current.container_id
            ):
                raise DevelopmentSessionConflict("development_session_identity_changed")

    def act(
        self, action: Literal["prepare", "resume", "stop"], generation: int
    ) -> ManagedRuntimeStatus:
        current = self._sessions.status()
        if current is None or current.generation != generation:
            raise DevelopmentSessionConflict("development_session_generation_conflict")
        phase = "reserved" if action == "prepare" else "bound"

        def precondition(session: Session) -> None:
            self._binding(session, generation, phase, current.target_id)
            if action == "prepare":
                self._sessions._runtime(
                    session, current.target_id, current.runtime_revision_id
                )

        if action == "prepare":
            return self._lifecycle.provision(
                current.target_id, precondition=precondition
            )
        method = self._lifecycle.start if action == "resume" else self._lifecycle.stop
        return method(
            current.target_id,
            precondition=precondition,
            before_dispatch=lambda: self._confirm_container(
                generation, current.target_id
            ),
        )

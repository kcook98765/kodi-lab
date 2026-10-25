"""Shared, transaction-local single-session policy used before broker dispatch."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.broker.docker_inventory import ManagedContainerRecord
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedProvisionReservationRow,
)


class DevelopmentSessionConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def active_session(session: Session) -> DevelopmentSessionRow | None:
    policy = session.get(DevelopmentSessionPolicyRow, 1)
    if policy is None or policy.active_generation is None:
        return None
    current = session.get(DevelopmentSessionRow, policy.active_generation)
    if current is None or current.phase == "closed":
        raise DevelopmentSessionConflict("development_session_requires_review")
    return current


def require_session_admission(
    session: Session,
    records: Sequence[ManagedContainerRecord],
    *,
    target_id: str,
    revision_id: str,
    container_id: str | None = None,
    starting: bool = False,
) -> DevelopmentSessionRow | None:
    current = active_session(session)
    if current is None:
        return None
    if current.target_id != target_id or current.runtime_revision_id != revision_id:
        raise DevelopmentSessionConflict("development_session_other_target")
    if current.phase not in ({"bound"} if starting else {"reserved", "bound"}):
        raise DevelopmentSessionConflict("development_session_requires_review")
    if starting and current.container_id != container_id:
        raise DevelopmentSessionConflict("development_session_identity_changed")
    if current.container_id is None:
        if records:
            raise DevelopmentSessionConflict("development_session_existing_resource")
    elif len(records) != 1 or any(
        item.managed_instance_id != current.target_id
        or item.container_id != current.container_id
        or item.runtime_revision_id != current.runtime_revision_id
        for item in records
    ):
        raise DevelopmentSessionConflict("development_session_identity_changed")
    if session.scalar(select(ManagedProvisionReservationRow).limit(1)) is not None:
        raise DevelopmentSessionConflict("development_session_provision_pending")
    return current

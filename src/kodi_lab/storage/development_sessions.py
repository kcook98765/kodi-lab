"""Shared, transaction-local single-session policy used before broker dispatch."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    SessionSourceCopyAdmissionRow,
)

if TYPE_CHECKING:
    from kodi_lab.broker.docker_inventory import ManagedContainerRecord


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


def require_workspace_session_admission(
    session: Session,
    *,
    target_id: str,
    revision_id: str | None,
    workspace_id: str,
    source_id: str,
    container_id: str | None = None,
) -> DevelopmentSessionRow | None:
    """Narrow managed runtime work to the explicitly selected bound session.

    Call during the existing writer claim before reserving the typed mutation
    lease, and recheck exact observed identity before dispatch. This supplies no
    grant, deployment baseline, attachment, trust or permission of its own.
    Offline edits, historical receipt reads and external targets do not use it.
    """
    current = active_session(session)
    if current is None:
        return None
    if current.target_id != target_id:
        raise DevelopmentSessionConflict("development_session_other_target")
    if current.phase != "bound":
        raise DevelopmentSessionConflict("development_session_requires_review")
    if current.runtime_revision_id != revision_id or (
        container_id is not None and current.container_id != container_id
    ):
        raise DevelopmentSessionConflict("development_session_identity_changed")
    if current.workspace_id != workspace_id or current.source_id != source_id:
        raise DevelopmentSessionConflict("development_session_other_workspace")
    if (
        session.scalar(select(ManagedProvisionReservationRow).limit(1)) is not None
        or session.scalar(select(ManagedStartReservationRow).limit(1)) is not None
    ):
        raise DevelopmentSessionConflict("development_session_runtime_pending")
    return current


def require_source_session_admission(
    session: Session,
    *,
    target_id: str,
    revision_id: str,
    source_id: str,
    container_id: str | None,
    operation_id: str | None = None,
) -> DevelopmentSessionRow | None:
    """Narrow source work; a positive CID is mandatory at live dispatch.

    None supplies metadata-only admission before native observation. Guided copy
    children always supply a positive CID and retain their exact generation.
    """
    current = active_session(session)
    receipt = (
        None
        if operation_id is None
        else session.scalar(
            select(SessionSourceCopyAdmissionRow).where(
                SessionSourceCopyAdmissionRow.operation_id == operation_id
            )
        )
    )
    if receipt is not None and (
        current is None
        or receipt.session_generation != current.generation
        or any(
            getattr(receipt, name) != getattr(current, name)
            for name in (
                "target_id",
                "runtime_revision_id",
                "workspace_id",
                "source_id",
                "container_id",
            )
        )
        or receipt.target_id != target_id
        or receipt.runtime_revision_id != revision_id
        or receipt.source_id != source_id
        or receipt.container_id != container_id
    ):
        raise DevelopmentSessionConflict("session_source_copy_selection_changed")
    if current is None:
        return None
    return require_workspace_session_admission(
        session,
        target_id=target_id,
        revision_id=revision_id,
        workspace_id=current.workspace_id,
        source_id=source_id,
        container_id=container_id,
    )

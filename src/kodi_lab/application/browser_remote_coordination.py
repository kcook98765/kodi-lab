"""Process-local Slice 3A remote exclusion against durable mutation leases."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

_LOCK = RLock()
_ACTIVE: dict[UUID, UUID] = {}


class BrowserRemoteCoordinationConflict(RuntimeError):
    pass


class BrowserRemoteCoordinationLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserRemoteCoordinationHandle:
    instance_id: UUID
    invocation_id: UUID


def acquire_browser_remote(
    session_factory: sessionmaker[Session], instance_id: UUID
) -> BrowserRemoteCoordinationHandle:
    """Exclude another remote and observe current durable mutation authority."""
    with _LOCK:
        if instance_id in _ACTIVE:
            raise BrowserRemoteCoordinationConflict("Another remote action is active.")
        with session_factory() as session:
            row = session.get(ManagedInstanceMutationLeaseRow, str(instance_id))
            if row is not None:
                raise BrowserRemoteCoordinationConflict(
                    "A durable managed-instance mutation is active."
                )
        handle = BrowserRemoteCoordinationHandle(instance_id, uuid4())
        _ACTIVE[instance_id] = handle.invocation_id
        return handle


def release_browser_remote(handle: BrowserRemoteCoordinationHandle) -> None:
    with _LOCK:
        if _ACTIVE.get(handle.instance_id) != handle.invocation_id:
            raise BrowserRemoteCoordinationLost(
                "Remote coordination is no longer owned."
            )
        del _ACTIVE[handle.instance_id]


def require_no_active_browser_remote(instance_id: UUID) -> None:
    """Reject every durable mutation reservation while a remote is active."""
    with _LOCK:
        if instance_id in _ACTIVE:
            raise IntegrityError(
                "browser remote coordination conflict", {}, RuntimeError("conflict")
            )

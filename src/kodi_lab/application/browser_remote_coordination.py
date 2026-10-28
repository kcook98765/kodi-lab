"""Process-local Slice 3A remote exclusion against durable mutation leases."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import (
    ManagedRemoteGuardRow,
)

_LOCK = RLock()
_ACTIVE: dict[UUID, UUID] = {}
_PROCESS_ID = str(uuid4())


class BrowserRemoteCoordinationConflict(RuntimeError):
    pass


class BrowserRemoteCoordinationLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserRemoteCoordinationHandle:
    instance_id: UUID
    invocation_id: UUID
    lock_fd: int
    sessions: sessionmaker[Session]


def dispatch_lock(session_factory: sessionmaker[Session], instance_id: UUID) -> int:
    engine = session_factory.kw.get("bind")
    if engine is None or engine.url.database is None:
        raise BrowserRemoteCoordinationConflict("Durable input lock unavailable.")
    path = Path(engine.url.database).parent / (".input-" + str(instance_id) + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BrowserRemoteCoordinationConflict("Input lock identity changed.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException as error:
        os.close(fd)
        if isinstance(error, BlockingIOError):
            raise BrowserRemoteCoordinationConflict(
                "An input dispatcher is still active."
            ) from error
        raise


@contextmanager
def stop_dispatch_fence(
    session_factory: sessionmaker[Session], instance_id: UUID
) -> Iterator[None]:
    fd = dispatch_lock(session_factory, instance_id)
    try:
        yield
    finally:
        os.close(fd)


def acquire_browser_remote(
    session_factory: sessionmaker[Session], instance_id: UUID
) -> BrowserRemoteCoordinationHandle:
    """Exclude another remote and observe current durable mutation authority."""
    fd = dispatch_lock(session_factory, instance_id)
    handle = BrowserRemoteCoordinationHandle(instance_id, uuid4(), fd, session_factory)
    with _LOCK:
        if instance_id in _ACTIVE:
            os.close(fd)
            raise BrowserRemoteCoordinationConflict("Another remote action is active.")
        _ACTIVE[instance_id] = handle.invocation_id
    try:
        with session_factory.begin() as session:
            session.add(
                ManagedRemoteGuardRow(
                    instance_id=str(instance_id),
                    invocation_id=str(handle.invocation_id),
                    process_id=_PROCESS_ID,
                )
            )
    except BaseException as error:
        os.close(fd)
        with _LOCK:
            if _ACTIVE.get(instance_id) == handle.invocation_id:
                del _ACTIVE[instance_id]
        if isinstance(error, IntegrityError):
            raise BrowserRemoteCoordinationConflict(
                "Manual control or an unresolved input action owns input. "
                "Stop Kodi to recover unresolved input."
            ) from error
        raise
    return handle


def mark_remote_dispatch(
    handle: BrowserRemoteCoordinationHandle, container_id: str
) -> None:
    with handle.sessions.begin() as session:
        result = session.execute(
            update(ManagedRemoteGuardRow)
            .where(
                ManagedRemoteGuardRow.instance_id == str(handle.instance_id),
                ManagedRemoteGuardRow.invocation_id == str(handle.invocation_id),
            )
            .values(state="dispatching", container_id=container_id)
        )
        if int(getattr(result, "rowcount", 0)) != 1:
            raise BrowserRemoteCoordinationLost(
                "Remote ownership changed before dispatch."
            )


def release_browser_remote(
    handle: BrowserRemoteCoordinationHandle, *, unresolved: bool = False
) -> None:
    with _LOCK:
        if _ACTIVE.get(handle.instance_id) != handle.invocation_id:
            raise BrowserRemoteCoordinationLost(
                "Remote coordination is no longer owned."
            )
    try:
        with handle.sessions.begin() as session:
            criteria = (
                ManagedRemoteGuardRow.instance_id == str(handle.instance_id),
                ManagedRemoteGuardRow.invocation_id == str(handle.invocation_id),
            )
            if unresolved:
                result = session.execute(
                    update(ManagedRemoteGuardRow)
                    .where(*criteria)
                    .values(state="ambiguous")
                )
            else:
                result = session.execute(delete(ManagedRemoteGuardRow).where(*criteria))
            if int(getattr(result, "rowcount", 0)) != 1:
                raise BrowserRemoteCoordinationLost("Durable remote guard changed.")
    finally:
        os.close(handle.lock_fd)
        with _LOCK:
            if _ACTIVE.get(handle.instance_id) == handle.invocation_id:
                del _ACTIVE[handle.instance_id]


def retire_stopped_remote(
    session_factory: sessionmaker[Session], instance_id: UUID, container_id: str
) -> None:
    """Only called after exact broker Stop proves the native runtime is stopped.

    No timeout, process loss, or viewer action is a dispatch-completion proof.
    Stop is the existing supported recovery operation; input is never replayed.
    """
    with session_factory.begin() as session:
        session.execute(
            delete(ManagedRemoteGuardRow).where(
                ManagedRemoteGuardRow.instance_id == str(instance_id),
                (ManagedRemoteGuardRow.container_id == container_id)
                | ManagedRemoteGuardRow.container_id.is_(None),
            )
        )


def require_no_active_browser_remote(instance_id: UUID) -> None:
    """Reject every durable mutation reservation while a remote is active."""
    with _LOCK:
        if instance_id in _ACTIVE:
            raise IntegrityError(
                "browser remote coordination conflict", {}, RuntimeError("conflict")
            )

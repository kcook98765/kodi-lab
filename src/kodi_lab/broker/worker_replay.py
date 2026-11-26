"""Persist one installer replay guard with the application state owner's UID."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import literal, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.remote_lifecycle import DATABASE
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    RuntimeRecipeInstallationRow,
    RuntimeWorkerTerminalOperationRow,
)


def record_terminal_operation(factory: sessionmaker[Session], operation: UUID) -> None:
    with factory.begin() as session:
        # FULL durability plus a journal-directory sync in rollback journal mode.
        session.connection().exec_driver_sql("PRAGMA synchronous=EXTRA")
        installation = select(RuntimeRecipeInstallationRow.id).where(
            RuntimeRecipeInstallationRow.id == str(operation),
            RuntimeRecipeInstallationRow.state.in_(("ready", "failed")),
        )
        statement = insert(RuntimeWorkerTerminalOperationRow).from_select(
            ["operation_id"],
            select(literal(operation.bytes)).where(~installation.exists()),
        )
        session.execute(statement.on_conflict_do_nothing())


def _state_database(root: Path) -> tuple[Path, int, int]:
    database = root / DATABASE
    if not root.is_absolute() or root.resolve() != root:
        raise OSError("worker replay state root is unsafe")
    owner = root.lstat()
    if owner.st_uid == 0 or owner.st_uid != owner.st_gid:
        raise OSError("worker replay state owner is unsafe")
    uid, gid = owner.st_uid, owner.st_gid
    for directory in (root, database.parent):
        item = directory.lstat()
        if (
            not stat.S_ISDIR(item.st_mode)
            or stat.S_IMODE(item.st_mode) != 0o700
            or (item.st_uid, item.st_gid) != (uid, gid)
        ):
            raise OSError("worker replay state directory is unsafe")
    item = database.lstat()
    if (
        not stat.S_ISREG(item.st_mode)
        or stat.S_IMODE(item.st_mode) != 0o600
        or item.st_nlink != 1
        or (item.st_uid, item.st_gid) != (uid, gid)
    ):
        raise OSError("worker replay database is unsafe")
    return database, uid, gid


def _write_as_owner(root: Path, operation: UUID) -> None:
    database, uid, gid = _state_database(root)
    if (os.geteuid(), os.getegid()) != (uid, gid):
        raise OSError("worker replay writer must run as the state owner")
    engine = create_database_engine(
        f"sqlite:///{database.as_uri()}?mode=rw&timeout=1&uri=true"
    )
    try:
        record_terminal_operation(create_session_factory(engine), operation)
    finally:
        engine.dispose()


def persist_terminal_operation(root: Path, operation: UUID) -> None:
    if os.geteuid() != 0:
        _write_as_owner(root, operation)
        return
    _, uid, gid = _state_database(root)
    try:
        # The broker is multithreaded. Drop identity in the exec path without
        # running Python in a forked copy of the broker.
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "kodi_lab.broker.worker_replay",
                str(root),
                str(operation),
            ],
            user=uid,
            group=gid,
            extra_groups=[],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
    except subprocess.SubprocessError as error:
        raise OSError("worker replay transaction did not complete") from error


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    _write_as_owner(Path(sys.argv[1]), UUID(sys.argv[2]))

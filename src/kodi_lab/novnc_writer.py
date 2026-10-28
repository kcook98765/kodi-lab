"""Closed, one-shot SQLite writes as the human viewer's application state UID.

Keep this subprocess entry point independent of ORM, Docker and Web imports: its
startup and transaction must fit the existing bounded display admission window.
No listener, generic query protocol or agent capability is provided.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID


def main() -> None:
    root = Path(os.environ["KODI_LAB_DATA_ROOT"])
    owner = root.lstat()
    if (
        not root.is_absolute()
        or not stat.S_ISDIR(owner.st_mode)
        or stat.S_IMODE(owner.st_mode) != 0o700
        or os.geteuid() == 0
        or (os.geteuid(), os.getegid()) != (owner.st_uid, owner.st_gid)
    ):
        raise ValueError("state-owner identity required")
    raw = sys.stdin.read(16385)
    if len(raw) > 16384:
        raise ValueError("state transaction exceeds bounds")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"method", "args"}
        or not isinstance(value["args"], list)
    ):
        raise ValueError("invalid state transaction")
    args = value["args"]
    method = value["method"]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    database = root / "database/kodi-lab.db"
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=1)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            if method == "claim" and len(args) == 6:
                binding = args[3]
                identity = json.dumps(
                    [
                        binding["container"],
                        binding["image"],
                        binding["started"],
                        binding["identity"],
                    ],
                    separators=(",", ":"),
                )
                result = connection.execute(
                    "UPDATE novnc_sessions SET state='active',broker_id=? "
                    "WHERE instance_id=? AND attempt=? AND owner=? AND mode=? "
                    "AND identity_json=? AND state='pending' AND broker_id IS NULL "
                    "AND expires_at>?",
                    (
                        args[5],
                        str(UUID(args[0])),
                        str(UUID(args[1])),
                        args[2],
                        args[4],
                        identity,
                        now,
                    ),
                )
                if result.rowcount != 1:
                    raise ValueError("human viewer ownership unavailable")
            elif method == "started" and len(args) == 4:
                result = connection.execute(
                    "UPDATE novnc_sessions SET exec_id=? WHERE instance_id=? "
                    "AND attempt=? AND broker_id=? AND state='active'",
                    (args[3], str(UUID(args[0])), str(UUID(args[1])), args[2]),
                )
                if result.rowcount != 1:
                    raise ValueError("worker ownership revoked")
            elif method == "finish" and len(args) == 4 and type(args[3]) is bool:
                scope = (str(UUID(args[0])), str(UUID(args[1])), args[2])
                if args[3]:
                    connection.execute(
                        "DELETE FROM novnc_sessions WHERE instance_id=? "
                        "AND attempt=? AND broker_id=?",
                        scope,
                    )
                else:
                    connection.execute(
                        "UPDATE novnc_sessions SET state='blocked' WHERE instance_id=? "
                        "AND attempt=? AND broker_id=?",
                        scope,
                    )
            elif method == "revoke" and len(args) == 3:
                instance = str(UUID(args[0]))
                attempt = str(UUID(args[1])) if args[1] else None
                revoke_scope = (instance, attempt, attempt, args[2], args[2])
                predicate = (
                    "instance_id=? AND (? IS NULL OR attempt=?) "
                    "AND (? IS NULL OR owner=?)"
                )
                connection.execute(
                    "DELETE FROM novnc_sessions WHERE broker_id IS NULL AND "
                    + predicate,
                    revoke_scope,
                )
                connection.execute(
                    "UPDATE novnc_sessions SET state='revoking' "
                    "WHERE broker_id IS NOT NULL AND " + predicate,
                    revoke_scope,
                )
            elif method == "expire_pending" and not args:
                connection.execute(
                    "DELETE FROM novnc_sessions WHERE broker_id IS NULL "
                    "AND expires_at<=?",
                    (now,),
                )
            else:
                raise ValueError("state transaction denied")
        print('{"ok":true}')
    finally:
        connection.close()


if __name__ == "__main__":
    main()

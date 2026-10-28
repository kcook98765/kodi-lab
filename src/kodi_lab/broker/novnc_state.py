"""One-shot state-owner transactions for the privileged human display worker.

Internal subprocess protocol only: no listener, broker operation or MCP facility.
The root broker keeps read-only SQLite connections; writes run as the state UID.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.novnc_control import HumanViewerOwnership, timestamp
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import NovncSessionRow


class StateOwnerOwnership(HumanViewerOwnership):
    def __init__(self, settings: Settings, sessions: sessionmaker[Session]) -> None:
        super().__init__(sessions)
        self.settings = settings

    def _write(self, method: str, args: list[object]) -> None:
        owner = self.settings.data_root.lstat()
        if (
            not stat.S_ISDIR(owner.st_mode)
            or owner.st_uid == 0
            or owner.st_uid != owner.st_gid
            or stat.S_IMODE(owner.st_mode) != 0o700
        ):
            raise ValueError("unsafe viewer state owner")
        # subprocess changes identity before exec without a Python preexec hook
        # or a forked Python transaction in this multithreaded broker.
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", __name__],
            input=json.dumps({"method": method, "args": args}),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            user=owner.st_uid,
            group=owner.st_gid,
            extra_groups=[],
            env={
                **os.environ,
                "KODI_LAB_DATA_ROOT": str(self.settings.data_root),
                "KODI_LAB_RUNTIME_ROOT": str(self.settings.runtime_root),
            },
        )
        if result.returncode != 0 or result.stdout != '{"ok":true}\n':
            raise ValueError("viewer state transaction unconfirmed")

    def claim(
        self,
        instance: UUID,
        attempt: UUID,
        owner: str,
        binding: ViewBinding,
        mode: str,
        broker: str,
    ) -> None:
        self._write(
            "claim",
            [str(instance), str(attempt), owner, binding.__dict__, mode, broker],
        )

    def started(self, instance: UUID, attempt: UUID, broker: str, exec_id: str) -> None:
        self._write("started", [str(instance), str(attempt), broker, exec_id])

    def finish(self, instance: UUID, attempt: UUID, broker: str, proven: bool) -> None:
        self._write("finish", [str(instance), str(attempt), broker, proven])

    def revoke(
        self, instance: UUID, attempt: UUID | None = None, owner: str | None = None
    ) -> None:
        self._write("revoke", [str(instance), str(attempt) if attempt else None, owner])

    def expire_pending(self) -> None:
        with self.sessions() as db:
            pending = db.scalar(
                select(NovncSessionRow.instance_id)
                .where(
                    NovncSessionRow.broker_id.is_(None),
                    NovncSessionRow.expires_at <= timestamp(),
                )
                .limit(1)
            )
        if pending is not None:
            self._write("expire_pending", [])


def main() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    owner = settings.data_root.lstat()
    if os.geteuid() == 0 or (os.geteuid(), os.getegid()) != (
        owner.st_uid,
        owner.st_gid,
    ):
        raise ValueError("state-owner identity required")
    raw = sys.stdin.read(16385)
    if len(raw) > 16384:
        raise ValueError("state transaction exceeds bounds")
    value = json.loads(raw)
    if set(value) != {"method", "args"} or not isinstance(value["args"], list):
        raise ValueError("invalid state transaction")
    engine = create_database_engine(settings.database_url)
    authority = HumanViewerOwnership(create_session_factory(engine))
    args = value["args"]
    try:
        if value["method"] == "claim" and len(args) == 6:
            binding = args[3]
            authority.claim(
                UUID(args[0]),
                UUID(args[1]),
                args[2],
                ViewBinding(
                    binding["container"],
                    binding["image"],
                    binding["started"],
                    tuple(binding["identity"]),
                ),
                args[4],
                args[5],
            )
        elif value["method"] == "started" and len(args) == 4:
            authority.started(UUID(args[0]), UUID(args[1]), args[2], args[3])
        elif value["method"] == "finish" and len(args) == 4 and type(args[3]) is bool:
            authority.finish(UUID(args[0]), UUID(args[1]), args[2], args[3])
        elif value["method"] == "revoke" and len(args) == 3:
            authority.revoke(UUID(args[0]), UUID(args[1]) if args[1] else None, args[2])
        elif value["method"] == "expire_pending" and not args:
            authority.expire_pending()
        else:
            raise ValueError("state transaction denied")
        print('{"ok":true}')
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

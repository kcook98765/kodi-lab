"""Test-only closed schema templates and fd-bound scratch reclamation."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from sqlalchemy import create_engine

from kodi_lab.storage import metadata
from scripts.test_scratch import OwnedScratch as OwnedScratch


class CurrentSchemaTemplate:
    """An unseeded ORM template; never a substitute for an Alembic transition."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "schema.db"
        engine = create_engine(f"sqlite:///{self.path}")
        try:
            metadata.create_all(engine)
        finally:
            engine.dispose()
        self._check(self.path)
        self.path.chmod(0o400)
        self._digest = hashlib.sha256(self.path.read_bytes()).digest()

    @staticmethod
    def _check(path: Path) -> None:
        if any(
            Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
        ):
            raise RuntimeError("schema template must be closed without sidecars")
        with closing(
            sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("schema clone integrity check failed")

    def clone(self, destination: Path) -> None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        source = os.open(self.path, flags)
        try:
            observed = os.fstat(source)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o400
                or any(
                    Path(f"{self.path}{suffix}").exists()
                    for suffix in ("-wal", "-shm", "-journal")
                )
            ):
                raise RuntimeError("schema template identity changed")
            with os.fdopen(source, "rb", closefd=False) as stream:
                data = stream.read()
            if hashlib.sha256(data).digest() != self._digest:
                raise RuntimeError("schema template changed")
        finally:
            os.close(source)
        # Exclusive creation never overwrites another test's database or a link.
        with destination.open("xb") as target:
            target.write(data)
        try:
            self._check(destination)
        except BaseException:
            destination.unlink()
            raise

"""Private launch and retirement receipts for reviewed installer workers."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.managed.runtime import atomic, sync_dir

ContainerId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
CreateRejectionStatus = Literal[400, 404, 409]
RECEIPT_LIMIT = 128
LOG_TAIL_BYTES = 8192
MAX_RECEIPT_BYTES = 64 * 1024
RETENTION = timedelta(days=7)


class WorkerDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["ready", "failed", "reused", "not_started", "interrupted"]
    recorded_operation: UUID | None
    completed_at: str | None = Field(max_length=64)
    failure_code: str | None = Field(max_length=128)
    exit_code: int
    oom_killed: bool
    started_at: str = Field(max_length=64)
    finished_at: str = Field(max_length=64)
    log_tail: str = Field(max_length=LOG_TAIL_BYTES)


class WorkerReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: UUID
    nonce: UUID
    generation: Literal["matrix", "nexus", "omega", "piers"]
    engine: str = Field(min_length=1, max_length=128)
    image: Digest
    state_volume: str = Field(min_length=1, max_length=255)
    lock_volume: str = Field(min_length=1, max_length=255)
    recipe_digest: Digest
    container: ContainerId | None = None
    start_requested: bool = False
    diagnostics: WorkerDiagnostics | None = None
    create_rejected_status: CreateRejectionStatus | None = None


class WorkerReceipts:
    """Files on the broker socket volume, writable by the broker identity."""

    def __init__(
        self,
        runtime_root: Path,
        preserve_terminal_identity: Callable[[WorkerReceipt], None],
    ) -> None:
        self.root = runtime_root / "reviewed-worker-receipts"
        self._preserve_terminal_identity = preserve_terminal_identity

    def directory(self, name: Literal["pending", "retired"]) -> Path:
        runtime = self.root.parent.lstat()
        if (
            not stat.S_ISDIR(runtime.st_mode)
            or runtime.st_uid != os.geteuid()
            or stat.S_IMODE(runtime.st_mode) not in {0o700, 0o750}
        ):
            raise OSError("worker receipt parent is unsafe")
        for path in (self.root, self.root / name):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                sync_dir(path.parent)
            entry = path.lstat()
            if (
                entry.st_uid != os.geteuid()
                or not stat.S_ISDIR(entry.st_mode)
                or stat.S_IMODE(entry.st_mode) != 0o700
            ):
                raise OSError("worker receipt directory is unsafe")
        return self.root / name

    def pending(self) -> list[Path]:
        return self._entries(self.directory("pending"))

    def _pending_path(self, operation: UUID) -> Path:
        return self.directory("pending") / f"{operation.hex}.json"

    def pending_receipt(self, operation: UUID) -> WorkerReceipt | None:
        try:
            return self.read(self._pending_path(operation))
        except FileNotFoundError:
            return None

    @staticmethod
    def _entries(directory: Path) -> list[Path]:
        return sorted(
            path
            for path in directory.iterdir()
            if re.fullmatch(r"[0-9a-f]{32}\.json", path.name)
        )

    def save(self, receipt: WorkerReceipt) -> Path:
        path = self._pending_path(receipt.operation)
        atomic(path, receipt.model_dump_json().encode())
        sync_dir(self.root)
        sync_dir(self.root.parent)
        return path

    @staticmethod
    def read(path: Path, *, durable: bool = False) -> WorkerReceipt:
        with os.fdopen(
            os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
        ) as stream:
            entry = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(entry.st_mode)
                or entry.st_uid != os.geteuid()
                or entry.st_nlink != 1
                or stat.S_IMODE(entry.st_mode) != 0o600
                or entry.st_size > MAX_RECEIPT_BYTES
            ):
                raise OSError("worker receipt file is unsafe")
            receipt = WorkerReceipt.model_validate_json(
                stream.read(MAX_RECEIPT_BYTES + 1)
            )
            if durable:
                os.fsync(stream.fileno())
        if path.stem != receipt.operation.hex:
            raise ValueError("worker receipt operation does not match its path")
        return receipt

    def confirm_terminal(self, path: Path, expected: WorkerReceipt) -> None:
        if self.read(path, durable=True) != expected:
            raise OSError("worker receipt changed before completion")
        # A previous rename or mkdir can be visible after an unsuccessful fsync.
        for directory in (path.parent, self.root, self.root.parent):
            sync_dir(directory)
        self._preserve_terminal_identity(expected)

    def finish(self, path: Path) -> None:
        retired = self.directory("retired")
        self.trim(limit=RECEIPT_LIMIT - 1)
        # Touch before rename to measure retention from verified retirement.
        os.utime(path, None, follow_symlinks=False)
        path.replace(retired / path.name)
        sync_dir(retired)
        sync_dir(path.parent)

    def contains_retired(self, operation: UUID) -> bool:
        return (self.directory("retired") / f"{operation.hex}.json").exists()

    def trim(self, *, limit: int = RECEIPT_LIMIT) -> None:
        retired = self.directory("retired")
        now = datetime.now(UTC).timestamp()
        entries = sorted(
            ((path.lstat().st_mtime, path) for path in self._entries(retired)),
            reverse=True,
        )
        changed = False
        for index, (updated, path) in enumerate(entries):
            if index >= limit or now - updated >= RETENTION.total_seconds():
                # Earlier brokers could retire a receipt without an independent
                # replay guard. Preserve that identity before evicting it.
                self._preserve_terminal_identity(self.read(path))
                path.unlink()
                changed = True
        if changed:
            sync_dir(retired)

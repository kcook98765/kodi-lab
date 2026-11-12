"""Bounded private source transfer; existing project intake remains authoritative."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MAX_SOURCE_ARCHIVE_BYTES = 4 * 1024 * 1024
SOURCE_CHUNK_BYTES = 32 * 1024
TRANSFER_RETENTION = timedelta(hours=24)
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class ProjectTransferError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProjectTransferInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(ge=1, le=MAX_SOURCE_ARCHIVE_BYTES)
    action: Literal["write", "status", "cancel"] = "write"
    offset: int = Field(default=0, ge=0, lt=MAX_SOURCE_ARCHIVE_BYTES)
    chunk_base64: str = Field(default="", max_length=43700)


class ProjectTransferStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    archive_sha256: str
    archive_size: int
    state: Literal["receiving", "ready", "cancelled", "expired", "failed"]
    next_offset: int
    chunk_bytes: int = SOURCE_CHUNK_BYTES
    expires_at: datetime


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_size: int = Field(ge=1, le=MAX_SOURCE_ARCHIVE_BYTES)
    expires_at: AwareDatetime
    state: Literal["receiving", "ready", "cancelled", "expired", "failed"]


def _read(directory: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, _READ, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise ProjectTransferError("project_transfer_requires_review")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) != info.st_size:
            raise ProjectTransferError("project_transfer_requires_review")
        return value
    finally:
        os.close(fd)


def _publish(directory: int, name: str, value: bytes) -> None:
    # An interrupted temporary is retained and fences further publication.
    temporary = "." + name + ".tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        view = memoryview(value)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short source transfer write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def _receipt(directory: int) -> _Receipt:
    return _Receipt.model_validate_json(_read(directory, "receipt.json", 2048))


def _record(directory: int, receipt: _Receipt) -> None:
    _publish(directory, "receipt.json", receipt.model_dump_json().encode())


@contextmanager
def _locked(
    root: Path, authorize: Callable[[], None], lock_name: str = "transfer.lock"
) -> Iterator[int]:
    from kodi_lab.application.agent_projects import _private_directory

    try:
        with _private_directory(root, "agent-projects", "transfers") as directory:
            lock = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(lock)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != 0
                ):
                    raise ProjectTransferError("project_transfer_requires_review")
                fcntl.flock(lock, fcntl.LOCK_EX)
                authorize()
                yield directory
            finally:
                os.close(lock)
    except (OSError, ValueError) as error:
        raise ProjectTransferError("project_transfer_requires_review") from error


def _contents(directory: int, receipt: _Receipt) -> tuple[int, list[bytes]]:
    chunks = []
    offset = 0
    expected = {"receipt.json"}
    while offset < receipt.archive_size:
        name = f"{offset:08d}.chunk"
        try:
            value = _read(directory, name, SOURCE_CHUNK_BYTES)
        except FileNotFoundError:
            break
        if len(value) != min(SOURCE_CHUNK_BYTES, receipt.archive_size - offset):
            raise ProjectTransferError("project_transfer_requires_review")
        chunks.append(value)
        offset += len(value)
        expected.add(name)
    if set(os.listdir(directory)) != expected:
        raise ProjectTransferError("project_transfer_requires_review")
    return offset, chunks


def _discard(
    directory: int, receipt: _Receipt, state: Literal["cancelled", "expired"]
) -> _Receipt:
    receipt = receipt.model_copy(update={"state": state})
    _record(directory, receipt)  # Durable tombstone precedes deleting exact chunks.
    for offset in range(0, receipt.archive_size, SOURCE_CHUNK_BYTES):
        name = f"{offset:08d}.chunk"
        try:
            _read(directory, name, SOURCE_CHUNK_BYTES)
        except FileNotFoundError:
            continue
        os.unlink(name, dir_fd=directory)
    os.fsync(directory)
    return receipt


def transfer_source(
    root: Path,
    client_id: UUID,
    operation: ProjectTransferInput,
    *,
    authorize: Callable[[], None],
    now: datetime | None = None,
) -> ProjectTransferStatus:
    from kodi_lab.application.agent_projects import _private_directory

    now = now or datetime.now(UTC)
    key = f"{client_id}-{operation.request_id}"
    with _locked(root, authorize) as transfers:
        if key not in os.listdir(transfers):
            if operation.action != "write" or operation.offset != 0:
                raise ProjectTransferError("project_transfer_not_found")
            names = [
                n
                for n in os.listdir(transfers)
                if n not in {"transfer.lock", "import.lock"}
            ]
            if (
                len(names) >= 32
                or sum(n.startswith(str(client_id) + "-") for n in names) >= 4
            ):
                raise ProjectTransferError("project_transfer_limit")
            if (
                os.fstatvfs(transfers).f_bavail * os.fstatvfs(transfers).f_frsize
                < 2 * 1024**3 + MAX_SOURCE_ARCHIVE_BYTES
            ):
                raise ProjectTransferError("project_transfer_storage_full")
            os.mkdir(key, 0o700, dir_fd=transfers)
            os.fsync(transfers)
            with _private_directory(
                root, "agent-projects", "transfers", key
            ) as directory:
                _record(
                    directory,
                    _Receipt(
                        archive_sha256=operation.archive_sha256,
                        archive_size=operation.archive_size,
                        expires_at=now + TRANSFER_RETENTION,
                        state="receiving",
                    ),
                )
        with _private_directory(root, "agent-projects", "transfers", key) as directory:
            receipt = _receipt(directory)
            if (receipt.archive_sha256, receipt.archive_size) != (
                operation.archive_sha256,
                operation.archive_size,
            ):
                raise ProjectTransferError("request_id_conflict")
            if operation.action == "cancel":
                receipt = _discard(directory, receipt, "cancelled")
            elif receipt.state in {"receiving", "ready"} and now >= receipt.expires_at:
                receipt = _discard(directory, receipt, "expired")
            if receipt.state in {"cancelled", "expired"}:
                # Resume cleanup after an interrupted cancellation/expiry.
                receipt = _discard(
                    directory,
                    receipt,
                    "cancelled" if receipt.state == "cancelled" else "expired",
                )
                return ProjectTransferStatus(
                    request_id=operation.request_id,
                    **receipt.model_dump(),
                    next_offset=0,
                )
            offset, chunks = _contents(directory, receipt)
            if operation.action == "write":
                if receipt.state == "failed":
                    raise ProjectTransferError("project_transfer_failed")
                try:
                    value = base64.b64decode(operation.chunk_base64, validate=True)
                except binascii.Error as error:
                    raise ProjectTransferError(
                        "project_package_encoding_invalid"
                    ) from error
                if (
                    operation.offset % SOURCE_CHUNK_BYTES
                    or len(value)
                    != min(SOURCE_CHUNK_BYTES, receipt.archive_size - operation.offset)
                    or not value
                ):
                    raise ProjectTransferError("project_transfer_chunk_invalid")
                if operation.offset > offset:
                    raise ProjectTransferError("project_transfer_offset_conflict")
                if operation.offset < offset:
                    if (
                        _read(
                            directory,
                            f"{operation.offset:08d}.chunk",
                            SOURCE_CHUNK_BYTES,
                        )
                        != value
                    ):
                        raise ProjectTransferError("request_id_conflict")
                else:
                    _publish(directory, f"{offset:08d}.chunk", value)
                    chunks.append(value)
                    offset += len(value)
                if offset == receipt.archive_size:
                    if (
                        hashlib.sha256(b"".join(chunks)).hexdigest()
                        != receipt.archive_sha256
                    ):
                        _record(
                            directory, receipt.model_copy(update={"state": "failed"})
                        )
                        raise ProjectTransferError("project_package_digest_mismatch")
                    receipt = receipt.model_copy(update={"state": "ready"})
                    _record(directory, receipt)
            return ProjectTransferStatus(
                request_id=operation.request_id,
                **receipt.model_dump(),
                next_offset=offset,
            )


def read_source_transfer(
    root: Path,
    client_id: UUID,
    request_id: UUID,
    digest: str,
    *,
    authorize: Callable[[], None],
) -> bytes:
    from kodi_lab.application.agent_projects import _private_directory

    key = f"{client_id}-{request_id}"
    with _locked(root, authorize) as transfers:
        if key not in os.listdir(transfers):
            raise ProjectTransferError("project_transfer_not_found")
        with _private_directory(root, "agent-projects", "transfers", key) as directory:
            receipt = _receipt(directory)
            if receipt.state != "ready" or datetime.now(UTC) >= receipt.expires_at:
                raise ProjectTransferError("project_transfer_not_ready")
            offset, chunks = _contents(directory, receipt)
            value = b"".join(chunks)
            if (
                offset != receipt.archive_size
                or receipt.archive_sha256 != digest
                or hashlib.sha256(value).hexdigest() != digest
            ):
                raise ProjectTransferError("project_package_digest_mismatch")
            return value


@contextmanager
def project_import_lock(root: Path, authorize: Callable[[], None]) -> Iterator[None]:
    """Serialize expanded source validation/publication to bound process memory."""
    with _locked(root, authorize, "import.lock"):
        yield

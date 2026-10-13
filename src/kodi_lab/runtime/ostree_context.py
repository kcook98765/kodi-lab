"""Race-resistant, operation-bound snapshot of verified trees for Docker.

The compressed context lives in a sealed anonymous Linux memfd, never a path
or a persistent binary cache. Hashes are computed over the bytes actually
copied, not a preceding pass over mutable paths. Docker receives only the
sealed descriptor. This module grants no installation or eligibility authority.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import os
import stat
import tarfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID

from kodi_lab.runtime.ostree import (
    MAX_STAGE_BYTES,
    MAX_STAGE_ENTRIES,
    AcquiredOstree,
    OstreeAcquisitionError,
    _canonical,
    _directory,
    _safe_link,
)
from kodi_lab.runtime.recipes import load_inactive_ostree_recipe

MAX_CONTEXT_BYTES = 1024**3
MAX_CAPTURE_SECONDS = 300
_SEALS = (
    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
)


class ContextError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedContext:
    stream: BinaryIO
    operation_id: UUID
    recipe_digest: str
    acquisition_digest: str
    digest: str
    size: int

    def assert_sealed(self, operation_id: UUID) -> None:
        if (
            operation_id != self.operation_id
            or fcntl.fcntl(self.stream.fileno(), fcntl.F_GET_SEALS) != _SEALS
            or os.fstat(self.stream.fileno()).st_size != self.size
        ):
            raise ContextError("invalid_sealed_handoff")


class _BoundedWriter:
    def __init__(self, stream: BinaryIO, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, payload: bytes) -> int:
        self.size += len(payload)
        if self.size > MAX_CONTEXT_BYTES or time.monotonic() > self.deadline:
            raise ContextError("context_resource_bound")
        self.digest.update(payload)
        return self.stream.write(payload)

    def flush(self) -> None:
        self.stream.flush()


class _HashReader:
    def __init__(self, fd: int, deadline: float) -> None:
        self.fd, self.deadline = fd, deadline
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() > self.deadline:
            raise ContextError("context_timeout")
        chunk = os.read(self.fd, min(size if size >= 0 else 1024**2, 1024**2))
        self.size += len(chunk)
        self.digest.update(chunk)
        return chunk


def _capture_tree(
    archive: tarfile.TarFile, acquired: AcquiredOstree, role: str, deadline: float
) -> tuple[str, int, int]:
    """Mirror Phase 2's canonical inventory while copying through nofollow FDs."""
    digest = hashlib.sha256()
    count = size = 0

    def walk(fd: int, prefix: str) -> None:
        nonlocal count, size
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            relative = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_uid != os.geteuid() or info.st_mode & (
                stat.S_ISUID | stat.S_ISGID
            ):
                raise ContextError("unsafe_payload_owner_or_mode")
            count += 1
            if count > MAX_STAGE_ENTRIES or time.monotonic() > deadline:
                raise ContextError("context_resource_bound")
            member = tarfile.TarInfo(f"payload/{role}/{relative}")
            member.uid = member.gid = 0
            member.mode = stat.S_IMODE(info.st_mode)
            record: list[object]
            if stat.S_ISDIR(info.st_mode):
                member.type = tarfile.DIRTYPE
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ContextError("payload_changed")
                    record = [relative, "directory", member.mode]
                    digest.update(_canonical(record) + b"\n")
                    archive.addfile(member)
                    walk(child, relative + "/")
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=fd)
                _safe_link(relative, target, role)
                member.type, member.linkname = tarfile.SYMTYPE, target
                archive.addfile(member)
                digest.update(_canonical([relative, "symlink", target]) + b"\n")
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_STAGE_BYTES - size:
                    raise ContextError("context_resource_bound")
                child = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
                try:
                    opened = os.fstat(child)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or (
                            opened.st_dev,
                            opened.st_ino,
                            opened.st_size,
                            opened.st_mode,
                        )
                        != (info.st_dev, info.st_ino, info.st_size, info.st_mode)
                    ):
                        raise ContextError("unsafe_payload_file")
                    reader = _HashReader(child, deadline)
                    member.size = info.st_size
                    archive.addfile(member, reader)
                    if os.read(child, 1) or reader.size != info.st_size:
                        raise ContextError("payload_changed")
                    size += reader.size
                    if size > MAX_STAGE_BYTES:
                        raise ContextError("context_resource_bound")
                    record = [
                        relative,
                        "file",
                        member.mode,
                        reader.size,
                        reader.digest.hexdigest(),
                    ]
                    digest.update(_canonical(record) + b"\n")
                finally:
                    os.close(child)
            else:
                raise ContextError("unsupported_payload_entry")

    fd = _directory(acquired.stage / "checkout" / role)
    try:
        walk(fd, "")
    finally:
        os.close(fd)
    return digest.hexdigest(), count, size


@contextmanager
def verified_context(
    acquired: AcquiredOstree, operation_id: UUID, *, entries: Mapping[str, bytes]
) -> Iterator[VerifiedContext]:
    """Trusted assembly supplies a fixed allowlist of wheel assets/support DEBs.

    This is an internal API, not a request-supplied file or Dockerfile interface.
    The public assembly entrypoint derives every entry from fixed configuration.
    """
    acquired.assert_handoff(operation_id)
    definition = load_inactive_ostree_recipe()
    expected = definition.recipe.provider.components
    if tuple((x.role, x.ref, x.commit) for x in acquired.evidence.components) != tuple(
        (x.role, x.ref, x.commit) for x in expected
    ):
        raise ContextError("component_identity_mismatch")
    fd = os.memfd_create(
        "kodi-lab-verified-context", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        with os.fdopen(os.dup(fd), "wb") as output:
            writer = _BoundedWriter(output, time.monotonic() + MAX_CAPTURE_SECONDS)
            with (
                gzip.GzipFile(
                    fileobj=writer, mode="wb", filename="", mtime=0, compresslevel=1
                ) as compressed,
                tarfile.open(fileobj=compressed, mode="w|") as archive,
            ):
                for component in acquired.evidence.components:
                    actual = _capture_tree(
                        archive, acquired, component.role, writer.deadline
                    )
                    if actual != (
                        component.inventory_sha256,
                        component.entry_count,
                        component.content_bytes,
                    ):
                        raise ContextError("verified_payload_tampered")
                for name, payload in sorted(entries.items()):
                    if name.startswith("/") or any(
                        p in ("", ".", "..") for p in name.split("/")
                    ):
                        raise ContextError("unsafe_context_asset")
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(payload), 0o644
                    archive.addfile(info, io.BytesIO(payload))
        acquired.assert_handoff(operation_id)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as stream:
            result = VerifiedContext(
                stream,
                operation_id,
                definition.digest,
                "sha256:"
                + hashlib.sha256(acquired.evidence.canonical_bytes()).hexdigest(),
                "sha256:" + writer.digest.hexdigest(),
                writer.size,
            )
            result.assert_sealed(operation_id)
            yield result
    except (OSError, OstreeAcquisitionError) as error:
        raise ContextError("invalid_acquisition_handoff") from error
    finally:
        os.close(fd)

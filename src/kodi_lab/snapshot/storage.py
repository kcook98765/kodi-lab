"""Bounded deterministic tar capture and immutable atomic publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from errno import ENOENT
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal, cast
from uuid import UUID

MAX_ARCHIVE_BYTES = 16 * 1024**3
MAX_FILE_BYTES = 8 * 1024**3
MAX_MEMBERS = 250_000
MAX_RELATIVE_PATH_BYTES = 512
MAX_MANIFEST_BYTES = 64 * 1024**2
MIN_FREE_BYTES = 2 * 1024**3
STORAGE_RESERVE_BYTES = 1 * 1024**3
KODI_ARCHIVE_UID = 1000
KODI_ARCHIVE_GID = 1000
MANIFEST_VERSION = 1

_PROFILE_ROOT = ".kodi"
_ALLOWED_TOP_LEVEL = frozenset({"userdata", "addons", "fixture-media", ".smb"})
_EXCLUDED_TOP_LEVEL = frozenset({"temp"})
_EMPTY_ONLY_SCAFFOLDING = frozenset({"media", "system"})
_EXCLUDED_PREFIXES = (
    PurePosixPath("addons/packages"),
    PurePosixPath("addons/temp"),
    PurePosixPath("userdata/Thumbnails"),
)


class SnapshotStorageError(RuntimeError):
    """Trusted snapshot storage rejected or could not publish an artifact."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class SnapshotArtifactDeletionError(RuntimeError):
    """The fixed UUID artifact subtree could not be removed safely."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class SnapshotCaptureContext:
    snapshot_id: UUID
    source_managed_instance_id: UUID
    runtime_revision_id: UUID
    kodi_release: str
    captured_fixture_definition_id: UUID | None
    captured_at: datetime
    prior_runtime_state: Literal["running", "stopped"]

    def __post_init__(self) -> None:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("capture timestamp must be timezone-aware")
        if not self.kodi_release or len(self.kodi_release) > 32:
            raise ValueError("Kodi release identity is invalid")


@dataclass(frozen=True, slots=True)
class SnapshotSourceMember:
    path: str
    member_type: Literal["directory", "file"]
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class SnapshotMemberRecord:
    path: str
    member_type: Literal["directory", "file"]
    size: int
    mode: int
    sha256: str | None

    def document(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "type": self.member_type,
        }


@dataclass(frozen=True, slots=True)
class SnapshotArtifactIdentity:
    snapshot_id: UUID
    archive_digest: str
    manifest_digest: str
    archive_size: int
    member_count: int


class _IteratorReader:
    """Small file-like adapter over Docker's archive chunk iterator."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._iterator = iter(chunks)
        self._buffer = bytearray()
        self._closed = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed stream")
        if size == 0:
            return b""
        if size < 0:
            for chunk in self._iterator:
                self._buffer.extend(chunk)
            result = bytes(self._buffer)
            self._buffer.clear()
            return result
        while len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._iterator))
            except StopIteration:
                break
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self._closed = True
        self._buffer.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> _IteratorReader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _BoundedHashingWriter:
    def __init__(self, raw: IO[bytes]) -> None:
        self._raw = raw
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        new_size = self.size + len(data)
        if new_size > MAX_ARCHIVE_BYTES:
            raise SnapshotStorageError(
                "snapshot_archive_too_large",
                "Snapshot archive exceeds the fixed size limit.",
            )
        written = self._raw.write(data)
        if written != len(data):
            raise OSError("short snapshot archive write")
        self._hash.update(data)
        self.size = new_size
        return written

    def flush(self) -> None:
        self._raw.flush()

    def tell(self) -> int:
        return self.size

    @property
    def digest(self) -> str:
        return f"sha256:{self._hash.hexdigest()}"


class _HashingReader:
    def __init__(self, raw: IO[bytes], expected_size: int) -> None:
        self._raw = raw
        self._remaining = expected_size
        self._hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        bounded = self._remaining if size < 0 else min(size, self._remaining)
        data = self._raw.read(bounded)
        if not data:
            raise SnapshotStorageError(
                "snapshot_source_changed",
                "Snapshot source changed while it was being captured.",
            )
        self._remaining -= len(data)
        self._hash.update(data)
        return data

    @property
    def digest(self) -> str:
        if self._remaining:
            raise SnapshotStorageError(
                "snapshot_source_changed",
                "Snapshot source changed while it was being captured.",
            )
        return f"sha256:{self._hash.hexdigest()}"


def snapshot_directory(data_root: Path, snapshot_id: UUID) -> Path:
    """Derive the only allowed final artifact location from a UUID."""

    return data_root / "snapshots" / str(snapshot_id)


def snapshot_artifact_presence(data_root: Path, snapshot_id: UUID) -> str:
    """Inspect only the fixed UUID path without following its final component."""

    path = snapshot_directory(data_root, snapshot_id)
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "inconsistent"
    return "directory" if stat.S_ISDIR(metadata.st_mode) else "inconsistent"


def _unlink_snapshot_tree(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise SnapshotArtifactDeletionError(
                "snapshot_delete_unsafe",
                "Snapshot artifact contains an unsafe directory entry.",
            )
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise SnapshotArtifactDeletionError(
                            "snapshot_delete_race",
                            "Snapshot artifact changed during deletion.",
                        )
                    _unlink_snapshot_tree(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                os.unlink(name, dir_fd=directory_fd)
        except SnapshotArtifactDeletionError:
            raise
        except OSError as error:
            raise SnapshotArtifactDeletionError(
                "snapshot_delete_failed",
                "Snapshot artifact could not be removed safely.",
            ) from error


def delete_snapshot_artifact(
    *,
    data_root: Path,
    snapshot_id: UUID,
    owner_uid: int = 0,
    owner_gid: int,
) -> bool:
    """Remove one descriptor-relative UUID subtree; return whether it existed."""

    snapshot_root = data_root / "snapshots"
    try:
        root_metadata = snapshot_root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != owner_uid
            or root_metadata.st_gid != owner_gid
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise SnapshotArtifactDeletionError(
                "snapshot_delete_storage_unsafe",
                "Snapshot storage root identity is not trusted.",
            )
        root_fd = os.open(snapshot_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except SnapshotArtifactDeletionError:
        raise
    except OSError as error:
        raise SnapshotArtifactDeletionError(
            "snapshot_delete_storage_unsafe",
            "Snapshot storage root identity is not trusted.",
        ) from error
    name = str(snapshot_id)
    target_fd: int | None = None
    try:
        try:
            target_metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as error:
            if error.errno == ENOENT:
                return False
            raise
        if (
            not stat.S_ISDIR(target_metadata.st_mode)
            or target_metadata.st_uid != owner_uid
            or target_metadata.st_gid != owner_gid
            or stat.S_IMODE(target_metadata.st_mode) & 0o022
        ):
            raise SnapshotArtifactDeletionError(
                "snapshot_delete_target_unsafe",
                "Snapshot artifact directory identity is not trusted.",
            )
        target_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        opened = os.fstat(target_fd)
        if (opened.st_dev, opened.st_ino) != (
            target_metadata.st_dev,
            target_metadata.st_ino,
        ):
            raise SnapshotArtifactDeletionError(
                "snapshot_delete_race", "Snapshot artifact changed during deletion."
            )
        _unlink_snapshot_tree(target_fd)
        os.close(target_fd)
        target_fd = None
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as error:
            if error.errno == ENOENT:
                return True
            raise
        raise SnapshotArtifactDeletionError(
            "snapshot_delete_absence_unproven",
            "Snapshot artifact absence could not be proven.",
        )
    except SnapshotArtifactDeletionError:
        raise
    except OSError as error:
        raise SnapshotArtifactDeletionError(
            "snapshot_delete_failed",
            "Snapshot artifact could not be removed safely.",
        ) from error
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(root_fd)


def _normalized_source_path(name: str) -> str | None:
    if "\x00" in name or name.startswith("/"):
        raise SnapshotStorageError(
            "unsafe_snapshot_member", "Snapshot source contains an unsafe path."
        )
    raw = PurePosixPath(name)
    if raw.as_posix() != name:
        raise SnapshotStorageError(
            "unsafe_snapshot_member", "Snapshot source contains an unsafe path."
        )
    if any(part in {"", ".", ".."} for part in raw.parts):
        raise SnapshotStorageError(
            "unsafe_snapshot_member", "Snapshot source contains an unsafe path."
        )
    parts = raw.parts
    if parts == (_PROFILE_ROOT,):
        return None
    if not parts or parts[0] != _PROFILE_ROOT:
        raise SnapshotStorageError(
            "unsafe_snapshot_member", "Snapshot source root is not canonical."
        )
    relative = PurePosixPath(*parts[1:])
    value = relative.as_posix()
    if len(value.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise SnapshotStorageError(
            "snapshot_path_too_long",
            "Snapshot source contains a path longer than the fixed limit.",
        )
    return value


def _is_log_or_runtime_artifact(path: PurePosixPath) -> bool:
    if len(path.parts) != 1:
        return False
    name = path.name.lower()
    return (
        name
        in {
            "kodi.log",
            "kodi.old.log",
            "kodi-stdout.log",
            "xvfb.log",
            "kodi.pid",
            "kodi.lock",
            "core",
        }
        or name.startswith("crashlog")
        or name.startswith("core.")
        or name.endswith((".pid", ".lock"))
    )


def _classify_path(
    path_text: str,
) -> Literal["include", "exclude", "recognized_empty_only"]:
    path = PurePosixPath(path_text)
    top = path.parts[0]
    if top in _EXCLUDED_TOP_LEVEL or _is_log_or_runtime_artifact(path):
        return "exclude"
    if top in _EMPTY_ONLY_SCAFFOLDING:
        if len(path.parts) == 1:
            return "recognized_empty_only"
        raise SnapshotStorageError(
            "nonempty_snapshot_scaffolding",
            "Recognized Kodi runtime scaffolding contains state.",
        )
    if top not in _ALLOWED_TOP_LEVEL:
        raise SnapshotStorageError(
            "unknown_snapshot_profile_state",
            "Kodi profile contains unclassified top-level state.",
        )
    if any(path == prefix or prefix in path.parents for prefix in _EXCLUDED_PREFIXES):
        return "exclude"
    if (
        len(path.parts) >= 3
        and path.parts[0:2] == ("userdata", "Database")
        and path.name.startswith("Textures")
        and ".db" in path.name
    ):
        return "exclude"
    return "include"


def _safe_source_member(info: tarfile.TarInfo) -> SnapshotSourceMember | None:
    path = _normalized_source_path(info.name.rstrip("/"))
    if path is None:
        if not info.isdir():
            raise SnapshotStorageError(
                "unsafe_snapshot_member", "Snapshot profile root is not a directory."
            )
        return None
    if info.islnk() or info.issym():
        raise SnapshotStorageError(
            "unsafe_snapshot_member", "Snapshot source contains a link."
        )
    if not (info.isfile() or info.isdir()):
        raise SnapshotStorageError(
            "unsafe_snapshot_member",
            "Snapshot source contains an unsupported filesystem object.",
        )
    classification = _classify_path(path)
    if classification == "exclude":
        return None
    if classification == "recognized_empty_only":
        if not info.isdir():
            raise SnapshotStorageError(
                "nonempty_snapshot_scaffolding",
                "Recognized Kodi runtime scaffolding contains state.",
            )
        return None
    size = info.size if info.isfile() else 0
    if size < 0 or size > MAX_FILE_BYTES:
        raise SnapshotStorageError(
            "snapshot_file_too_large",
            "Snapshot source contains a file larger than the fixed limit.",
        )
    return SnapshotSourceMember(
        path=path,
        member_type="file" if info.isfile() else "directory",
        size=size,
        mode=stat.S_IMODE(info.mode) & 0o777,
    )


def enumerate_snapshot_source(
    chunks: Iterable[bytes],
) -> tuple[SnapshotSourceMember, ...]:
    """Read one source tar pass, validate it, and return bounded safe inventory."""

    members: list[SnapshotSourceMember] = []
    seen: set[str] = set()
    try:
        with (
            _IteratorReader(chunks) as reader,
            tarfile.open(fileobj=cast(Any, reader), mode="r|*") as archive,
        ):
            for info in archive:
                member = _safe_source_member(info)
                if member is None:
                    continue
                if member.path in seen:
                    raise SnapshotStorageError(
                        "unsafe_snapshot_member",
                        "Snapshot source contains duplicate member paths.",
                    )
                seen.add(member.path)
                members.append(member)
                if len(members) > MAX_MEMBERS:
                    raise SnapshotStorageError(
                        "snapshot_member_limit_exceeded",
                        "Snapshot source exceeds the fixed member limit.",
                    )
    except SnapshotStorageError:
        raise
    except (tarfile.TarError, OSError, ValueError) as error:
        raise SnapshotStorageError(
            "snapshot_source_invalid", "Snapshot source archive is invalid."
        ) from error
    if not members:
        raise SnapshotStorageError(
            "snapshot_source_empty", "Snapshot source contains no approved state."
        )
    return tuple(sorted(members, key=lambda member: member.path))


def _canonical_manifest(
    context: SnapshotCaptureContext,
    *,
    archive_digest: str,
    archive_size: int,
    members: list[SnapshotMemberRecord],
) -> bytes:
    document = {
        "archive": {
            "byte_size": archive_size,
            "member_count": len(members),
            "sha256": archive_digest,
        },
        "captured_at": context.captured_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "captured_fixture_definition_id": (
            None
            if context.captured_fixture_definition_id is None
            else str(context.captured_fixture_definition_id)
        ),
        "kodi_release": context.kodi_release,
        "manifest_version": MANIFEST_VERSION,
        "members": [member.document() for member in members],
        "prior_runtime_state": context.prior_runtime_state,
        "runtime_revision_id": str(context.runtime_revision_id),
        "snapshot_id": str(context.snapshot_id),
        "source_managed_instance_id": str(context.source_managed_instance_id),
    }
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SnapshotStorageError(
            "snapshot_manifest_too_large",
            "Snapshot manifest exceeds the fixed size limit.",
        )
    return payload


def _require_disk_reserve(snapshot_root: Path, estimated_bytes: int) -> None:
    try:
        usage = shutil.disk_usage(snapshot_root)
    except OSError as error:
        raise SnapshotStorageError(
            "snapshot_storage_unavailable",
            "Snapshot storage free space could not be determined.",
        ) from error
    required = max(MIN_FREE_BYTES, estimated_bytes + STORAGE_RESERVE_BYTES)
    if usage.free < required:
        raise SnapshotStorageError(
            "insufficient_snapshot_storage",
            "Snapshot storage does not have the required free-space reserve.",
        )


def _estimated_archive_bytes(members: tuple[SnapshotSourceMember, ...]) -> int:
    tar_blocks = sum((member.size + 511) // 512 for member in members)
    estimate = (len(members) + tar_blocks + 20) * 512
    if estimate > MAX_ARCHIVE_BYTES:
        raise SnapshotStorageError(
            "snapshot_archive_too_large",
            "Estimated snapshot archive exceeds the fixed size limit.",
        )
    return estimate


def _prepare_storage(
    snapshot_root: Path,
    snapshot_id: UUID,
    *,
    owner_uid: int,
    owner_gid: int,
) -> tuple[Path, Path]:
    snapshot_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    if snapshot_root.is_symlink() or not snapshot_root.is_dir():
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot storage root is not safe."
        )
    root_stat = snapshot_root.stat(follow_symlinks=False)
    if root_stat.st_uid != owner_uid:
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot storage root owner is not trusted."
        )
    os.chown(snapshot_root, owner_uid, owner_gid, follow_symlinks=False)
    os.chmod(snapshot_root, 0o750, follow_symlinks=False)
    staging_root = snapshot_root / ".staging"
    staging_root.mkdir(mode=0o700, exist_ok=True)
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot staging root is not safe."
        )
    staging_stat = staging_root.stat(follow_symlinks=False)
    if staging_stat.st_uid != owner_uid:
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot staging root owner is not trusted."
        )
    os.chown(staging_root, owner_uid, owner_gid, follow_symlinks=False)
    os.chmod(staging_root, 0o700, follow_symlinks=False)
    final = snapshot_root / str(snapshot_id)
    stage = staging_root / str(snapshot_id)
    if final.exists() or final.is_symlink():
        raise SnapshotStorageError(
            "snapshot_already_exists", "Snapshot artifact identity already exists."
        )
    try:
        stage.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SnapshotStorageError(
            "snapshot_staging_conflict", "Snapshot staging identity already exists."
        ) from error
    return stage, final


def _cleanup_exact_stage(stage: Path) -> None:
    if stage.is_symlink() or not stage.is_dir():
        return
    try:
        names = {entry.name for entry in os.scandir(stage)}
        if not names.issubset({"state.tar", "manifest.json", ".source"}):
            return
        source = stage / ".source"
        if ".source" in names:
            if source.is_symlink() or not source.is_dir():
                return
            source_names = {entry.name for entry in os.scandir(source)}
            if any(
                len(name) != 8
                or any(character not in "0123456789abcdef" for character in name)
                for name in source_names
            ):
                return
            for name in source_names:
                path = source / name
                if path.is_symlink() or not path.is_file():
                    return
            for name in source_names:
                (source / name).unlink()
            source.rmdir()
            names.remove(".source")
        for name in names:
            path = stage / name
            if path.is_symlink() or not path.is_file():
                return
        for name in names:
            (stage / name).unlink()
        stage.rmdir()
    except OSError:
        return


def publish_snapshot_from_source(
    *,
    data_root: Path,
    context: SnapshotCaptureContext,
    expected_members: tuple[SnapshotSourceMember, ...],
    source_chunks: Iterable[bytes],
    owner_uid: int = 0,
    owner_gid: int,
) -> SnapshotArtifactIdentity:
    """Build and atomically publish one immutable deterministic snapshot."""

    if expected_members != tuple(
        sorted(expected_members, key=lambda member: member.path)
    ) or len({member.path for member in expected_members}) != len(expected_members):
        raise SnapshotStorageError(
            "snapshot_inventory_not_canonical",
            "Snapshot source inventory is not canonical.",
        )

    snapshot_root = data_root / "snapshots"
    snapshot_root.mkdir(mode=0o750, parents=True, exist_ok=True)
    if snapshot_root.is_symlink() or not snapshot_root.is_dir():
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot storage root is not safe."
        )
    if snapshot_root.stat(follow_symlinks=False).st_uid != owner_uid:
        raise SnapshotStorageError(
            "snapshot_storage_unsafe", "Snapshot storage root owner is not trusted."
        )
    estimated_bytes = _estimated_archive_bytes(expected_members)
    _require_disk_reserve(snapshot_root, estimated_bytes * 2)
    stage, final = _prepare_storage(
        snapshot_root,
        context.snapshot_id,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    archive_path = stage / "state.tar"
    manifest_path = stage / "manifest.json"
    source_root = stage / ".source"
    source_root.mkdir(mode=0o700)
    records: list[SnapshotMemberRecord] = []
    expected_by_path = {member.path: member for member in expected_members}
    seen: set[str] = set()
    spooled: dict[str, tuple[Path, str]] = {}
    writer: _BoundedHashingWriter | None = None
    try:
        with (
            _IteratorReader(source_chunks) as reader,
            tarfile.open(fileobj=cast(Any, reader), mode="r|*") as source,
        ):
            for info in source:
                member = _safe_source_member(info)
                if member is None:
                    continue
                expected = expected_by_path.get(member.path)
                if member.path in seen or expected is None or member != expected:
                    raise SnapshotStorageError(
                        "snapshot_source_changed",
                        "Snapshot source changed while it was being captured.",
                    )
                seen.add(member.path)
                if member.member_type == "file":
                    extracted = source.extractfile(info)
                    if extracted is None:
                        raise SnapshotStorageError(
                            "snapshot_source_invalid",
                            "Snapshot source file could not be read.",
                        )
                    source_path = source_root / f"{len(spooled):08x}"
                    hashing_reader = _HashingReader(extracted, member.size)
                    with source_path.open("xb") as source_file:
                        while chunk := hashing_reader.read(1024**2):
                            if source_file.write(chunk) != len(chunk):
                                raise OSError("short snapshot spool write")
                    spooled[member.path] = (source_path, hashing_reader.digest)
        if seen != set(expected_by_path):
            raise SnapshotStorageError(
                "snapshot_source_changed",
                "Snapshot source changed while it was being captured.",
            )
        with archive_path.open("xb") as raw_output:
            writer = _BoundedHashingWriter(raw_output)
            with tarfile.open(
                fileobj=cast(Any, writer),
                mode="w|",
                format=tarfile.GNU_FORMAT,
            ) as output:
                for member in expected_members:
                    destination = tarfile.TarInfo(member.path)
                    destination.type = (
                        tarfile.REGTYPE
                        if member.member_type == "file"
                        else tarfile.DIRTYPE
                    )
                    destination.size = member.size
                    destination.mode = member.mode
                    destination.uid = KODI_ARCHIVE_UID
                    destination.gid = KODI_ARCHIVE_GID
                    destination.uname = "kodi"
                    destination.gname = "kodi"
                    destination.mtime = 0
                    if member.member_type == "directory":
                        output.addfile(destination)
                        digest = None
                    else:
                        source_path, digest = spooled[member.path]
                        with source_path.open("rb") as source_file:
                            output.addfile(destination, source_file)
                        source_path.unlink()
                    records.append(
                        SnapshotMemberRecord(
                            path=member.path,
                            member_type=member.member_type,
                            size=member.size,
                            mode=member.mode,
                            sha256=digest,
                        )
                    )
            raw_output.flush()
            os.fsync(raw_output.fileno())
        source_root.rmdir()
        if writer is None:
            raise SnapshotStorageError(
                "snapshot_source_changed",
                "Snapshot source changed while it was being captured.",
            )
        manifest = _canonical_manifest(
            context,
            archive_digest=writer.digest,
            archive_size=writer.size,
            members=records,
        )
        manifest_digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
        with manifest_path.open("xb") as manifest_file:
            manifest_file.write(manifest)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        for path in (archive_path, manifest_path):
            os.chown(path, owner_uid, owner_gid, follow_symlinks=False)
            os.chmod(path, 0o440, follow_symlinks=False)
        os.chown(stage, owner_uid, owner_gid, follow_symlinks=False)
        os.chmod(stage, 0o750, follow_symlinks=False)
        os.rename(stage, final)
        root_fd = os.open(snapshot_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        return SnapshotArtifactIdentity(
            snapshot_id=context.snapshot_id,
            archive_digest=writer.digest,
            manifest_digest=manifest_digest,
            archive_size=writer.size,
            member_count=len(records),
        )
    except SnapshotStorageError:
        _cleanup_exact_stage(stage)
        raise
    except (OSError, tarfile.TarError, ValueError) as error:
        _cleanup_exact_stage(stage)
        raise SnapshotStorageError(
            "snapshot_capture_failed", "Snapshot artifact could not be captured safely."
        ) from error

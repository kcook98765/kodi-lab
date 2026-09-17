"""Bounded no-follow development-source capture and copy/sync manifests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

MAX_REGULAR_FILES = 25_000
MAX_ENTRIES = 50_000
MAX_INCLUDED_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 512
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_CAPTURED_DESCRIPTOR_BYTES = 1024 * 1024
MANIFEST_VERSION = 1
EXCLUSION_POLICY_VERSION = 1
VCS_ADMIN_DIRECTORIES = frozenset({".git", ".hg", ".svn"})

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class DevelopmentTreeError(RuntimeError):
    """One closed source/destination tree rule failed."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


@dataclass(frozen=True, slots=True)
class DevelopmentTreeEntry:
    path: str
    entry_type: Literal["directory", "file"]
    mode: int
    size: int
    sha256: str | None
    uid: int | None = None
    gid: int | None = None

    def canonical(self, *, include_ownership: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "mode": self.mode,
            "path": self.path,
            "size": self.size,
            "type": self.entry_type,
        }
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if include_ownership:
            result["uid"] = self.uid
            result["gid"] = self.gid
        return result


@dataclass(frozen=True, slots=True)
class DevelopmentTreeManifest:
    entries: tuple[DevelopmentTreeEntry, ...]
    digest: str
    canonical_json: bytes
    included_bytes: int
    captured_files: tuple[tuple[str, bytes], ...] = ()

    def by_path(self) -> dict[str, DevelopmentTreeEntry]:
        return {entry.path: entry for entry in self.entries}

    def captured_file(self, path: str) -> bytes | None:
        return dict(self.captured_files).get(path)


@dataclass(frozen=True, slots=True)
class DevelopmentTreeDelta:
    new_paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    unchanged_count: int
    destination_only_count: int
    transferred_bytes: int

    @property
    def applied_paths(self) -> tuple[str, ...]:
        return self.new_paths + self.changed_paths


def _safe_component(name: str) -> None:
    try:
        encoded = name.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise DevelopmentTreeError(
            "development_source_unsafe_name", "Source tree contains an invalid name."
        ) from error
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or len(encoded) > MAX_COMPONENT_BYTES
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise DevelopmentTreeError(
            "development_source_unsafe_name", "Source tree contains an unsafe name."
        )


def _safe_relative_path(path: str) -> None:
    components = path.split("/")
    if (
        not path
        or len(components) > MAX_DEPTH
        or len(path.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
    ):
        raise DevelopmentTreeError(
            "development_source_tree_bounds", "Source tree path bounds were exceeded."
        )
    for component in components:
        _safe_component(component)


def _normalized_mode(source_mode: int, *, directory: bool) -> int:
    return 0o755 if directory else 0o644 | (source_mode & 0o111)


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_nlink,
        first.st_uid,
        first.st_gid,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_nlink,
        second.st_uid,
        second.st_gid,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


class _Capture:
    def __init__(self, stage_root: Path | None) -> None:
        self.stage_root = stage_root
        self.entries: list[DevelopmentTreeEntry] = []
        self.regular_files = 0
        self.included_bytes = 0

    def add(self, entry: DevelopmentTreeEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > MAX_ENTRIES:
            raise DevelopmentTreeError(
                "development_source_tree_bounds", "Source tree entry limit exceeded."
            )
        if entry.entry_type == "file":
            self.regular_files += 1
            self.included_bytes += entry.size
            if self.regular_files > MAX_REGULAR_FILES:
                raise DevelopmentTreeError(
                    "development_source_tree_bounds", "Source file limit exceeded."
                )
            if self.included_bytes > MAX_INCLUDED_BYTES:
                raise DevelopmentTreeError(
                    "development_source_tree_bounds", "Source byte limit exceeded."
                )


def _open_development_source(development_root: Path, locator: str) -> int:
    if (
        not development_root.is_absolute()
        or development_root == Path("/")
        or development_root != Path(os.path.normpath(development_root))
    ):
        raise DevelopmentTreeError(
            "development_source_root_invalid", "Development root is not safe."
        )
    current_fd = -1
    try:
        current_fd = os.open("/", _DIRECTORY_FLAGS)
        for component in development_root.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        for component in locator.split("/"):
            _safe_component(component)
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except (OSError, ValueError) as error:
        if current_fd >= 0:
            os.close(current_fd)
        raise DevelopmentTreeError(
            "development_source_unavailable",
            "Development source is unavailable or link-unsafe.",
        ) from error


def capture_development_tree(
    development_root: Path,
    locator: str,
    *,
    stage_root: Path | None = None,
) -> DevelopmentTreeManifest:
    """Capture one deterministic tree, optionally materializing normalized bytes."""

    source_fd = _open_development_source(development_root, locator)
    capture = _Capture(stage_root)
    try:
        root_stat = os.fstat(source_fd)
        _walk_directory(source_fd, "", 0, capture)
        if not _same_object(root_stat, os.fstat(source_fd)):
            raise DevelopmentTreeError(
                "development_source_changed_during_capture",
                "Development source changed during capture.",
            )
    finally:
        os.close(source_fd)
    entries = tuple(sorted(capture.entries, key=lambda item: item.path.encode("utf-8")))
    canonical = _canonical_manifest(entries, include_ownership=False)
    return DevelopmentTreeManifest(
        entries=entries,
        digest=f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        canonical_json=canonical,
        included_bytes=capture.included_bytes,
    )


def _walk_directory(
    directory_fd: int, prefix: str, depth: int, capture: _Capture
) -> None:
    if depth >= MAX_DEPTH:
        raise DevelopmentTreeError(
            "development_source_tree_bounds", "Source tree depth limit exceeded."
        )
    try:
        names = os.listdir(directory_fd)
    except OSError as error:
        raise DevelopmentTreeError(
            "development_source_unreadable", "Source tree cannot be read safely."
        ) from error
    for name in sorted(names, key=lambda value: value.encode("utf-8", "surrogatepass")):
        _safe_component(name)
        relative = name if not prefix else f"{prefix}/{name}"
        _safe_relative_path(relative)
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise DevelopmentTreeError(
                "development_source_unreadable", "Source tree cannot be read safely."
            ) from error
        if stat.S_ISDIR(before.st_mode):
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as error:
                raise DevelopmentTreeError(
                    "development_source_unsafe_entry",
                    "Source tree contains an unsafe directory.",
                ) from error
            try:
                opened = os.fstat(child_fd)
                if not _same_object(before, opened):
                    raise DevelopmentTreeError(
                        "development_source_changed_during_capture",
                        "Development source changed during capture.",
                    )
                if name in VCS_ADMIN_DIRECTORIES:
                    continue
                capture.add(
                    DevelopmentTreeEntry(
                        relative,
                        "directory",
                        _normalized_mode(0, directory=True),
                        0,
                        None,
                    )
                )
                if capture.stage_root is not None:
                    target = capture.stage_root.joinpath(*relative.split("/"))
                    target.mkdir(mode=0o700)
                    os.chmod(target, 0o755)
                    os.utime(target, ns=(0, 0), follow_symlinks=False)
                _walk_directory(child_fd, relative, depth + 1, capture)
                after = os.fstat(child_fd)
                linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_object(opened, after) or not _same_object(after, linked):
                    raise DevelopmentTreeError(
                        "development_source_changed_during_capture",
                        "Development source changed during capture.",
                    )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise DevelopmentTreeError(
                "development_source_unsafe_entry",
                "Source tree contains a linked or special entry.",
            )
        if before.st_size > MAX_FILE_BYTES:
            raise DevelopmentTreeError(
                "development_source_tree_bounds", "Source file size limit exceeded."
            )
        _capture_file(directory_fd, name, relative, before, capture)


def _capture_file(
    directory_fd: int,
    name: str,
    relative: str,
    before: os.stat_result,
    capture: _Capture,
) -> None:
    try:
        source_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise DevelopmentTreeError(
            "development_source_unsafe_entry", "Source file cannot be opened safely."
        ) from error
    target_fd: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(source_fd)
        if not _same_object(before, opened) or opened.st_nlink != 1:
            raise DevelopmentTreeError(
                "development_source_changed_during_capture",
                "Development source changed during capture.",
            )
        if capture.stage_root is not None:
            target = capture.stage_root.joinpath(*relative.split("/"))
            target_fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise DevelopmentTreeError(
                    "development_source_tree_bounds", "Source file size limit exceeded."
                )
            digest.update(chunk)
            if target_fd is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
        after = os.fstat(source_fd)
        linked = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            total != before.st_size
            or not _same_object(opened, after)
            or not _same_object(after, linked)
        ):
            raise DevelopmentTreeError(
                "development_source_changed_during_capture",
                "Development source changed during capture.",
            )
        mode = _normalized_mode(before.st_mode, directory=False)
        if target_fd is not None:
            os.fchmod(target_fd, mode)
            os.fsync(target_fd)
            os.utime(target_fd, ns=(0, 0))
        capture.add(
            DevelopmentTreeEntry(relative, "file", mode, total, digest.hexdigest())
        )
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)


def _canonical_manifest(
    entries: tuple[DevelopmentTreeEntry, ...], *, include_ownership: bool
) -> bytes:
    document = {
        "entries": [
            entry.canonical(include_ownership=include_ownership) for entry in entries
        ],
        "exclusion_policy_version": EXCLUSION_POLICY_VERSION,
        "manifest_version": MANIFEST_VERSION,
    }
    encoded = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise DevelopmentTreeError(
            "development_source_tree_bounds", "Canonical manifest limit exceeded."
        )
    return encoded


def compare_captures(
    first: DevelopmentTreeManifest, second: DevelopmentTreeManifest
) -> None:
    if first.canonical_json != second.canonical_json:
        raise DevelopmentTreeError(
            "development_source_changed_during_capture",
            "Development source changed during capture.",
        )


def classify_delta(
    source: DevelopmentTreeManifest, destination: DevelopmentTreeManifest | None
) -> DevelopmentTreeDelta:
    source_by_path = source.by_path()
    destination_by_path = {} if destination is None else destination.by_path()
    new: list[str] = []
    changed: list[str] = []
    unchanged = 0
    for path, entry in source_by_path.items():
        prior = destination_by_path.get(path)
        if prior is None:
            new.append(path)
        elif entry.entry_type != prior.entry_type:
            raise DevelopmentTreeError(
                "development_source_type_conflict",
                "Copy/sync would require a file or directory deletion.",
            )
        elif entry.entry_type == "file" and (
            entry.sha256 != prior.sha256 or entry.mode != prior.mode
        ):
            changed.append(path)
        else:
            unchanged += 1
    destination_only = len(set(destination_by_path) - set(source_by_path))
    transferred = sum(source_by_path[path].size for path in new + changed)
    return DevelopmentTreeDelta(
        tuple(new), tuple(changed), unchanged, destination_only, transferred
    )


def build_delta_archive(
    stage_root: Path,
    archive_path: Path,
    manifest: DevelopmentTreeManifest,
    delta: DevelopmentTreeDelta,
    *,
    runtime_uid: int,
    runtime_gid: int,
    root_name: str | None,
) -> None:
    """Create a deterministic tar containing only the actual applied delta."""

    entries = manifest.by_path()
    applied = set(delta.applied_paths)
    with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
        if root_name is not None:
            info = tarfile.TarInfo(root_name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = runtime_uid
            info.gid = runtime_gid
            info.mtime = 0
            archive.addfile(info)
        for path in sorted(applied, key=lambda value: value.encode("utf-8")):
            entry = entries[path]
            member_name = path if root_name is None else f"{root_name}/{path}"
            info = tarfile.TarInfo(member_name)
            info.mode = entry.mode
            info.uid = runtime_uid
            info.gid = runtime_gid
            info.mtime = 0
            if entry.entry_type == "directory":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = entry.size
                with stage_root.joinpath(*path.split("/")).open("rb") as stream:
                    archive.addfile(info, stream)


def capture_destination_archive(
    chunks: Iterable[bytes],
    *,
    root_name: str,
    capture_paths: frozenset[str] = frozenset(),
) -> DevelopmentTreeManifest:
    """Build a bounded semantic manifest from Docker's fixed-path tar readback."""

    reader = _IterableReader(chunks)
    entries: list[DevelopmentTreeEntry] = []
    total_bytes = 0
    regular_files = 0
    seen: set[str] = set()
    captured: dict[str, bytes] = {}
    root_seen = False
    for requested_path in capture_paths:
        _safe_relative_path(requested_path)
    try:
        with tarfile.open(fileobj=reader, mode="r|*") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                parts = pure.parts
                if not parts or parts[0] != root_name or pure.is_absolute():
                    raise DevelopmentTreeError(
                        "development_destination_unsafe",
                        "Destination readback has an unexpected root.",
                    )
                if len(parts) == 1:
                    if root_seen or not member.isdir():
                        raise DevelopmentTreeError(
                            "development_destination_unsafe",
                            "Destination root is not a directory.",
                        )
                    root_seen = True
                    continue
                if not root_seen:
                    raise DevelopmentTreeError(
                        "development_destination_unsafe",
                        "Destination readback has no canonical root entry.",
                    )
                relative = "/".join(parts[1:])
                _safe_relative_path(relative)
                if relative in seen or member.issym() or member.islnk():
                    raise DevelopmentTreeError(
                        "development_destination_unsafe",
                        "Destination contains an unsafe entry.",
                    )
                seen.add(relative)
                if member.isdir():
                    entry = DevelopmentTreeEntry(
                        relative,
                        "directory",
                        member.mode & 0o7777,
                        0,
                        None,
                        member.uid,
                        member.gid,
                    )
                elif member.isfile():
                    if member.size > MAX_FILE_BYTES:
                        raise DevelopmentTreeError(
                            "development_destination_bounds",
                            "Destination readback bounds were exceeded.",
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DevelopmentTreeError(
                            "development_destination_unsafe",
                            "Destination file could not be read.",
                        )
                    digest = hashlib.sha256()
                    observed = 0
                    retained = bytearray()
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        observed += len(chunk)
                        digest.update(chunk)
                        if relative in capture_paths:
                            retained.extend(chunk)
                            if len(retained) > MAX_CAPTURED_DESCRIPTOR_BYTES:
                                raise DevelopmentTreeError(
                                    "development_destination_bounds",
                                    "Captured destination descriptor is too large.",
                                )
                    if observed != member.size:
                        raise DevelopmentTreeError(
                            "development_destination_unsafe",
                            "Destination file readback was inconsistent.",
                        )
                    regular_files += 1
                    total_bytes += observed
                    if relative in capture_paths:
                        captured[relative] = bytes(retained)
                    entry = DevelopmentTreeEntry(
                        relative,
                        "file",
                        member.mode & 0o7777,
                        observed,
                        digest.hexdigest(),
                        member.uid,
                        member.gid,
                    )
                else:
                    raise DevelopmentTreeError(
                        "development_destination_unsafe",
                        "Destination contains an unsafe entry.",
                    )
                entries.append(entry)
                if (
                    len(entries) > MAX_ENTRIES
                    or regular_files > MAX_REGULAR_FILES
                    or total_bytes > MAX_INCLUDED_BYTES
                ):
                    raise DevelopmentTreeError(
                        "development_destination_bounds",
                        "Destination readback bounds were exceeded.",
                    )
    except (tarfile.TarError, OSError) as error:
        raise DevelopmentTreeError(
            "development_destination_unsafe", "Destination readback is invalid."
        ) from error
    if not root_seen:
        raise DevelopmentTreeError(
            "development_destination_unsafe",
            "Destination readback has no canonical root entry.",
        )
    ordered = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
    canonical = _canonical_manifest(ordered, include_ownership=True)
    return DevelopmentTreeManifest(
        ordered,
        f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        canonical,
        total_bytes,
        tuple(sorted(captured.items())),
    )


def verify_destination(
    source: DevelopmentTreeManifest,
    before: DevelopmentTreeManifest | None,
    after: DevelopmentTreeManifest,
    *,
    runtime_uid: int,
    runtime_gid: int,
) -> None:
    source_entries = source.by_path()
    before_entries = {} if before is None else before.by_path()
    after_entries = after.by_path()
    for path, expected in source_entries.items():
        actual = after_entries.get(path)
        if (
            actual is None
            or actual.entry_type != expected.entry_type
            or actual.mode != expected.mode
            or actual.size != expected.size
            or actual.sha256 != expected.sha256
            or actual.uid != runtime_uid
            or actual.gid != runtime_gid
        ):
            raise DevelopmentTreeError(
                "development_destination_verification_failed",
                "Destination verification did not match staged source.",
            )
    destination_only = set(before_entries) - set(source_entries)
    if any(
        after_entries.get(path) != before_entries[path] for path in destination_only
    ):
        raise DevelopmentTreeError(
            "development_destination_verification_failed",
            "Destination-only state changed during copy/sync.",
        )
    if set(after_entries) != set(source_entries) | destination_only:
        raise DevelopmentTreeError(
            "development_destination_verification_failed",
            "Unexpected destination entries appeared during copy/sync.",
        )


class _IterableReader(io.RawIOBase):
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._iterator = iter(chunks)
        self._buffer = bytearray()
        self._closed = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while size < 0 or len(self._buffer) < size:
            try:
                self._buffer.extend(next(self._iterator))
            except StopIteration:
                break
        if size < 0:
            result = bytes(self._buffer)
            self._buffer.clear()
            return result
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

"""UUID-derived workspace creation and bounded descriptor-relative reads."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import secrets
import shutil
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    DevelopmentTreeManifest,
    capture_development_tree,
    compare_captures,
)
from kodi_lab.development.sources import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidationResult,
    parse_captured_addon_xml,
)

MAX_TEXT_READ_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 2_000
MAX_CHANGE_PREVIEW_BYTES = 16 * 1024

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


class AgentWorkspaceStorageError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(f"{code}: {safe_message}")


@dataclass(frozen=True, slots=True)
class WorkspaceCreationResult:
    manifest_digest: str
    tree_digest: str


@dataclass(frozen=True, slots=True)
class WorkspaceTreeEntry:
    name: str
    entry_type: str
    size: int | None


@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    tree_digest: str
    path: str


@dataclass(frozen=True, slots=True)
class WorkspaceChange:
    path: str
    change_kind: Literal["added", "modified"]
    baseline_size: int | None
    current_size: int
    before_preview: str | None
    after_preview: str
    before_truncated: bool
    after_truncated: bool


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    tree_digest: str
    changes: tuple[WorkspaceChange, ...]
    identity_changed: bool


@dataclass(frozen=True, slots=True)
class WorkspaceStructuralChange:
    path: str
    change_kind: Literal["added", "modified"]


@dataclass(frozen=True, slots=True)
class WorkspaceStructuralValidation:
    tree_digest: str
    changes: tuple[WorkspaceStructuralChange, ...]
    baseline_addon: DevelopmentSourceValidationResult
    current_addon: DevelopmentSourceValidationResult


class AgentWorkspaceReconciliationRequired(AgentWorkspaceStorageError):
    pass


@contextmanager
def workspace_coordination_lock(
    data_root: Path,
    workspace_id: UUID,
    *,
    exclusive: bool,
    blocking: bool = True,
) -> Iterator[None]:
    """Coordinate one workspace without exposing its derived storage path."""

    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    lock_fd = -1
    try:
        lock_fd = os.open(
            ".mutation.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=workspace_fd,
        )
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(lock_fd, operation)
        except BlockingIOError as error:
            raise AgentWorkspaceStorageError(
                "agent_workspace_busy", "Workspace activity is already in progress."
            ) from error
        yield
    finally:
        if lock_fd >= 0:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(workspace_fd)


def _storage_root(data_root: Path) -> Path:
    if not data_root.is_absolute() or data_root == Path("/"):
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_invalid", "Workspace storage is unavailable."
        )
    root = data_root / "agent-workspaces"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_unavailable", "Workspace storage is unavailable."
        ) from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_unavailable", "Workspace storage is unavailable."
        )
    return root


def _workspace_root(data_root: Path, workspace_id: UUID) -> Path:
    return _storage_root(data_root) / str(workspace_id)


def workspace_storage_identity(data_root: Path, workspace_id: UUID) -> tuple[int, int]:
    """Return the no-follow identity of one exact UUID-derived workspace root."""

    storage_fd = os.open(_storage_root(data_root), _DIRECTORY_FLAGS)
    try:
        metadata = os.stat(str(workspace_id), dir_fd=storage_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AgentWorkspaceReconciliationRequired(
                "workspace_cleanup_identity_changed",
                "Workspace cleanup storage identity could not be proven.",
            )
        return metadata.st_dev, metadata.st_ino
    except FileNotFoundError as error:
        raise AgentWorkspaceReconciliationRequired(
            "workspace_cleanup_storage_missing",
            "Workspace cleanup storage identity could not be proven.",
        ) from error
    finally:
        os.close(storage_fd)


def _remove_directory_contents_fd(directory_fd: int, root_device: int) -> None:
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (
                    opened.st_dev != root_device
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                ):
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_cleanup_mount_boundary",
                        "Workspace cleanup encountered an unsafe mount boundary.",
                    )
                _remove_directory_contents_fd(child_fd, root_device)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            # Lab owns the complete UUID subtree. Unlink links, hardlinks, and
            # special objects themselves; never open or follow their targets.
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def cleanup_workspace_storage(
    data_root: Path,
    workspace_id: UUID,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Remove only one proven UUID subtree using descriptor-relative no-follow IO."""

    storage_fd = os.open(_storage_root(data_root), _DIRECTORY_FLAGS)
    name = str(workspace_id)
    try:
        try:
            metadata = os.stat(name, dir_fd=storage_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise AgentWorkspaceReconciliationRequired(
                "workspace_cleanup_identity_changed",
                "Workspace cleanup storage identity changed.",
            )
        root_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=storage_fd)
        try:
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise AgentWorkspaceReconciliationRequired(
                    "workspace_cleanup_identity_changed",
                    "Workspace cleanup storage identity changed.",
                )
            _remove_directory_contents_fd(root_fd, opened.st_dev)
        finally:
            os.close(root_fd)
        final = os.stat(name, dir_fd=storage_fd, follow_symlinks=False)
        if (final.st_dev, final.st_ino) != expected_identity:
            raise AgentWorkspaceReconciliationRequired(
                "workspace_cleanup_identity_changed",
                "Workspace cleanup storage identity changed.",
            )
        os.rmdir(name, dir_fd=storage_fd)
        os.fsync(storage_fd)
    except AgentWorkspaceStorageError:
        raise
    except OSError as error:
        raise AgentWorkspaceReconciliationRequired(
            "workspace_cleanup_interrupted",
            "Workspace cleanup requires reconciliation.",
        ) from error
    finally:
        os.close(storage_fd)


def workspace_storage_absent(data_root: Path, workspace_id: UUID) -> bool:
    storage_fd = os.open(_storage_root(data_root), _DIRECTORY_FLAGS)
    try:
        try:
            os.stat(str(workspace_id), dir_fd=storage_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    finally:
        os.close(storage_fd)


def _remove_exact_private_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_inconsistent", "Workspace storage is inconsistent."
        )
    shutil.rmtree(path)


def remove_unpublished_workspace_storage(data_root: Path, workspace_id: UUID) -> None:
    root = _storage_root(data_root)
    _remove_exact_private_tree(root / f".staging-{workspace_id}")
    _remove_exact_private_tree(root / str(workspace_id))


def workspace_baseline_published(data_root: Path, workspace_id: UUID) -> bool:
    baseline = _workspace_root(data_root, workspace_id) / "baseline" / "tree"
    try:
        metadata = baseline.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _copy_normalized_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    source_fd = os.open(source, _DIRECTORY_FLAGS)
    destination_fd = os.open(destination, _DIRECTORY_FLAGS)
    try:
        _copy_directory_fd(source_fd, destination_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _copy_directory_fd(source_fd: int, destination_fd: int) -> None:
    for name in sorted(os.listdir(source_fd), key=lambda value: value.encode("utf-8")):
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISDIR(before.st_mode):
            child_source = os.open(name, _DIRECTORY_FLAGS, dir_fd=source_fd)
            try:
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                child_destination = os.open(
                    name, _DIRECTORY_FLAGS, dir_fd=destination_fd
                )
                try:
                    _copy_directory_fd(child_source, child_destination)
                    os.fchmod(child_destination, mode)
                    os.fsync(child_destination)
                finally:
                    os.close(child_destination)
            finally:
                os.close(child_source)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            source_file = os.open(name, _FILE_FLAGS, dir_fd=source_fd)
            destination_file = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                opened = os.fstat(source_file)
                if opened.st_ino != before.st_ino or opened.st_dev != before.st_dev:
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_baseline_changed",
                        "Workspace baseline changed during creation.",
                    )
                while chunk := os.read(source_file, 1024 * 1024):
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(destination_file, view) :]
                if os.fstat(source_file).st_size != before.st_size:
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_baseline_changed",
                        "Workspace baseline changed during creation.",
                    )
                os.fchmod(destination_file, mode)
                os.fsync(destination_file)
            finally:
                os.close(destination_file)
                os.close(source_file)
        else:
            raise AgentWorkspaceStorageError(
                "agent_workspace_baseline_unsafe",
                "Workspace baseline contains an unsafe entry.",
            )


def _write_manifest(path: Path, payload: bytes) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def create_workspace_storage(
    data_root: Path,
    workspace_id: UUID,
    development_root: Path,
    source_locator: str,
    expected_addon_id: str,
    *,
    checkpoint: Callable[[str], None] = lambda _phase: None,
) -> WorkspaceCreationResult:
    """Create baseline then generation-0 tree from one verified normalized capture."""

    storage_root = _storage_root(data_root)
    stage = storage_root / f".staging-{workspace_id}"
    final = storage_root / str(workspace_id)
    if stage.exists() or final.exists():
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_conflict", "Workspace storage already exists."
        )
    published = False
    try:
        stage.mkdir(mode=0o700)
        capture = stage / "capture"
        capture.mkdir(mode=0o700)
        checkpoint("capturing")
        first = capture_development_tree(
            development_root, source_locator, stage_root=capture
        )
        try:
            descriptor_fd = os.open(capture / "addon.xml", _FILE_FLAGS)
            try:
                descriptor = os.read(descriptor_fd, MAX_ADDON_XML_BYTES + 1)
                if len(descriptor) > MAX_ADDON_XML_BYTES or os.read(descriptor_fd, 1):
                    raise DevelopmentTreeError(
                        "agent_workspace_addon_xml_invalid",
                        "Captured workspace add-on descriptor is invalid.",
                    )
            finally:
                os.close(descriptor_fd)
            observation = parse_captured_addon_xml(source_locator, descriptor)
        except (OSError, DevelopmentSourceValidationError) as error:
            raise DevelopmentTreeError(
                "agent_workspace_addon_xml_invalid",
                "Captured workspace add-on descriptor is invalid.",
            ) from error
        if observation.addon_id != expected_addon_id:
            raise DevelopmentTreeError(
                "agent_workspace_source_identity_changed",
                "Development source add-on identity changed after registration.",
            )
        checkpoint("verifying_source")
        second = capture_development_tree(development_root, source_locator)
        compare_captures(first, second)

        final.mkdir(mode=0o700)
        baseline = final / "baseline"
        baseline.mkdir(mode=0o700)
        checkpoint("publishing_baseline")
        os.rename(capture, baseline / "tree")
        _write_manifest(baseline / "manifest.json", first.canonical_json)
        published = True

        checkpoint("creating_tree")
        _copy_normalized_tree(baseline / "tree", final / "tree")
        checkpoint("verifying_tree")
        baseline_manifest = capture_development_tree(baseline, "tree")
        tree_manifest = capture_development_tree(final, "tree")
        compare_captures(first, baseline_manifest)
        compare_captures(first, tree_manifest)
        _remove_exact_private_tree(stage)
        return WorkspaceCreationResult(first.digest, tree_manifest.digest)
    except DevelopmentTreeError:
        if not published:
            _remove_exact_private_tree(stage)
            _remove_exact_private_tree(final)
        raise
    except Exception:
        if not published:
            _remove_exact_private_tree(stage)
            _remove_exact_private_tree(final)
        raise


def normalize_workspace_relative_path(
    value: str, *, allow_root: bool
) -> tuple[str, ...]:
    try:
        encoded_value = value.encode("utf-8", "strict")
    except (AttributeError, UnicodeEncodeError) as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_path_invalid", "Workspace path is invalid."
        ) from error
    if value == "" and allow_root:
        return ()
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or len(encoded_value) > 512
    ):
        raise AgentWorkspaceStorageError(
            "agent_workspace_path_invalid", "Workspace path is invalid."
        )
    parts = value.split("/")
    if len(parts) > 32:
        raise AgentWorkspaceStorageError(
            "agent_workspace_path_invalid", "Workspace path is invalid."
        )
    for part in parts:
        if (
            part in {"", ".", ".."}
            or len(part.encode("utf-8", "strict")) > 255
            or any(unicodedata.category(character) == "Cc" for character in part)
        ):
            raise AgentWorkspaceStorageError(
                "agent_workspace_path_invalid", "Workspace path is invalid."
            )
    return tuple(parts)


def _open_workspace_path(
    data_root: Path, workspace_id: UUID, parts: tuple[str, ...], *, directory: bool
) -> int:
    root = _workspace_root(data_root, workspace_id) / "tree"
    try:
        fd = os.open(root, _DIRECTORY_FLAGS)
        for index, part in enumerate(parts):
            opens_directory = index < len(parts) - 1 or directory
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if opens_directory:
                if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_path_unavailable",
                        "Workspace path is unavailable or unsafe.",
                    )
                flags = _DIRECTORY_FLAGS
            else:
                if stat.S_ISLNK(before.st_mode):
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_path_unavailable",
                        "Workspace path is unavailable or unsafe.",
                    )
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_file_unsafe", "Workspace file is unsafe."
                    )
                flags = _FILE_FLAGS
            next_fd = os.open(part, flags, dir_fd=fd)
            opened = os.fstat(next_fd)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                os.close(next_fd)
                raise AgentWorkspaceStorageError(
                    "agent_workspace_path_unavailable",
                    "Workspace path is unavailable or unsafe.",
                )
            os.close(fd)
            fd = next_fd
        return fd
    except AgentWorkspaceStorageError:
        with suppress(OSError, UnboundLocalError):
            os.close(fd)
        raise
    except OSError as error:
        with suppress(OSError, UnboundLocalError):
            os.close(fd)
        raise AgentWorkspaceStorageError(
            "agent_workspace_path_unavailable",
            "Workspace path is unavailable or unsafe.",
        ) from error


def inspect_workspace_directory(
    data_root: Path, workspace_id: UUID, relative_path: str
) -> tuple[WorkspaceTreeEntry, ...]:
    parts = normalize_workspace_relative_path(relative_path, allow_root=True)
    fd = _open_workspace_path(data_root, workspace_id, parts, directory=True)
    try:
        names = sorted(os.listdir(fd), key=lambda value: value.encode("utf-8"))
        if len(names) > MAX_DIRECTORY_ENTRIES:
            raise AgentWorkspaceStorageError(
                "agent_workspace_directory_too_large",
                "Workspace directory is too large.",
            )
        result: list[WorkspaceTreeEntry] = []
        for name in names:
            normalize_workspace_relative_path(name, allow_root=False)
            metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                result.append(WorkspaceTreeEntry(name, "directory", None))
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                result.append(WorkspaceTreeEntry(name, "file", metadata.st_size))
            else:
                raise AgentWorkspaceStorageError(
                    "agent_workspace_entry_unsafe",
                    "Workspace contains an unsafe entry.",
                )
        return tuple(result)
    finally:
        os.close(fd)


def read_workspace_text(data_root: Path, workspace_id: UUID, relative_path: str) -> str:
    parts = normalize_workspace_relative_path(relative_path, allow_root=False)
    fd = _open_workspace_path(data_root, workspace_id, parts, directory=False)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AgentWorkspaceStorageError(
                "agent_workspace_file_unsafe", "Workspace file is unsafe."
            )
        if metadata.st_size > MAX_TEXT_READ_BYTES:
            raise AgentWorkspaceStorageError(
                "agent_workspace_file_too_large",
                "Workspace file exceeds the read limit.",
            )
        payload = bytearray()
        while chunk := os.read(
            fd, min(1024 * 1024, MAX_TEXT_READ_BYTES + 1 - len(payload))
        ):
            payload.extend(chunk)
            if len(payload) > MAX_TEXT_READ_BYTES:
                raise AgentWorkspaceStorageError(
                    "agent_workspace_file_too_large",
                    "Workspace file exceeds the read limit.",
                )
        after = os.fstat(fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ):
            raise AgentWorkspaceStorageError(
                "agent_workspace_file_changed", "Workspace file changed during reading."
            )
        try:
            return bytes(payload).decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise AgentWorkspaceStorageError(
                "agent_workspace_file_not_text",
                "Workspace file is not valid UTF-8 text.",
            ) from error
    finally:
        os.close(fd)


def _capture_workspace_tree(
    data_root: Path, workspace_id: UUID, *, baseline: bool = False
) -> DevelopmentTreeManifest:
    locator = f"{workspace_id}/baseline/tree" if baseline else f"{workspace_id}/tree"
    try:
        return capture_development_tree(
            _storage_root(data_root), locator, reject_vcs=True
        )
    except DevelopmentTreeError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_tree_unsafe", "Workspace tree is inconsistent or unsafe."
        ) from error


def capture_workspace_deployment_tree(
    data_root: Path, workspace_id: UUID, *, stage_root: Path
) -> DevelopmentTreeManifest:
    """Recapture and materialize one UUID-derived current tree for transfer."""

    locator = f"{workspace_id}/tree"
    try:
        return capture_development_tree(
            _storage_root(data_root),
            locator,
            stage_root=stage_root,
            reject_vcs=True,
        )
    except DevelopmentTreeError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_tree_unsafe", "Workspace tree is inconsistent or unsafe."
        ) from error


def _open_workspace_root_fd(data_root: Path, workspace_id: UUID) -> int:
    storage_fd = os.open(_storage_root(data_root), _DIRECTORY_FLAGS)
    try:
        return os.open(str(workspace_id), _DIRECTORY_FLAGS, dir_fd=storage_fd)
    except OSError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_storage_unavailable", "Workspace storage is unavailable."
        ) from error
    finally:
        os.close(storage_fd)


def _open_mutation_parent(
    workspace_root_fd: int, parts: tuple[str, ...]
) -> tuple[int, str]:
    try:
        current_fd = os.open("tree", _DIRECTORY_FLAGS, dir_fd=workspace_root_fd)
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except OSError as error:
        with suppress(OSError, UnboundLocalError):
            os.close(current_fd)
        raise AgentWorkspaceStorageError(
            "agent_workspace_parent_unsafe",
            "Workspace target parent is unavailable or unsafe.",
        ) from error


def _read_regular_fd(fd: int, *, maximum: int = MAX_TEXT_READ_BYTES) -> bytes:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AgentWorkspaceStorageError(
            "agent_workspace_file_unsafe", "Workspace file is unsafe."
        )
    if metadata.st_size > maximum:
        raise AgentWorkspaceStorageError(
            "agent_workspace_file_too_large", "Workspace file exceeds the text limit."
        )
    payload = bytearray()
    while chunk := os.read(fd, min(1024 * 1024, maximum + 1 - len(payload))):
        payload.extend(chunk)
        if len(payload) > maximum:
            raise AgentWorkspaceStorageError(
                "agent_workspace_file_too_large",
                "Workspace file exceeds the text limit.",
            )
    after = os.fstat(fd)
    if (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise AgentWorkspaceStorageError(
            "agent_workspace_file_changed", "Workspace file changed during reading."
        )
    return bytes(payload)


def _encode_text(value: str) -> bytes:
    try:
        payload = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_content_invalid", "Workspace content must be UTF-8 text."
        ) from error
    if len(payload) > MAX_TEXT_READ_BYTES:
        raise AgentWorkspaceStorageError(
            "agent_workspace_content_too_large",
            "Workspace content exceeds the 2 MiB limit.",
        )
    return payload


def _write_private_file(
    directory_fd: int, name: str, payload: bytes, mode: int
) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_create(directory_fd: int, temporary: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentWorkspaceStorageError(
            "agent_workspace_atomic_create_unavailable",
            "Atomic workspace creation is unavailable.",
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(temporary),
        directory_fd,
        os.fsencode(target),
        1,  # Linux RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise AgentWorkspaceStorageError(
            "agent_workspace_create_conflict", "Workspace file already exists."
        )
    raise AgentWorkspaceStorageError(
        "agent_workspace_atomic_create_failed", "Workspace file creation failed."
    ) from OSError(error_number, os.strerror(error_number))


def _open_target(directory_fd: int, target: str) -> int:
    try:
        return os.open(target, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_replace_missing",
            "Workspace file does not exist or is unsafe.",
        ) from error


def _atomic_replace(directory_fd: int, temporary: str, target: str) -> None:
    os.replace(
        temporary,
        target,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _unlink_if_exact(
    directory_fd: int,
    target: str,
    expected: bytes,
    expected_identity: tuple[int, int] | None,
) -> None:
    fd = _open_target(directory_fd, target)
    try:
        metadata = os.fstat(fd)
        if (
            _read_regular_fd(fd) != expected
            or expected_identity is None
            or (metadata.st_dev, metadata.st_ino) != expected_identity
        ):
            raise AgentWorkspaceStorageError(
                "agent_workspace_rollback_unproven",
                "Workspace rollback could not be proven.",
            )
    finally:
        os.close(fd)
    os.unlink(target, dir_fd=directory_fd)
    os.fsync(directory_fd)


@contextmanager
def mutate_workspace_text(
    data_root: Path,
    workspace_id: UUID,
    relative_path: str,
    *,
    operation: Literal["create", "replace", "patch"],
    content: str,
    expected_tree_digest: str,
    expected_text: str | None = None,
) -> Iterator[WorkspaceMutationResult]:
    """Atomically mutate one text file and roll it back unless the caller commits."""

    parts = normalize_workspace_relative_path(relative_path, allow_root=False)
    payload = _encode_text(content)
    expected_payload = None if expected_text is None else _encode_text(expected_text)
    if operation == "patch" and expected_payload is None:
        raise AgentWorkspaceStorageError(
            "agent_workspace_patch_invalid", "An exact patch preimage is required."
        )
    if relative_path == "addon.xml":
        try:
            parse_captured_addon_xml("workspace", payload)
        except DevelopmentSourceValidationError as error:
            raise AgentWorkspaceStorageError(
                "agent_workspace_addon_xml_invalid",
                "Workspace add-on descriptor is invalid.",
            ) from error

    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    parent_fd = -1
    temporary: str | None = None
    backup: str | None = None
    applied = False
    committed = False
    preflight_complete = False
    post_identity: tuple[int, int] | None = None
    try:
        lock_fd = os.open(
            ".mutation.lock",
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=workspace_fd,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            before = _capture_workspace_tree(data_root, workspace_id)
            if before.digest != expected_tree_digest:
                raise AgentWorkspaceStorageError(
                    "agent_workspace_integrity_changed",
                    "Workspace tree no longer matches its durable generation.",
                )
            preflight_complete = True
            parent_fd, target = _open_mutation_parent(workspace_fd, parts)
            mode = 0o644
            prior_payload: bytes | None = None
            if operation == "create":
                try:
                    existing = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    del existing
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_create_conflict",
                        "Workspace file already exists.",
                    )
            else:
                target_fd = _open_target(parent_fd, target)
                try:
                    target_metadata = os.fstat(target_fd)
                    prior_payload = _read_regular_fd(target_fd)
                    if (
                        os.fstat(target_fd).st_dev,
                        os.fstat(target_fd).st_ino,
                    ) != (target_metadata.st_dev, target_metadata.st_ino):
                        raise AgentWorkspaceStorageError(
                            "agent_workspace_integrity_changed",
                            "Workspace target changed during validation.",
                        )
                    try:
                        prior_payload.decode("utf-8", "strict")
                    except UnicodeDecodeError as error:
                        raise AgentWorkspaceStorageError(
                            "agent_workspace_file_not_text",
                            "Workspace file is not valid UTF-8 text.",
                        ) from error
                    mode = stat.S_IMODE(target_metadata.st_mode)
                finally:
                    os.close(target_fd)
                if operation == "patch" and prior_payload != expected_payload:
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_patch_preimage_mismatch",
                        "Workspace patch preimage does not match exactly.",
                    )
                backup = f".rollback-{secrets.token_hex(16)}"
                os.link(
                    target,
                    backup,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=workspace_fd,
                    follow_symlinks=False,
                )
                backup_fd = os.open(backup, _FILE_FLAGS, dir_fd=workspace_fd)
                try:
                    backup_metadata = os.fstat(backup_fd)
                finally:
                    os.close(backup_fd)
                linked_metadata = os.stat(
                    target, dir_fd=parent_fd, follow_symlinks=False
                )
                if (
                    backup_metadata.st_dev,
                    backup_metadata.st_ino,
                    linked_metadata.st_dev,
                    linked_metadata.st_ino,
                    linked_metadata.st_nlink,
                ) != (
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                    target_metadata.st_dev,
                    target_metadata.st_ino,
                    2,
                ):
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_integrity_changed",
                        "Workspace target changed during validation.",
                    )
            temporary = f".workspace-write-{secrets.token_hex(16)}"
            _write_private_file(parent_fd, temporary, payload, mode)
            if operation == "create":
                _atomic_create(parent_fd, temporary, target)
            else:
                _atomic_replace(parent_fd, temporary, target)
            temporary = None
            applied = True
            os.fsync(parent_fd)
            post_fd = _open_target(parent_fd, target)
            try:
                post_metadata = os.fstat(post_fd)
                if _read_regular_fd(post_fd) != payload:
                    raise AgentWorkspaceStorageError(
                        "agent_workspace_integrity_changed",
                        "Workspace postimage could not be proven.",
                    )
                post_identity = (post_metadata.st_dev, post_metadata.st_ino)
            finally:
                os.close(post_fd)
            after = _capture_workspace_tree(data_root, workspace_id)
            post_name = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if (post_name.st_dev, post_name.st_ino) != post_identity:
                raise AgentWorkspaceStorageError(
                    "agent_workspace_integrity_changed",
                    "Workspace target changed during recapture.",
                )
            yield WorkspaceMutationResult(after.digest, relative_path)
            committed = True
        except BaseException as original:
            if applied:
                try:
                    if operation == "create":
                        _unlink_if_exact(parent_fd, target, payload, post_identity)
                    else:
                        current_fd = _open_target(parent_fd, target)
                        try:
                            current_metadata = os.fstat(current_fd)
                            if (
                                _read_regular_fd(current_fd) != payload
                                or post_identity is None
                                or (
                                    current_metadata.st_dev,
                                    current_metadata.st_ino,
                                )
                                != post_identity
                            ):
                                raise AgentWorkspaceStorageError(
                                    "agent_workspace_rollback_unproven",
                                    "Workspace rollback could not be proven.",
                                )
                        finally:
                            os.close(current_fd)
                        if backup is None:
                            raise AgentWorkspaceStorageError(
                                "agent_workspace_rollback_unproven",
                                "Workspace rollback could not be proven.",
                            )
                        os.replace(
                            backup,
                            target,
                            src_dir_fd=workspace_fd,
                            dst_dir_fd=parent_fd,
                        )
                        backup = None
                        os.fsync(parent_fd)
                    restored = _capture_workspace_tree(data_root, workspace_id)
                    if restored.digest != expected_tree_digest:
                        raise AgentWorkspaceStorageError(
                            "agent_workspace_rollback_unproven",
                            "Workspace rollback could not be proven.",
                        )
                except BaseException as rollback_error:
                    raise AgentWorkspaceReconciliationRequired(
                        "agent_workspace_reconciliation_required",
                        "Workspace mutation requires reconciliation.",
                    ) from rollback_error
            elif preflight_complete:
                try:
                    if temporary is not None and parent_fd >= 0:
                        with suppress(FileNotFoundError):
                            os.unlink(temporary, dir_fd=parent_fd)
                        temporary = None
                    if backup is not None:
                        os.unlink(backup, dir_fd=workspace_fd)
                        backup = None
                    unchanged = _capture_workspace_tree(data_root, workspace_id)
                    if unchanged.digest != expected_tree_digest:
                        raise AgentWorkspaceStorageError(
                            "agent_workspace_integrity_changed",
                            "Workspace changed before mutation could be applied.",
                        )
                except BaseException as integrity_error:
                    raise AgentWorkspaceReconciliationRequired(
                        "agent_workspace_reconciliation_required",
                        "Workspace mutation requires reconciliation.",
                    ) from integrity_error
            raise original
        finally:
            if temporary is not None and parent_fd >= 0:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=parent_fd)
            if backup is not None and (committed or not applied):
                with suppress(OSError):
                    os.unlink(backup, dir_fd=workspace_fd)
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(workspace_fd)


def _read_named_tree_text(
    data_root: Path,
    workspace_id: UUID,
    relative_path: str,
    *,
    baseline: bool,
) -> str:
    parts = normalize_workspace_relative_path(relative_path, allow_root=False)
    prefix = ("baseline", "tree") if baseline else ("tree",)
    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    fd = workspace_fd
    try:
        sequence = (*prefix, *parts)
        for index, part in enumerate(sequence):
            next_fd = os.open(
                part,
                _DIRECTORY_FLAGS if index < len(sequence) - 1 else _FILE_FLAGS,
                dir_fd=fd,
            )
            if fd != workspace_fd:
                os.close(fd)
            fd = next_fd
        payload = _read_regular_fd(fd)
        try:
            return payload.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise AgentWorkspaceStorageError(
                "agent_workspace_changed_binary",
                "Changed workspace files must be UTF-8 text.",
            ) from error
    except OSError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_tree_unsafe", "Workspace tree is inconsistent or unsafe."
        ) from error
    finally:
        if fd != workspace_fd:
            os.close(fd)
        os.close(workspace_fd)


def _preview(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_CHANGE_PREVIEW_BYTES:
        return value, False
    return encoded[:MAX_CHANGE_PREVIEW_BYTES].decode("utf-8", "ignore"), True


def _derive_workspace_diff_locked(
    data_root: Path,
    workspace_id: UUID,
    *,
    expected_baseline_digest: str,
    expected_tree_digest: str,
    expected_addon_id: str,
) -> WorkspaceDiff:
    baseline = _capture_workspace_tree(data_root, workspace_id, baseline=True)
    current = _capture_workspace_tree(data_root, workspace_id)
    if (
        baseline.digest != expected_baseline_digest
        or current.digest != expected_tree_digest
    ):
        raise AgentWorkspaceStorageError(
            "agent_workspace_integrity_changed",
            "Workspace tree no longer matches durable metadata.",
        )
    baseline_by_path = baseline.by_path()
    current_by_path = current.by_path()
    if set(baseline_by_path) - set(current_by_path):
        raise AgentWorkspaceStorageError(
            "agent_workspace_baseline_entry_missing",
            "Workspace is missing a baseline entry.",
        )
    changes: list[WorkspaceChange] = []
    for path in sorted(current_by_path, key=lambda value: value.encode("utf-8")):
        present = current_by_path[path]
        prior = baseline_by_path.get(path)
        if prior is None:
            if present.entry_type != "file":
                raise AgentWorkspaceStorageError(
                    "agent_workspace_type_transition",
                    "Workspace contains an unsupported added directory.",
                )
            after = _read_named_tree_text(data_root, workspace_id, path, baseline=False)
            after_preview, after_truncated = _preview(after)
            changes.append(
                WorkspaceChange(
                    path,
                    "added",
                    None,
                    present.size,
                    None,
                    after_preview,
                    False,
                    after_truncated,
                )
            )
            continue
        if present.entry_type != prior.entry_type:
            raise AgentWorkspaceStorageError(
                "agent_workspace_type_transition",
                "Workspace contains an unsupported type transition.",
            )
        if present.entry_type == "directory" or (
            present.sha256 == prior.sha256 and present.mode == prior.mode
        ):
            continue
        before = _read_named_tree_text(data_root, workspace_id, path, baseline=True)
        after = _read_named_tree_text(data_root, workspace_id, path, baseline=False)
        before_preview, before_truncated = _preview(before)
        after_preview, after_truncated = _preview(after)
        changes.append(
            WorkspaceChange(
                path,
                "modified",
                prior.size,
                present.size,
                before_preview,
                after_preview,
                before_truncated,
                after_truncated,
            )
        )
    descriptor = _read_named_tree_text(
        data_root, workspace_id, "addon.xml", baseline=False
    ).encode("utf-8")
    try:
        identity = parse_captured_addon_xml("workspace", descriptor).addon_id
    except DevelopmentSourceValidationError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_addon_xml_invalid",
            "Workspace add-on descriptor is invalid.",
        ) from error
    return WorkspaceDiff(
        tree_digest=current.digest,
        changes=tuple(changes),
        identity_changed=identity != expected_addon_id,
    )


def derive_workspace_diff(
    data_root: Path,
    workspace_id: UUID,
    *,
    expected_baseline_digest: str,
    expected_tree_digest: str,
    expected_addon_id: str,
) -> WorkspaceDiff:
    """Derive one coherent change view while excluding concurrent mutations."""

    with workspace_coordination_lock(
        data_root, workspace_id, exclusive=False, blocking=True
    ):
        return _derive_workspace_diff_locked(
            data_root,
            workspace_id,
            expected_baseline_digest=expected_baseline_digest,
            expected_tree_digest=expected_tree_digest,
            expected_addon_id=expected_addon_id,
        )


def _parse_workspace_descriptor(
    data_root: Path, workspace_id: UUID, *, baseline: bool
) -> DevelopmentSourceValidationResult:
    try:
        document = _read_named_tree_text(
            data_root, workspace_id, "addon.xml", baseline=baseline
        ).encode("utf-8")
        return parse_captured_addon_xml("workspace", document)
    except (AgentWorkspaceStorageError, DevelopmentSourceValidationError) as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_addon_xml_invalid",
            "Workspace add-on descriptor is invalid.",
        ) from error


def _validate_workspace_structure_locked(
    data_root: Path,
    workspace_id: UUID,
    *,
    expected_baseline_digest: str,
    expected_tree_digest: str,
) -> WorkspaceStructuralValidation:
    baseline = _capture_workspace_tree(data_root, workspace_id, baseline=True)
    current = _capture_workspace_tree(data_root, workspace_id)
    if baseline.digest != expected_baseline_digest:
        raise AgentWorkspaceStorageError(
            "agent_workspace_integrity_changed",
            "Workspace tree no longer matches durable metadata.",
        )
    baseline_by_path = baseline.by_path()
    current_by_path = current.by_path()
    if set(baseline_by_path) - set(current_by_path):
        raise AgentWorkspaceStorageError(
            "agent_workspace_baseline_entry_missing",
            "Workspace is missing a baseline entry.",
        )
    changes: list[WorkspaceStructuralChange] = []
    for path in sorted(current_by_path, key=lambda value: value.encode("utf-8")):
        present = current_by_path[path]
        prior = baseline_by_path.get(path)
        if prior is None:
            if present.entry_type != "file":
                raise AgentWorkspaceStorageError(
                    "agent_workspace_type_transition",
                    "Workspace contains an unsupported added directory.",
                )
            _read_named_tree_text(data_root, workspace_id, path, baseline=False)
            changes.append(WorkspaceStructuralChange(path, "added"))
            continue
        if present.entry_type != prior.entry_type:
            raise AgentWorkspaceStorageError(
                "agent_workspace_type_transition",
                "Workspace contains an unsupported type transition.",
            )
        if present.entry_type == "directory":
            continue
        if present.sha256 == prior.sha256 and present.mode == prior.mode:
            continue
        if present.sha256 == prior.sha256:
            raise AgentWorkspaceStorageError(
                "agent_workspace_mode_transition",
                "Workspace contains an unsupported mode-only transition.",
            )
        _read_named_tree_text(data_root, workspace_id, path, baseline=True)
        _read_named_tree_text(data_root, workspace_id, path, baseline=False)
        changes.append(WorkspaceStructuralChange(path, "modified"))
    baseline_addon = _parse_workspace_descriptor(data_root, workspace_id, baseline=True)
    current_addon = _parse_workspace_descriptor(data_root, workspace_id, baseline=False)
    if current.digest != expected_tree_digest:
        raise AgentWorkspaceStorageError(
            "agent_workspace_integrity_changed",
            "Workspace tree no longer matches durable metadata.",
        )
    return WorkspaceStructuralValidation(
        tree_digest=current.digest,
        changes=tuple(changes),
        baseline_addon=baseline_addon,
        current_addon=current_addon,
    )


def validate_workspace_structure(
    data_root: Path,
    workspace_id: UUID,
    *,
    expected_baseline_digest: str,
    expected_tree_digest: str,
) -> WorkspaceStructuralValidation:
    """Validate one exact bounded structural view without returning content."""

    with workspace_coordination_lock(
        data_root, workspace_id, exclusive=False, blocking=False
    ):
        return _validate_workspace_structure_locked(
            data_root,
            workspace_id,
            expected_baseline_digest=expected_baseline_digest,
            expected_tree_digest=expected_tree_digest,
        )

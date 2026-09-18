"""UUID-derived workspace creation and bounded descriptor-relative reads."""

from __future__ import annotations

import os
import shutil
import stat
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kodi_lab.development.deployment import (
    DevelopmentTreeError,
    capture_development_tree,
    compare_captures,
)
from kodi_lab.development.sources import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationError,
    parse_captured_addon_xml,
)

MAX_TEXT_READ_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 2_000

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


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
    if value == "" and allow_root:
        return ()
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > 512
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
            or len(part.encode("utf-8")) > 255
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
            flags = (
                _DIRECTORY_FLAGS if index < len(parts) - 1 or directory else _FILE_FLAGS
            )
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
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

"""Three-way workspace review, exact source apply, backup, and rollback."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from kodi_lab.development.deployment import (
    DevelopmentTreeEntry,
    DevelopmentTreeError,
    _open_development_source,
    capture_development_tree,
)
from kodi_lab.development.sources import (
    DevelopmentSourceValidationError,
    parse_captured_addon_xml,
)
from kodi_lab.workspace.storage import (
    MAX_TEXT_READ_BYTES,
    AgentWorkspaceStorageError,
    _capture_workspace_tree,
    _open_workspace_root_fd,
    _read_named_tree_text,
)

MAX_PROMOTION_FILES = 100
MAX_PROMOTION_REVIEW_BYTES = 4 * 1024 * 1024
MAX_PROMOTION_RESPONSE_BYTES = 8 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


class AgentWorkspacePromotionStorageError(RuntimeError):
    def __init__(
        self, code: str, safe_message: str, *, reconciliation_required: bool = False
    ) -> None:
        self.code = code
        self.safe_message = safe_message
        self.reconciliation_required = reconciliation_required
        super().__init__(f"{code}: {safe_message}")


@dataclass(frozen=True, slots=True)
class PromotionPathMaterial:
    ordinal: int
    relative_path: str
    change_kind: Literal["add", "modify"]
    pre_sha256: str | None
    pre_size: int | None
    pre_mode: int | None
    post_sha256: str
    post_size: int
    post_mode: int
    before_text: str | None
    after_text: str
    conflict_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionReviewMaterial:
    source_capture_digest: str
    current_name: str
    current_version: str
    promoted_name: str
    promoted_version: str
    paths: tuple[PromotionPathMaterial, ...]
    review_text_bytes: int
    permission_classification: Literal[
        "promotion_enabled", "readable_not_promotion_writable"
    ]

    @property
    def conflicted(self) -> bool:
        return any(item.conflict_reason is not None for item in self.paths)


def _entry_identity(entry: DevelopmentTreeEntry) -> tuple[str, int, int]:
    if entry.entry_type != "file" or entry.sha256 is None:
        raise AgentWorkspacePromotionStorageError(
            "promotion_type_transition", "Promotion supports regular files only."
        )
    return entry.sha256, entry.size, entry.mode


def _read_source_file(
    source_fd: int, relative_path: str, entry: DevelopmentTreeEntry
) -> bytes:
    current = os.dup(source_fd)
    try:
        parts = relative_path.split("/")
        for index, part in enumerate(parts):
            next_fd = os.open(
                part,
                _DIRECTORY_FLAGS if index < len(parts) - 1 else _FILE_FLAGS,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        metadata = os.fstat(current)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_unsafe", "Development source is unsafe."
            )
        payload = bytearray()
        while chunk := os.read(current, 1024 * 1024):
            payload.extend(chunk)
            if len(payload) > max(MAX_TEXT_READ_BYTES, entry.size):
                raise AgentWorkspacePromotionStorageError(
                    "promotion_source_changed", "Development source changed."
                )
        digest = hashlib.sha256(payload).hexdigest()
        mode = 0o644 | (metadata.st_mode & 0o111)
        if (digest, len(payload), mode) != _entry_identity(entry):
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_changed", "Development source changed."
            )
        return bytes(payload)
    except OSError as error:
        raise AgentWorkspacePromotionStorageError(
            "promotion_source_unsafe", "Development source is unavailable or unsafe."
        ) from error
    finally:
        os.close(current)


def _parents(relative_path: str) -> tuple[str, ...]:
    parts = relative_path.split("/")[:-1]
    return tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _read_review_text(
    data_root: Path, workspace_id: UUID, relative_path: str, *, baseline: bool
) -> str:
    try:
        return _read_named_tree_text(
            data_root, workspace_id, relative_path, baseline=baseline
        )
    except AgentWorkspaceStorageError as error:
        if error.code == "agent_workspace_file_too_large":
            raise AgentWorkspacePromotionStorageError(
                "promotion_review_too_large",
                "Promotion review exceeds its complete-text limit.",
            ) from error
        if error.code == "agent_workspace_changed_binary":
            raise AgentWorkspacePromotionStorageError(
                "promotion_binary_change", "Promotion supports UTF-8 text files only."
            ) from error
        raise AgentWorkspacePromotionStorageError(
            "promotion_workspace_unsafe", "Workspace review material is unsafe."
        ) from error


def _writable(source_fd: int, paths: tuple[PromotionPathMaterial, ...]) -> bool:
    try:
        if not os.access(".", os.W_OK | os.X_OK, dir_fd=source_fd, effective_ids=True):
            return False
        for parent in sorted(
            {item for path in paths for item in _parents(path.relative_path)}
        ):
            if not os.access(
                parent,
                os.W_OK | os.X_OK,
                dir_fd=source_fd,
                effective_ids=True,
                follow_symlinks=False,
            ):
                return False
        return True
    except (NotImplementedError, OSError):
        return False


def prepare_promotion_review(
    data_root: Path,
    workspace_id: UUID,
    development_root: Path,
    source_locator: str,
    *,
    expected_baseline_digest: str,
    expected_workspace_digest: str,
    expected_addon_id: str,
) -> PromotionReviewMaterial:
    """Build a complete review under the workspace mutation exclusion lock."""

    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    lock_fd = os.open(
        ".mutation.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=workspace_fd,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        baseline = _capture_workspace_tree(data_root, workspace_id, baseline=True)
        workspace = _capture_workspace_tree(data_root, workspace_id)
        if (
            baseline.digest != expected_baseline_digest
            or workspace.digest != expected_workspace_digest
        ):
            raise AgentWorkspacePromotionStorageError(
                "promotion_workspace_drift",
                "Workspace no longer matches its generation.",
            )
        baseline_entries = baseline.by_path()
        workspace_entries = workspace.by_path()
        if set(baseline_entries) - set(workspace_entries):
            raise AgentWorkspacePromotionStorageError(
                "promotion_delete_unsupported", "Promotion cannot delete source paths."
            )
        try:
            source = capture_development_tree(development_root, source_locator)
        except DevelopmentTreeError as error:
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_unsafe",
                "Development source is inconsistent or unsafe.",
            ) from error
        source_entries = source.by_path()
        raw: list[
            tuple[
                str,
                Literal["add", "modify"],
                DevelopmentTreeEntry | None,
                DevelopmentTreeEntry,
            ]
        ] = []
        for relative_path in sorted(
            workspace_entries, key=lambda value: value.encode("utf-8")
        ):
            post = workspace_entries[relative_path]
            pre = baseline_entries.get(relative_path)
            if pre is None:
                if post.entry_type != "file":
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_directory_creation_unsupported",
                        "Promotion cannot create directories.",
                    )
                raw.append((relative_path, "add", None, post))
            elif post.entry_type != pre.entry_type:
                raise AgentWorkspacePromotionStorageError(
                    "promotion_type_transition", "Promotion cannot change object types."
                )
            elif post.entry_type == "file" and (post.sha256, post.mode) != (
                pre.sha256,
                pre.mode,
            ):
                if post.sha256 == pre.sha256:
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_mode_only_transition",
                        "Promotion cannot apply a mode-only change.",
                    )
                raw.append((relative_path, "modify", pre, post))
        if not raw:
            raise AgentWorkspacePromotionStorageError(
                "promotion_no_changes", "Workspace has no promotable changes."
            )
        if len(raw) > MAX_PROMOTION_FILES:
            raise AgentWorkspacePromotionStorageError(
                "promotion_review_too_large", "Promotion review exceeds its file limit."
            )
        source_fd = _open_development_source(development_root, source_locator)
        try:
            materials: list[PromotionPathMaterial] = []
            review_bytes = 0
            for ordinal, (relative_path, kind, pre, post) in enumerate(raw):
                after = _read_review_text(
                    data_root, workspace_id, relative_path, baseline=False
                )
                before: str | None = None
                conflict: str | None = None
                current_source = source_entries.get(relative_path)
                if kind == "modify":
                    assert pre is not None
                    before = _read_review_text(
                        data_root, workspace_id, relative_path, baseline=True
                    )
                    if current_source is None or current_source.entry_type != "file":
                        conflict = "overlapping_source_path"
                    elif _entry_identity(current_source) != _entry_identity(
                        pre
                    ) or _read_source_file(
                        source_fd, relative_path, current_source
                    ) != before.encode("utf-8"):
                        conflict = "overlapping_source_modification"
                else:
                    if current_source is not None:
                        conflict = "source_addition_conflict"
                    elif any(
                        source_entries.get(parent) is None
                        or source_entries[parent].entry_type != "directory"
                        for parent in _parents(relative_path)
                    ):
                        conflict = "source_parent_conflict"
                before_bytes = b"" if before is None else before.encode("utf-8")
                after_bytes = after.encode("utf-8")
                review_bytes += len(before_bytes) + len(after_bytes)
                if review_bytes > MAX_PROMOTION_REVIEW_BYTES:
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_review_too_large",
                        "Promotion review exceeds its complete-text limit.",
                    )
                post_hash, post_size, post_mode = _entry_identity(post)
                materials.append(
                    PromotionPathMaterial(
                        ordinal=ordinal,
                        relative_path=relative_path,
                        change_kind=kind,
                        pre_sha256=None if pre is None else _entry_identity(pre)[0],
                        pre_size=None if pre is None else pre.size,
                        pre_mode=None if pre is None else pre.mode,
                        post_sha256=post_hash,
                        post_size=post_size,
                        post_mode=post_mode,
                        before_text=before,
                        after_text=after,
                        conflict_reason=conflict,
                    )
                )
            descriptor_path = next(
                (item for item in materials if item.relative_path == "addon.xml"), None
            )
            if descriptor_path is None:
                descriptor_entry = source_entries.get("addon.xml")
                if descriptor_entry is None:
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_source_identity_drift",
                        "Development source add-on identity is unavailable.",
                    )
                descriptor = _read_source_file(source_fd, "addon.xml", descriptor_entry)
            else:
                descriptor = descriptor_path.after_text.encode("utf-8")
            current_descriptor_entry = source_entries.get("addon.xml")
            if current_descriptor_entry is None:
                raise AgentWorkspacePromotionStorageError(
                    "promotion_source_identity_drift",
                    "Development source add-on identity is unavailable.",
                )
            try:
                current_observation = parse_captured_addon_xml(
                    source_locator,
                    _read_source_file(source_fd, "addon.xml", current_descriptor_entry),
                )
                observation = parse_captured_addon_xml(source_locator, descriptor)
            except DevelopmentSourceValidationError as error:
                raise AgentWorkspacePromotionStorageError(
                    "promotion_addon_xml_invalid",
                    "Effective promoted add-on descriptor is invalid.",
                ) from error
            if observation.addon_id != expected_addon_id:
                raise AgentWorkspacePromotionStorageError(
                    "promotion_source_identity_drift",
                    "Promotion cannot change the registered add-on ID.",
                )
            if current_observation.addon_id != expected_addon_id:
                raise AgentWorkspacePromotionStorageError(
                    "promotion_source_identity_drift",
                    "Development source add-on identity changed.",
                )
            paths = tuple(materials)
            return PromotionReviewMaterial(
                source_capture_digest=source.digest,
                current_name=current_observation.observed_name,
                current_version=current_observation.observed_version,
                promoted_name=observation.observed_name,
                promoted_version=observation.observed_version,
                paths=paths,
                review_text_bytes=review_bytes,
                permission_classification=(
                    "promotion_enabled"
                    if _writable(source_fd, paths)
                    else "readable_not_promotion_writable"
                ),
            )
        finally:
            os.close(source_fd)
    finally:
        with suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(workspace_fd)


def _renameat2(directory_fd: int, source: str, target: str, flag: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentWorkspacePromotionStorageError(
            "promotion_atomic_primitive_unavailable",
            "Required atomic source-apply support is unavailable.",
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
        os.fsencode(source),
        directory_fd,
        os.fsencode(target),
        flag,
    )
    if result == 0:
        return
    number = ctypes.get_errno()
    code = (
        "promotion_source_overlap"
        if number in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOENT}
        else "promotion_atomic_apply_failed"
    )
    raise AgentWorkspacePromotionStorageError(
        code, "Development source changed before exact application."
    ) from OSError(number, os.strerror(number))


def _open_parent(source_fd: int, relative_path: str) -> tuple[int, str]:
    current = os.dup(source_fd)
    parts = relative_path.split("/")
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except OSError as error:
        os.close(current)
        raise AgentWorkspacePromotionStorageError(
            "promotion_source_parent_changed",
            "Development source parent is unavailable or unsafe.",
        ) from error


def _write_file(directory_fd: int, name: str, payload: bytes, mode: int) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short source write")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_exact_fd(fd: int) -> tuple[bytes, int, tuple[int, int]]:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AgentWorkspacePromotionStorageError(
            "promotion_source_unsafe", "Development source contains an unsafe object."
        )
    payload = bytearray()
    while chunk := os.read(fd, 1024 * 1024):
        payload.extend(chunk)
    return (
        bytes(payload),
        0o644 | (metadata.st_mode & 0o111),
        (metadata.st_dev, metadata.st_ino),
    )


def _matches(
    payload: bytes, mode: int, sha256: str, size: int, expected_mode: int
) -> bool:
    return (
        len(payload) == size
        and hashlib.sha256(payload).hexdigest() == sha256
        and mode == expected_mode
    )


def _backup_root(data_root: Path, operation_id: UUID, backup_id: UUID) -> Path:
    return data_root / "source-promotions" / str(operation_id) / f"backup-{backup_id}"


def _publish_backup(
    data_root: Path,
    operation_id: UUID,
    backup_id: UUID,
    source_fd: int,
    paths: tuple[PromotionPathMaterial, ...],
) -> tuple[Path, dict[int, int]]:
    def write_immutable(path: Path, payload: bytes) -> None:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short backup write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    root = data_root / "source-promotions"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    operation_root = root / str(operation_id)
    operation_root.mkdir(mode=0o700)
    stage = operation_root / f".staging-{backup_id}"
    final = _backup_root(data_root, operation_id, backup_id)
    stage.mkdir(mode=0o700)
    manifest: list[dict[str, object]] = []
    raw_modes: dict[int, int] = {}
    try:
        for item in paths:
            record: dict[str, object] = {
                "kind": item.change_kind,
                "ordinal": item.ordinal,
                "path": item.relative_path,
            }
            if item.change_kind == "modify":
                parent_fd, target = _open_parent(source_fd, item.relative_path)
                try:
                    fd = os.open(target, _FILE_FLAGS, dir_fd=parent_fd)
                    try:
                        raw_mode = stat.S_IMODE(os.fstat(fd).st_mode) & 0o777
                        payload, mode, _identity = _read_exact_fd(fd)
                    finally:
                        os.close(fd)
                finally:
                    os.close(parent_fd)
                assert (
                    item.pre_sha256 is not None
                    and item.pre_size is not None
                    and item.pre_mode is not None
                )
                if not _matches(
                    payload, mode, item.pre_sha256, item.pre_size, item.pre_mode
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_source_overlap", "Development source path changed."
                    )
                backup_file = stage / f"{item.ordinal:03d}.preimage"
                write_immutable(backup_file, payload)
                raw_modes[item.ordinal] = raw_mode
                record.update(
                    sha256=item.pre_sha256,
                    size=item.pre_size,
                    normalized_mode=item.pre_mode,
                    raw_mode=raw_mode,
                )
            else:
                record["absent"] = True
            manifest.append(record)
        encoded = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest_path = stage / "manifest.json"
        write_immutable(manifest_path, encoded)
        stage_fd = os.open(stage, _DIRECTORY_FLAGS)
        try:
            os.fsync(stage_fd)
        finally:
            os.close(stage_fd)
        os.rename(stage, final)
        operation_fd = os.open(operation_root, _DIRECTORY_FLAGS)
        try:
            os.fsync(operation_fd)
        finally:
            os.close(operation_fd)
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        for item in paths:
            if item.change_kind == "modify":
                payload = (final / f"{item.ordinal:03d}.preimage").read_bytes()
                assert item.pre_sha256 is not None and item.pre_size is not None
                if (
                    len(payload) != item.pre_size
                    or hashlib.sha256(payload).hexdigest() != item.pre_sha256
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_backup_verification_failed",
                        "Promotion backup verification failed.",
                    )
        if (final / "manifest.json").read_bytes() != encoded:
            raise AgentWorkspacePromotionStorageError(
                "promotion_backup_verification_failed",
                "Promotion backup verification failed.",
            )
        os.chmod(final, 0o500)
        os.chmod(operation_root, 0o500)
        return final, raw_modes
    except Exception:
        with suppress(OSError):
            if stage.exists():
                for child in stage.iterdir():
                    child.unlink()
                stage.rmdir()
            operation_root.rmdir()
        raise


def prove_promotion_preimages(
    development_root: Path,
    source_locator: str,
    *,
    expected_addon_id: str,
    paths: tuple[PromotionPathMaterial, ...],
) -> None:
    """Prove a post-marker/no-journal source still has every planned preimage."""

    try:
        source = capture_development_tree(development_root, source_locator)
    except DevelopmentTreeError as error:
        raise AgentWorkspacePromotionStorageError(
            "promotion_source_unsafe", "Development source is inconsistent or unsafe."
        ) from error
    entries = source.by_path()
    source_fd = _open_development_source(development_root, source_locator)
    try:
        descriptor_entry = entries.get("addon.xml")
        if descriptor_entry is None or descriptor_entry.entry_type != "file":
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_identity_drift",
                "Development source add-on identity is unavailable.",
            )
        try:
            observation = parse_captured_addon_xml(
                source_locator,
                _read_source_file(source_fd, "addon.xml", descriptor_entry),
            )
        except DevelopmentSourceValidationError as error:
            raise AgentWorkspacePromotionStorageError(
                "promotion_addon_xml_invalid",
                "Development source add-on descriptor is invalid.",
            ) from error
        if observation.addon_id != expected_addon_id:
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_identity_drift",
                "Development source add-on identity changed.",
            )
        for item in paths:
            current = entries.get(item.relative_path)
            if item.change_kind == "add":
                if current is not None or any(
                    entries.get(parent) is None
                    or entries[parent].entry_type != "directory"
                    for parent in _parents(item.relative_path)
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_source_overlap",
                        "Development source no longer has the planned preimages.",
                    )
            elif (
                current is None
                or current.entry_type != "file"
                or item.pre_sha256 is None
                or item.pre_size is None
                or item.pre_mode is None
                or _entry_identity(current)
                != (item.pre_sha256, item.pre_size, item.pre_mode)
            ):
                raise AgentWorkspacePromotionStorageError(
                    "promotion_source_overlap",
                    "Development source no longer has the planned preimages.",
                )
            else:
                _read_source_file(source_fd, item.relative_path, current)
    finally:
        os.close(source_fd)


def execute_promotion(
    data_root: Path,
    workspace_id: UUID,
    development_root: Path,
    source_locator: str,
    *,
    operation_id: UUID,
    backup_id: UUID,
    expected_baseline_digest: str,
    expected_workspace_digest: str,
    expected_addon_id: str,
    paths: tuple[PromotionPathMaterial, ...],
    backup_published: Callable[[UUID], None],
    application_marker: Callable[[], None],
    journal_update: Callable[[int, str, int | None, int | None], None],
    final_capture_started: Callable[[], None],
    publish_success: Callable[[], None],
    failure_injector: Callable[[str], None] = lambda _phase: None,
) -> None:
    """Apply postimages and publish success while source/workspace are locked."""

    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    lock_fd = os.open(
        ".mutation.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=workspace_fd,
    )
    source_fd = -1
    marker = False
    applied: list[tuple[PromotionPathMaterial, tuple[int, int]]] = []
    backup_path: Path | None = None
    backup_raw_modes: dict[int, int] = {}
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        baseline = _capture_workspace_tree(data_root, workspace_id, baseline=True)
        workspace = _capture_workspace_tree(data_root, workspace_id)
        if (
            baseline.digest != expected_baseline_digest
            or workspace.digest != expected_workspace_digest
        ):
            raise AgentWorkspacePromotionStorageError(
                "promotion_workspace_drift", "Workspace changed after review."
            )
        source = capture_development_tree(development_root, source_locator)
        source_fd = _open_development_source(development_root, source_locator)
        source_entries = source.by_path()
        descriptor_entry = source_entries.get("addon.xml")
        if descriptor_entry is None or descriptor_entry.entry_type != "file":
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_identity_drift",
                "Development source add-on identity is unavailable.",
            )
        try:
            current_observation = parse_captured_addon_xml(
                source_locator,
                _read_source_file(source_fd, "addon.xml", descriptor_entry),
            )
        except DevelopmentSourceValidationError as error:
            raise AgentWorkspacePromotionStorageError(
                "promotion_addon_xml_invalid",
                "Development source add-on descriptor is invalid.",
            ) from error
        if current_observation.addon_id != expected_addon_id:
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_identity_drift",
                "Development source add-on identity changed.",
            )
        for item in paths:
            current = source_entries.get(item.relative_path)
            if item.change_kind == "add":
                if current is not None or any(
                    source_entries.get(parent) is None
                    or source_entries[parent].entry_type != "directory"
                    for parent in _parents(item.relative_path)
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_source_overlap",
                        "Development source overlaps the reviewed addition.",
                    )
            else:
                if (
                    current is None
                    or current.entry_type != "file"
                    or item.pre_sha256 is None
                    or item.pre_size is None
                    or item.pre_mode is None
                    or _entry_identity(current)
                    != (item.pre_sha256, item.pre_size, item.pre_mode)
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_source_overlap",
                        "Development source overlaps a reviewed modification.",
                    )
                _read_source_file(source_fd, item.relative_path, current)
            workspace_entry = workspace.by_path().get(item.relative_path)
            if workspace_entry is None or _entry_identity(workspace_entry) != (
                item.post_sha256,
                item.post_size,
                item.post_mode,
            ):
                raise AgentWorkspacePromotionStorageError(
                    "promotion_workspace_drift", "Workspace changed after review."
                )
        if not _writable(source_fd, paths):
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_not_writable",
                "Development source is not promotion-writable.",
            )
        failure_injector("before_backup_publication")
        backup_path, backup_raw_modes = _publish_backup(
            data_root, operation_id, backup_id, source_fd, paths
        )
        backup_published(backup_id)
        failure_injector("after_backup_before_marker")
        application_marker()
        marker = True
        failure_injector("after_marker_before_first_write")
        for index, item in enumerate(paths):
            parent_fd, target = _open_parent(source_fd, item.relative_path)
            temporary = (
                f".promotion-{operation_id.hex}-{item.ordinal}-{secrets.token_hex(4)}"
            )
            try:
                payload = item.after_text.encode("utf-8")
                _write_file(parent_fd, temporary, payload, item.post_mode)
                if item.change_kind == "add":
                    _renameat2(parent_fd, temporary, target, 1)
                else:
                    _renameat2(parent_fd, temporary, target, 2)
                    displaced_fd = os.open(temporary, _FILE_FLAGS, dir_fd=parent_fd)
                    try:
                        displaced, displaced_mode, _ = _read_exact_fd(displaced_fd)
                    finally:
                        os.close(displaced_fd)
                    assert (
                        item.pre_sha256 is not None
                        and item.pre_size is not None
                        and item.pre_mode is not None
                    )
                    if not _matches(
                        displaced,
                        displaced_mode,
                        item.pre_sha256,
                        item.pre_size,
                        item.pre_mode,
                    ):
                        _renameat2(parent_fd, temporary, target, 2)
                        raise AgentWorkspacePromotionStorageError(
                            "promotion_source_overlap",
                            "Development source changed during exact exchange.",
                        )
                    os.unlink(temporary, dir_fd=parent_fd)
                target_fd = os.open(target, _FILE_FLAGS, dir_fd=parent_fd)
                try:
                    current_payload, current_mode, identity = _read_exact_fd(target_fd)
                finally:
                    os.close(target_fd)
                if not _matches(
                    current_payload,
                    current_mode,
                    item.post_sha256,
                    item.post_size,
                    item.post_mode,
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_postimage_unproven",
                        "Applied source postimage could not be proven.",
                    )
                os.fsync(parent_fd)
                applied.append((item, identity))
                journal_update(item.ordinal, "applied", identity[0], identity[1])
                failure_injector(
                    "after_first_addition"
                    if item.change_kind == "add"
                    else "after_first_modification"
                )
                if index == 0 and len(paths) > 1:
                    failure_injector("mid_multi_file_apply")
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent_fd)
                os.close(parent_fd)
        failure_injector("after_all_writes_before_final_recapture")
        final_capture_started()
        final = capture_development_tree(development_root, source_locator)
        final_entries = final.by_path()
        for item in paths:
            current = final_entries.get(item.relative_path)
            if current is None or _entry_identity(current) != (
                item.post_sha256,
                item.post_size,
                item.post_mode,
            ):
                raise AgentWorkspacePromotionStorageError(
                    "promotion_final_capture_failed",
                    "Final source capture did not prove the promotion.",
                )
        descriptor = _read_source_file(
            source_fd, "addon.xml", final_entries["addon.xml"]
        )
        try:
            observation = parse_captured_addon_xml(source_locator, descriptor)
        except DevelopmentSourceValidationError as error:
            raise AgentWorkspacePromotionStorageError(
                "promotion_addon_xml_invalid", "Promoted add-on descriptor is invalid."
            ) from error
        if observation.addon_id != expected_addon_id:
            raise AgentWorkspacePromotionStorageError(
                "promotion_source_identity_drift", "Promoted add-on identity changed."
            )
        failure_injector("after_final_recapture_before_db_success")
        publish_success()
        return
    except Exception as original:
        if not marker:
            raise
        rollback_failed = False
        for item, applied_identity in reversed(applied):
            parent_fd = -1
            temporary = f".promotion-rollback-{operation_id.hex}-{item.ordinal}"
            try:
                parent_fd, target = _open_parent(source_fd, item.relative_path)
                current_fd = os.open(target, _FILE_FLAGS, dir_fd=parent_fd)
                try:
                    current_payload, current_mode, current_identity = _read_exact_fd(
                        current_fd
                    )
                finally:
                    os.close(current_fd)
                if current_identity != applied_identity or not _matches(
                    current_payload,
                    current_mode,
                    item.post_sha256,
                    item.post_size,
                    item.post_mode,
                ):
                    raise AgentWorkspacePromotionStorageError(
                        "promotion_rollback_unproven",
                        "Source rollback could not prove the operation postimage.",
                    )
                if item.change_kind == "add":
                    os.unlink(target, dir_fd=parent_fd)
                else:
                    assert backup_path is not None and item.pre_mode is not None
                    preimage = (
                        backup_path / f"{item.ordinal:03d}.preimage"
                    ).read_bytes()
                    _write_file(
                        parent_fd,
                        temporary,
                        preimage,
                        backup_raw_modes[item.ordinal],
                    )
                    _renameat2(parent_fd, temporary, target, 2)
                    displaced_fd = os.open(temporary, _FILE_FLAGS, dir_fd=parent_fd)
                    try:
                        displaced, displaced_mode, _ = _read_exact_fd(displaced_fd)
                    finally:
                        os.close(displaced_fd)
                    if not _matches(
                        displaced,
                        displaced_mode,
                        item.post_sha256,
                        item.post_size,
                        item.post_mode,
                    ):
                        _renameat2(parent_fd, temporary, target, 2)
                        raise AgentWorkspacePromotionStorageError(
                            "promotion_rollback_unproven",
                            "Source rollback exchange could not be proven.",
                        )
                    os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
                journal_update(item.ordinal, "rolled_back", None, None)
            except Exception:
                rollback_failed = True
                with suppress(Exception):
                    journal_update(item.ordinal, "uncertain", None, None)
            finally:
                if parent_fd >= 0:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=parent_fd)
                    os.close(parent_fd)
        try:
            restored = capture_development_tree(development_root, source_locator)
            restored_entries = restored.by_path()
            for item in paths:
                current = restored_entries.get(item.relative_path)
                if item.change_kind == "add":
                    rollback_failed = rollback_failed or current is not None
                else:
                    assert item.pre_sha256 is not None
                    assert item.pre_size is not None
                    assert item.pre_mode is not None
                    rollback_failed = (
                        rollback_failed
                        or current is None
                        or _entry_identity(current)
                        != (item.pre_sha256, item.pre_size, item.pre_mode)
                    )
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise AgentWorkspacePromotionStorageError(
                "promotion_reconciliation_required",
                "Source promotion requires reconciliation.",
                reconciliation_required=True,
            ) from original
        raise AgentWorkspacePromotionStorageError(
            getattr(original, "code", "promotion_application_failed"),
            "Source promotion failed and was rolled back exactly.",
        ) from original
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        with suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        os.close(workspace_fd)

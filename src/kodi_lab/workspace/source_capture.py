"""Private, whole-tree source capture and recoverable workspace switch primitives.

These functions are deliberately not an authority surface. Their callers must hold
the workspace coordination lock and publish a durable capturing operation first.
"""

from __future__ import annotations

import ctypes
import errno
import os
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
    DevelopmentSourceValidationError,
    parse_captured_addon_xml,
)
from kodi_lab.workspace.storage import (
    AgentWorkspaceStorageError,
    _copy_normalized_tree,
    _open_workspace_root_fd,
    _storage_root,
    _write_manifest,
)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


@dataclass(frozen=True, slots=True)
class StagedSourceCapture:
    digest: str
    addon_version: str


def _workspace_path(data_root: Path, workspace_id: UUID) -> Path:
    return _storage_root(data_root) / str(workspace_id)


def _operation_path(data_root: Path, workspace_id: UUID, operation_id: UUID) -> Path:
    return (
        _workspace_path(data_root, workspace_id)
        / "capture-operations"
        / str(operation_id)
    )


def _open_operation_fd(workspace_fd: int, operation_id: UUID) -> int:
    operations_fd = os.open("capture-operations", _DIR, dir_fd=workspace_fd)
    try:
        return os.open(str(operation_id), _DIR, dir_fd=operations_fd)
    finally:
        os.close(operations_fd)


def _capture_private(data_root: Path, workspace_id: UUID, suffix: str) -> str:
    try:
        return capture_development_tree(
            _storage_root(data_root), f"{workspace_id}/{suffix}", reject_vcs=True
        ).digest
    except DevelopmentTreeError as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_unsafe", "Workspace capture storage is unsafe."
        ) from error


def _optional_digest(data_root: Path, workspace_id: UUID, suffix: str) -> str | None:
    path = _workspace_path(data_root, workspace_id) / suffix
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return _capture_private(data_root, workspace_id, suffix)


def _exchange(first_fd: int, first: str, second_fd: int, second: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_atomic_unavailable",
            "Atomic capture is unavailable.",
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(first_fd, os.fsencode(first), second_fd, os.fsencode(second), 2) != 0:
        number = ctypes.get_errno()
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_switch_failed", "Workspace capture switch failed."
        ) from OSError(number, os.strerror(number))
    os.fsync(first_fd)
    os.fsync(second_fd)


def _rename_no_replace(first_fd: int, first: str, second_fd: int, second: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_atomic_unavailable",
            "Atomic capture is unavailable.",
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(first_fd, os.fsencode(first), second_fd, os.fsencode(second), 1) != 0:
        number = ctypes.get_errno()
        code = (
            "agent_workspace_capture_archive_exists"
            if number in {errno.EEXIST, errno.ENOTEMPTY}
            else "agent_workspace_capture_archive_failed"
        )
        raise AgentWorkspaceStorageError(
            code, "Workspace capture archive failed."
        ) from OSError(number, os.strerror(number))
    os.fsync(first_fd)
    os.fsync(second_fd)


def stage_source_capture(
    data_root: Path,
    workspace_id: UUID,
    operation_id: UUID,
    development_root: Path,
    source_locator: str,
    expected_addon_id: str,
    *,
    prior_digest: str,
    checkpoint: Callable[[str], None] = lambda _phase: None,
) -> StagedSourceCapture:
    """Materialize one two-pass-verified source tree in UUID-derived private state."""

    if _capture_private(data_root, workspace_id, "tree") != prior_digest:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_private_edits",
            "Workspace tree does not match its captured baseline.",
        )
    workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
    try:
        with suppress(FileExistsError):
            os.mkdir("capture-operations", 0o700, dir_fd=workspace_fd)
        operations_fd = os.open("capture-operations", _DIR, dir_fd=workspace_fd)
        try:
            os.mkdir(str(operation_id), 0o700, dir_fd=operations_fd)
            operation_fd = os.open(str(operation_id), _DIR, dir_fd=operations_fd)
            try:
                os.mkdir("new-baseline", 0o700, dir_fd=operation_fd)
                baseline_fd = os.open("new-baseline", _DIR, dir_fd=operation_fd)
                try:
                    os.mkdir("tree", 0o700, dir_fd=baseline_fd)
                    os.fsync(baseline_fd)
                finally:
                    os.close(baseline_fd)
                os.fsync(operation_fd)
            finally:
                os.close(operation_fd)
        finally:
            os.close(operations_fd)
        os.fsync(workspace_fd)
    finally:
        os.close(workspace_fd)
    op_path = _operation_path(data_root, workspace_id, operation_id)
    first = capture_development_tree(
        development_root,
        source_locator,
        stage_root=op_path / "new-baseline" / "tree",
    )
    checkpoint("staged")
    try:
        descriptor = (op_path / "new-baseline" / "tree" / "addon.xml").read_bytes()
        observed = parse_captured_addon_xml(source_locator, descriptor)
    except (OSError, DevelopmentSourceValidationError) as error:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_addon_invalid", "Captured add-on is invalid."
        ) from error
    if observed.addon_id != expected_addon_id:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_identity_changed",
            "Source add-on identity changed.",
        )
    second = capture_development_tree(development_root, source_locator)
    compare_captures(first, second)
    _write_manifest(op_path / "new-baseline" / "manifest.json", first.canonical_json)
    _copy_normalized_tree(op_path / "new-baseline" / "tree", op_path / "new-tree")
    if (
        _capture_private(
            data_root,
            workspace_id,
            f"capture-operations/{operation_id}/new-baseline/tree",
        )
        != first.digest
        or _capture_private(
            data_root, workspace_id, f"capture-operations/{operation_id}/new-tree"
        )
        != first.digest
    ):
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_stage_changed", "Staged capture changed."
        )
    checkpoint("verified")
    return StagedSourceCapture(first.digest, observed.observed_version)


def complete_source_capture_switch(
    data_root: Path,
    workspace_id: UUID,
    operation_id: UUID,
    prior_baseline_id: UUID,
    *,
    prior_digest: str,
    captured_digest: str,
    checkpoint: Callable[[str], None] = lambda _phase: None,
) -> None:
    """Resume the two guarded exchanges and retain the previous baseline."""

    op_prefix = f"capture-operations/{operation_id}"
    baseline = _capture_private(data_root, workspace_id, "baseline/tree")
    tree = _capture_private(data_root, workspace_id, "tree")
    staged_baseline = _optional_digest(
        data_root, workspace_id, f"{op_prefix}/new-baseline/tree"
    )
    staged_tree = _capture_private(data_root, workspace_id, f"{op_prefix}/new-tree")
    history = _optional_digest(
        data_root, workspace_id, f"history/{prior_baseline_id}/tree"
    )
    if baseline not in {prior_digest, captured_digest} or tree not in {
        prior_digest,
        captured_digest,
    }:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_reconciliation_required",
            "Capture state is inconsistent.",
        )
    if baseline == prior_digest:
        if tree != prior_digest or staged_baseline != captured_digest:
            raise AgentWorkspaceStorageError(
                "agent_workspace_capture_reconciliation_required",
                "Capture state is inconsistent.",
            )
        workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
        try:
            operation_fd = _open_operation_fd(workspace_fd, operation_id)
            try:
                _exchange(workspace_fd, "baseline", operation_fd, "new-baseline")
            finally:
                os.close(operation_fd)
        finally:
            os.close(workspace_fd)
        checkpoint("baseline_switched")
    elif staged_baseline != prior_digest and history != prior_digest:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_reconciliation_required",
            "Prior baseline is missing.",
        )
    if _capture_private(data_root, workspace_id, "tree") == prior_digest:
        if staged_tree != captured_digest:
            raise AgentWorkspaceStorageError(
                "agent_workspace_capture_reconciliation_required",
                "Capture state is inconsistent.",
            )
        workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
        try:
            operation_fd = _open_operation_fd(workspace_fd, operation_id)
            try:
                _exchange(workspace_fd, "tree", operation_fd, "new-tree")
            finally:
                os.close(operation_fd)
        finally:
            os.close(workspace_fd)
        checkpoint("tree_switched")
    elif staged_tree != prior_digest:
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_reconciliation_required", "Prior tree is missing."
        )
    if (
        _optional_digest(data_root, workspace_id, f"history/{prior_baseline_id}/tree")
        is None
    ):
        if (
            _capture_private(data_root, workspace_id, f"{op_prefix}/new-baseline/tree")
            != prior_digest
        ):
            raise AgentWorkspaceStorageError(
                "agent_workspace_capture_reconciliation_required",
                "Prior baseline is missing.",
            )
        workspace_fd = _open_workspace_root_fd(data_root, workspace_id)
        try:
            with suppress(FileExistsError):
                os.mkdir("history", 0o700, dir_fd=workspace_fd)
            history_fd = os.open("history", _DIR, dir_fd=workspace_fd)
            operation_fd = _open_operation_fd(workspace_fd, operation_id)
            try:
                _rename_no_replace(
                    operation_fd, "new-baseline", history_fd, str(prior_baseline_id)
                )
            finally:
                os.close(operation_fd)
                os.close(history_fd)
        finally:
            os.close(workspace_fd)
        checkpoint("baseline_archived")
    if (
        _capture_private(data_root, workspace_id, "baseline/tree") != captured_digest
        or _capture_private(data_root, workspace_id, "tree") != captured_digest
        or _capture_private(
            data_root, workspace_id, f"history/{prior_baseline_id}/tree"
        )
        != prior_digest
        or _capture_private(data_root, workspace_id, f"{op_prefix}/new-tree")
        != prior_digest
    ):
        raise AgentWorkspaceStorageError(
            "agent_workspace_capture_reconciliation_required",
            "Capture switch is inconsistent.",
        )

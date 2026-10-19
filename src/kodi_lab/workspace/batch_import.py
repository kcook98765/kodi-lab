"""Atomic private-tree switch for a reviewed, registered-source text batch.

The application holds the workspace lock and persists its operation journal
before calling this primitive. No caller-provided host paths are accepted.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from kodi_lab.development.deployment import capture_development_tree
from kodi_lab.workspace.source_capture import _exchange
from kodi_lab.workspace.storage import (
    AgentWorkspaceReconciliationRequired,
    AgentWorkspaceStorageError,
    _copy_normalized_tree,
    _open_workspace_root_fd,
    _storage_root,
    _write_private_file,
    normalize_workspace_relative_path,
)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


@contextmanager
def switch_import_tree(
    data_root: Path,
    workspace_id: UUID,
    operation_id: UUID,
    files: Mapping[str, bytes],
    *,
    expected_digest: str,
    prepared: Callable[[str], None] = lambda _digest: None,
) -> Iterator[str]:
    """Publish all accepted files together; prove rollback on ordinary failure."""

    root = _storage_root(data_root)
    workspace_path = root / str(workspace_id)
    stage_name = f".import-{operation_id}"
    stage = workspace_path / stage_name
    fd = _open_workspace_root_fd(data_root, workspace_id)
    switched = False
    after_digest: str | None = None
    try:
        before = capture_development_tree(root, f"{workspace_id}/tree", reject_vcs=True)
        if before.digest != expected_digest:
            raise AgentWorkspaceStorageError(
                "workspace_import_bytes_changed", "Workspace bytes changed."
            )
        # A retained staging tree is reconciliation evidence and is never reused.
        _copy_normalized_tree(workspace_path / "tree", stage)
        stage_fd = os.open(stage_name, _DIR, dir_fd=fd)
        try:
            for path, payload in files.items():
                parts = normalize_workspace_relative_path(path, allow_root=False)
                parent = os.dup(stage_fd)
                try:
                    for part in parts[:-1]:
                        next_fd = os.open(part, _DIR, dir_fd=parent)
                        os.close(parent)
                        parent = next_fd
                    prior = before.by_path().get(path)
                    mode = 0o644 if prior is None else prior.mode
                    temporary = f".import-write-{secrets.token_hex(16)}"
                    _write_private_file(parent, temporary, payload, mode)
                    os.replace(
                        temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent
                    )
                    os.fsync(parent)
                finally:
                    os.close(parent)
        finally:
            os.close(stage_fd)
        after = capture_development_tree(
            root, f"{workspace_id}/{stage_name}", reject_vcs=True
        )
        after_digest = after.digest
        if (
            capture_development_tree(root, f"{workspace_id}/tree").digest
            != expected_digest
        ):
            raise AgentWorkspaceStorageError(
                "workspace_import_bytes_changed", "Workspace bytes changed."
            )
        prepared(after_digest)
        _exchange(fd, "tree", fd, stage_name)
        switched = True
        if (
            capture_development_tree(root, f"{workspace_id}/tree").digest
            != after_digest
        ):
            raise AgentWorkspaceReconciliationRequired(
                "workspace_import_reconciliation_required", "Imported bytes changed."
            )
        yield after_digest
    except AgentWorkspaceReconciliationRequired:
        # A database commit with an unavailable outcome must retain both trees;
        # rolling back without durable publication evidence could split identity.
        raise
    except BaseException:
        if switched:
            try:
                if (
                    capture_development_tree(root, f"{workspace_id}/tree").digest
                    != after_digest
                    or capture_development_tree(
                        root, f"{workspace_id}/{stage_name}"
                    ).digest
                    != expected_digest
                ):
                    raise ValueError("Unproven import rollback")
                _exchange(fd, "tree", fd, stage_name)
                if (
                    capture_development_tree(root, f"{workspace_id}/tree").digest
                    != expected_digest
                ):
                    raise ValueError("Import rollback digest changed")
            except BaseException as error:
                raise AgentWorkspaceReconciliationRequired(
                    "workspace_import_reconciliation_required",
                    "Import requires reconciliation; retained trees were preserved.",
                ) from error
        raise
    finally:
        os.close(fd)
    # Old trees are retained with the durable import receipt. Reference-aware
    # cleanup is separate; interrupted operations never discard either tree.

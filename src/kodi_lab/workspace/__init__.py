"""Unprivileged protected agent-workspace storage."""

from kodi_lab.workspace.storage import (
    MAX_DIRECTORY_ENTRIES,
    MAX_TEXT_READ_BYTES,
    AgentWorkspaceStorageError,
    WorkspaceCreationResult,
    WorkspaceTreeEntry,
    create_workspace_storage,
    inspect_workspace_directory,
    read_workspace_text,
    remove_unpublished_workspace_storage,
    workspace_baseline_published,
)

__all__ = [
    "MAX_DIRECTORY_ENTRIES",
    "MAX_TEXT_READ_BYTES",
    "AgentWorkspaceStorageError",
    "WorkspaceCreationResult",
    "WorkspaceTreeEntry",
    "create_workspace_storage",
    "inspect_workspace_directory",
    "read_workspace_text",
    "remove_unpublished_workspace_storage",
    "workspace_baseline_published",
]

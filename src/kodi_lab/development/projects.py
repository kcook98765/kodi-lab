"""Bounded Web-admin project selection beneath the configured source root."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kodi_lab.development.confined_fs import (
    open_development_root,
    open_source_component,
)
from kodi_lab.development.sources import (
    DevelopmentSourceValidationResult,
    DevelopmentSourceValidator,
)
from kodi_lab.domain import normalize_development_source_locator

MAX_PROJECT_ENTRIES = 128
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class LocalProjectError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.safe_message = code, message
        super().__init__(message)


class LocalProjectResolver:
    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._validator = DevelopmentSourceValidator(root)

    @contextmanager
    def _directory(self, locator: str | None = None) -> Iterator[int]:
        if self._root is None:
            raise LocalProjectError(
                "development_root_not_configured",
                "Ask the administrator to configure a local development root first.",
            )
        descriptor = open_development_root(self._root)
        try:
            if locator is not None:
                for component in locator.split("/"):
                    child = open_source_component(
                        descriptor, component, _DIRECTORY_FLAGS
                    )
                    os.close(descriptor)
                    descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _entries(descriptor: int) -> list[str]:
        # Do not materialize an unbounded os.listdir result.
        with os.scandir(descriptor) as entries:
            result = []
            for entry in entries:
                result.append(entry.name)
                if len(result) > MAX_PROJECT_ENTRIES:
                    raise LocalProjectError(
                        "project_discovery_limit",
                        "This directory has too many entries. Select the installable "
                        "add-on subdirectory directly, or use Advanced source setup.",
                    )
        return sorted(result)

    def discover(self) -> list[str]:
        """List immediate non-hidden directories without inspecting their contents."""
        with self._directory() as descriptor:
            return [
                name
                for name in self._entries(descriptor)
                if not name.startswith(".")
                and stat.S_ISDIR(
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
                )
            ]

    def resolve(self, project: str) -> DevelopmentSourceValidationResult:
        try:
            locator = normalize_development_source_locator(project)
        except (TypeError, ValueError) as error:
            raise LocalProjectError(
                "project_path_invalid",
                "Choose a project relative to the approved development root; "
                "absolute paths and parent traversal are not allowed.",
            ) from error
        candidates = []
        with self._directory(locator) as descriptor:
            names = self._entries(descriptor)
            if "addon.xml" in names:
                candidates.append(locator)
            for name in names:
                if name.startswith("."):
                    continue
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise LocalProjectError(
                        "project_link_unsafe",
                        "Project selection contains a symlink. Use real directories "
                        "inside the approved development root.",
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                child = open_source_component(descriptor, name, _DIRECTORY_FLAGS)
                try:
                    try:
                        os.stat("addon.xml", dir_fd=child, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    candidates.append(f"{locator}/{name}")
                finally:
                    os.close(child)
        if len(candidates) != 1:
            raise LocalProjectError(
                "project_layout_unsupported",
                "Choose a directory with addon.xml, or a project with exactly one "
                "immediate add-on subdirectory. For multiple add-ons, select the "
                "installable subdirectory explicitly.",
            )
        return self._validator.validate(candidates[0])

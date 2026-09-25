"""Explicit POSIX private state bootstrap for an installed remote host."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path


class HostBootstrapError(RuntimeError):
    """The requested installed-host state cannot be initialized safely."""


def explicit_data_root() -> Path:
    """Require an operator-chosen root rather than a deployment default."""

    raw = os.environ.get("KODI_LAB_DATA_ROOT", "")
    root = Path(raw)
    if not raw or not root.is_absolute() or root == Path("/"):
        raise HostBootstrapError("KODI_LAB_DATA_ROOT must be an absolute private path")
    if Path(os.path.normpath(raw)) != root:
        raise HostBootstrapError("KODI_LAB_DATA_ROOT must be normalized")
    return root


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise HostBootstrapError(f"private state directory is unsafe: {path}")


def initialize_state(root: Path) -> None:
    """Create only the private directories used by remote-host state."""

    for path in (
        root,
        root / "database",
        root / "secrets",
        root / "secrets" / "external-targets",
        root / "secrets" / "external-bridges",
    ):
        ensure_private_directory(path)


def generate_master_key(root: Path) -> Path:
    """Write a new 256-bit base64url key once, without exposing its value."""

    if os.environ.get("KODI_LAB_EXTERNAL_TARGET_MASTER_KEY"):
        raise HostBootstrapError("a master key is already configured")
    ensure_private_directory(root)
    path = root / "master-key"
    key = base64.urlsafe_b64encode(os.urandom(32)) + b"\n"
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(key)
            stream.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path

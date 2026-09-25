"""Read-only identity inventory for offline recovery of published snapshots."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID

from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.snapshot.storage import MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES


def published_snapshot_inventory(
    data_root: Path, *, web_gid: int
) -> dict[str, dict[str, str]]:
    """Check the fixed broker-owned tree without extracting or changing it."""

    root = data_root / "snapshots"
    if not root.exists() and not root.is_symlink():
        return {}

    def require(path: Path, *, directory: bool, mode: int) -> os.stat_result:
        item = path.lstat()
        if (
            (
                not stat.S_ISDIR(item.st_mode)
                if directory
                else not stat.S_ISREG(item.st_mode)
            )
            or item.st_uid != 0
            or item.st_gid != web_gid
            or stat.S_IMODE(item.st_mode) != mode
            or (not directory and item.st_nlink != 1)
        ):
            raise HostBootstrapError("published snapshot ownership is unsafe")
        return item

    require(root, directory=True, mode=0o750)
    result: dict[str, dict[str, str]] = {}
    for directory in sorted(root.iterdir()):
        if directory.name == ".staging":
            require(directory, directory=True, mode=0o700)
            continue
        try:
            identifier = UUID(directory.name)
        except ValueError as error:
            raise HostBootstrapError(
                "snapshot recovery contains an unknown entry"
            ) from error
        if str(identifier) != directory.name or len(result) >= 10000:
            raise HostBootstrapError("snapshot recovery inventory is invalid")
        require(directory, directory=True, mode=0o750)
        if {entry.name for entry in directory.iterdir()} != {
            "manifest.json",
            "state.tar",
        }:
            raise HostBootstrapError("snapshot recovery artifact inventory is invalid")
        hashes: dict[str, str] = {}
        for name, maximum in (
            ("manifest.json", MAX_MANIFEST_BYTES),
            ("state.tar", MAX_ARCHIVE_BYTES),
        ):
            path = directory / name
            item = require(path, directory=False, mode=0o440)
            if not 1 <= item.st_size <= maximum:
                raise HostBootstrapError("snapshot recovery artifact size is invalid")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[name] = digest.hexdigest()
        result[directory.name] = hashes
    return result

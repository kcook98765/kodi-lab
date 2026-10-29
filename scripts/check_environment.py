"""Record release scratch selection without changing admission or cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.test_timing import resources, source_identity


def filesystem(path: Path) -> dict[str, Any]:
    info = os.statvfs(path)
    kind = subprocess.run(
        ["stat", "-f", "-c", "%T", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    return {
        "type": kind,
        "free_bytes": info.f_bavail * info.f_frsize,
        "total_bytes": info.f_blocks * info.f_frsize,
    }


def environment(parent: Path) -> dict[str, Any]:
    selected = filesystem(parent)
    tmpfs = None
    try:
        if os.access("/dev/shm", os.W_OK):
            tmpfs = filesystem(Path("/dev/shm"))
    except (OSError, subprocess.SubprocessError):
        pass
    suitable = bool(
        tmpfs and tmpfs["type"] == "tmpfs" and tmpfs["free_bytes"] >= 3 * 1024**3
    )
    return {
        "schema_version": 1,
        "source": source_identity(Path.cwd()),
        "explicit_pytest_parent": "KODI_LAB_CHECK_PYTEST_TMP_PARENT" in os.environ,
        "selected_filesystem": selected,
        "tmpfs": tmpfs,
        "disk_with_suitable_tmpfs": selected["type"] != "tmpfs" and suitable,
        "resources": resources(parent),
    }


def main() -> None:
    parent, output = map(Path, sys.argv[1:])
    document = environment(parent)
    with output.open("x") as stream:
        json.dump(document, stream, indent=2, allow_nan=False)
        stream.write("\n")
    if document["disk_with_suitable_tmpfs"]:
        print(
            "Warning: disk-backed pytest scratch selected while /dev/shm "
            "appears suitable (at least 3 GiB free). Explicit overrides remain "
            "supported for capacity needs; selection is unchanged.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()

"""Shared read-only storage snapshot format and filesystem observations."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

SNAPSHOT_FILE = "storage-inventory.json"
SNAPSHOT_SCHEMA = 1
MAX_SNAPSHOT_BYTES = 256 * 1024
WARNING_BYTES = 12 * 1024**3
STALE_SECONDS = 120
Document = dict[str, Any]


def read_file(path: Path, limit: int) -> bytes:
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb"
    ) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Unsupported inventory source")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Inventory source exceeded its size limit")
        return data


def read_inventory(path: Path) -> Document:
    document = json.loads(read_file(path, MAX_SNAPSHOT_BYTES))
    if not isinstance(document, dict) or document.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("Unknown storage inventory format")
    if any(
        not isinstance(document.get(name), list) for name in ("filesystems", "profiles")
    ) or any(
        not isinstance(document.get(name), dict)
        for name in (
            "snapshots",
            "workers",
            "prepare",
            "engine",
            "unattributed_profiles",
        )
    ):
        raise ValueError("Incomplete storage inventory")
    return dict(document)


def observe_filesystem(path: Path, scope: str) -> Document:
    try:
        info = path.stat()
        usage = os.statvfs(path)
        capacity = usage.f_frsize * usage.f_blocks
        available = max(0, usage.f_frsize * usage.f_bavail)
    except OSError:
        return {"scope": scope, "source": "Statvfs", "state": "unavailable"}
    return {
        "scope": scope,
        "source": "Statvfs",
        "state": "measured",
        "device": info.st_dev,
        "capacity": capacity,
        "available": available,
        "warning": available < WARNING_BYTES
        or (capacity > 0 and available * 10 < capacity),
    }


def distinct_filesystems(observations: list[Document]) -> list[Document]:
    measured: dict[int, Document] = {}
    unavailable: list[Document] = []
    for row in observations:
        if row.get("state") != "measured":
            unavailable.append({**row, "scopes": [row["scope"]]})
            continue
        if row["device"] not in measured:
            measured[row["device"]] = {**row, "scopes": []}
        first = measured[row["device"]]
        first["scopes"].append(row["scope"])
        if (first["stale"] and not row["stale"]) or (
            first["stale"] == row["stale"]
            and row["age"] is not None
            and (first["age"] is None or row["age"] < first["age"])
        ):
            first.update({**row, "scopes": first["scopes"]})
    return [
        {key: value for key, value in row.items() if key != "device"}
        for row in [*measured.values(), *unavailable]
    ]

"""Shared read-only storage snapshot format and filesystem observations."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

SNAPSHOT_FILE = "storage-inventory.json"
SNAPSHOT_SCHEMA = 1
MAX_SNAPSHOT_BYTES = 256 * 1024
WARNING_BYTES = 12 * 1024**3
STALE_SECONDS = 120
Document = dict[str, Any]


Count = Annotated[int, Field(ge=0)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
MeasureState = Literal["measured", "unavailable", "partial", "changed", "unprepared"]


class InventoryProjection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class Filesystem(InventoryProjection):
    scope: Literal["Managed state"]
    source: Literal["Statvfs"]
    state: Literal["measured", "unavailable"]
    device: Count | None = None
    capacity: Count | None = None
    available: Count | None = None
    warning: bool | None = None


class Usage(InventoryProjection):
    bytes: Count | None
    state: MeasureState


class Profile(InventoryProjection):
    version: Annotated[str, Field(max_length=40)]
    generation: Literal["matrix", "nexus", "omega", "piers"]
    profile: Usage
    image: Digest | None
    reviewed_image: Digest | None


class Archive(InventoryProjection):
    name: Annotated[str, Field(max_length=200)]
    bytes: Count


class Snapshots(InventoryProjection):
    state: MeasureState
    items: Annotated[list[Archive], Field(max_length=160)]


class Backlog(InventoryProjection):
    state: MeasureState
    count: Count | None = None
    capacity: Count | None = None
    blocks_new: bool | None = None
    oldest_seconds: Count | None = None
    oldest_observed_seconds: Count | None = None
    reason: Annotated[str, Field(max_length=100)] | None = None
    reasons: (
        Annotated[list[Annotated[str, Field(max_length=100)]], Field(max_length=16)]
        | None
    ) = None
    grace: Count | None = None
    review: Count | None = None
    scratch_count: Count | None = None


class RuntimeImage(InventoryProjection):
    identity: Digest
    bytes: Count | None


class Engine(InventoryProjection):
    state: Literal["measured", "unavailable", "partial", "collecting"]
    scope: Literal["WSL 2 guest Engine", "Docker Engine"] | None = None
    images: Annotated[list[RuntimeImage], Field(max_length=8)]


class Unattributed(InventoryProjection):
    count: Count | None = None
    bytes: Count | None = None
    state: MeasureState | None = None


class StorageSnapshot(InventoryProjection):
    schema_version: Literal[1] = Field(alias="schema")
    measured_at: FiniteFloat
    filesystems: Annotated[list[Filesystem], Field(max_length=4)]
    profiles: Annotated[list[Profile], Field(max_length=4)]
    snapshots: Snapshots
    workers: Backlog
    prepare: Backlog
    engine: Engine
    unattributed_profiles: Unattributed


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
    snapshot = StorageSnapshot.model_validate_json(read_file(path, MAX_SNAPSHOT_BYTES))
    for row in snapshot.filesystems:
        if row.state == "measured" and None in (
            row.device,
            row.capacity,
            row.available,
            row.warning,
        ):
            raise ValueError("Incomplete filesystem measurement")
    return snapshot.model_dump(exclude_unset=True, by_alias=True)


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
    measured: dict[tuple[int, int, int], Document] = {}
    unavailable: list[Document] = []
    for row in observations:
        if row.get("state") != "measured":
            unavailable.append({**row, "scopes": [row["scope"]]})
            continue
        identity = (row["device"], row["capacity"], row["available"])
        if identity not in measured:
            measured[identity] = {**row, "scopes": []}
        first = measured[identity]
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

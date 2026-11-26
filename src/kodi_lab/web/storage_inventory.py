"""Read the site storage snapshot off the Web request path."""

from __future__ import annotations

import time
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from typing import Any

from kodi_lab.storage.inventory_snapshot import (
    SNAPSHOT_FILE,
    STALE_SECONDS,
    Document,
    distinct_filesystems,
    observe_filesystem,
    read_inventory,
)


class StorageInventory:
    def __init__(
        self, runtime_root: Path, data_root: Path, repository_root: Path
    ) -> None:
        self._snapshot_path = runtime_root / SNAPSHOT_FILE
        self._paths = (
            (data_root, "Application data"),
            (repository_root, "Repository data"),
        )
        self._stopped = Event()
        self._broker: Document = {}
        self._local: tuple[float | None, list[Document]] = (None, [])

    def start(self) -> None:
        self._stopped.set()
        self._stopped = Event()
        Thread(
            target=self._collect,
            args=(self._stopped,),
            name="web-storage-inventory",
            daemon=True,
        ).start()

    def close(self) -> None:
        self._stopped.set()

    def _collect(self, stopped: Event) -> None:
        while not stopped.is_set():
            self.refresh()
            stopped.wait(10)

    def refresh(self) -> None:
        rows = [observe_filesystem(path, scope) for path, scope in self._paths]
        self._local = (time.time(), rows)
        # Keep the most recent verified sample with its original timestamp.
        with suppress(OSError, ValueError, RecursionError):
            self._broker = read_inventory(self._snapshot_path)

    def snapshot(self) -> Document:
        broker = self._broker
        age = self.age(broker.get("measured_at"))
        stale = age is None or age > STALE_SECONDS
        local_time, local_rows = self._local
        local_age = self.age(local_time)
        local_stale = local_age is None or local_age > STALE_SECONDS
        observations = [
            *({**row, "age": local_age, "stale": local_stale} for row in local_rows),
            *(
                {**row, "age": age, "stale": stale}
                for row in broker.get("filesystems", [])
            ),
        ]
        warning = any(row.get("warning") and not row["stale"] for row in observations)
        filesystems = distinct_filesystems(observations)
        return {
            "filesystems": filesystems,
            "age": age,
            "stale": stale,
            "local_age": local_age,
            "warning": warning,
            "profiles": broker.get("profiles", []),
            "snapshots": broker.get("snapshots", {"state": "unavailable", "items": []}),
            "workers": broker.get("workers", {"state": "unavailable"}),
            "prepare": broker.get("prepare", {"state": "unavailable"}),
            "engine": broker.get("engine", {"state": "unavailable", "images": []}),
            "unattributed_profiles": broker.get(
                "unattributed_profiles", {"state": "unavailable"}
            ),
        }

    @staticmethod
    def age(value: Any) -> int | None:
        if not isinstance(value, (float, int)) or not 0 < value <= time.time():
            return None
        return int(time.time() - value)


def byte_size(value: int | None) -> str:
    if value is None:
        return "Unknown"
    if value < 1024:
        return f"{value} B"
    size = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
    raise AssertionError("unreachable size unit")

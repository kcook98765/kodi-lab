from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from kodi_lab.broker import storage_inventory as broker_storage
from kodi_lab.storage.inventory_snapshot import SNAPSHOT_FILE, Document, read_inventory
from kodi_lab.web import storage_inventory as web_storage
from tests.web.conftest import login

IMAGE = "sha256:" + "a" * 64
SLOT = "b" * 32


def test_storage_requires_session_and_uses_cached_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def snapshot(_cache: web_storage.StorageInventory) -> Document:
        return {
            "warning": True,
            "age": 10,
            "stale": False,
            "profiles": [],
            "engine": {
                "state": "unavailable",
                "images": [{"identity": IMAGE, "bytes": None}],
            },
            "filesystems": [
                {
                    "scopes": ["Application data"],
                    "source": "Statvfs",
                    "available": 0,
                    "capacity": 1024**3,
                    "warning": True,
                    "stale": False,
                    "age": 5,
                }
            ],
            "snapshots": {"state": "measured", "items": []},
            "workers": {"count": 128, "capacity": 128, "blocks_new": True},
            "prepare": {"count": 6, "capacity": 6, "blocks_new": True},
            "unattributed_profiles": {},
        }

    monkeypatch.setattr(web_storage.StorageInventory, "snapshot", snapshot)
    assert client.get("/api/storage-maintenance").status_code == 401
    assert client.get("/storage-maintenance").status_code == 401
    login(client)
    result = client.get("/api/storage-maintenance")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    page = client.get("/storage-maintenance")
    assert page.status_code == 200
    assert "128 of 128 cleanup slots used" in page.text
    assert "6 of 6 cleanup slots used" in page.text
    assert "0 B" in page.text and "Unknown" in page.text
    assert "Windows host availability is unknown" in page.text
    assert "Storage is running low" in client.get("/kodi").text
    assert "/storage-maintenance" in client.get("/managed-kodi").text
    assert client.post("/api/storage-maintenance").status_code == 405


def test_background_engine_delay_keeps_committed_inventory_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    runtime = tmp_path / "runtime"
    managed.mkdir()
    runtime.mkdir()
    profile = managed / "volumes" / SLOT
    profile.mkdir(parents=True)
    (managed / "profile").write_text(SLOT)
    (managed / "image").write_text(IMAGE)
    asset = profile / "settings"
    asset.write_bytes(b"sample" * 4096)
    (profile / "same-settings").hardlink_to(asset)
    (profile / "unrelated").symlink_to(tmp_path, target_is_directory=True)
    second = managed / "nexus"
    second.mkdir()
    (second / "image").write_text(IMAGE)
    (second / "profile").write_text("c" * 32)
    snapshot_path = managed / "snapshots"
    snapshot_path.mkdir()
    archive = snapshot_path / "Fixture.tar.gz"
    archive.write_bytes(b"fixture-only")
    archive.with_suffix(".json").write_text("{}")

    started, release, stopped = Event(), Event(), Event()

    def engine(images: set[str]) -> Document:
        assert images == {IMAGE}
        started.set()
        assert release.wait(3)
        return {"state": "unavailable", "images": [{"identity": IMAGE, "bytes": None}]}

    monkeypatch.setattr(broker_storage, "engine_inventory", engine)
    worker = Thread(
        target=broker_storage.serve_storage_inventory,
        args=(stopped, managed, runtime, os.getgid()),
        daemon=True,
    )
    worker.start()
    try:
        assert started.wait(2)
        cache = web_storage.StorageInventory(runtime, tmp_path, tmp_path)
        cache.refresh()
        data = cache.snapshot()
        assert data["stale"] is False
        assert len(data["filesystems"]) == 1
        assert set(data["filesystems"][0]["scopes"]) == {
            "Application data",
            "Repository data",
            "Managed state",
        }
        omega = next(row for row in data["profiles"] if row["generation"] == "omega")
        assert omega["profile"] == {
            "bytes": asset.stat().st_blocks * 512,
            "state": "measured",
        }
        assert data["engine"]["state"] == "collecting"
        assert data["engine"]["images"] == [{"identity": IMAGE, "bytes": None}]
        assert data["snapshots"]["items"] == [
            {"name": "Fixture", "bytes": len(b"fixture-only")}
        ]
    finally:
        stopped.set()
        release.set()
        worker.join(4)
    assert read_inventory(runtime / SNAPSHOT_FILE)["engine"]["state"] == "unavailable"


def test_stale_broker_measurement_does_not_raise_fresh_storage_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path
    row: Document = {
        "scope": "Managed state",
        "device": 1,
        "source": "Statvfs",
        "state": "measured",
        "capacity": 100,
        "available": 0,
        "warning": True,
    }
    doc: Document = {
        "schema": 1,
        "measured_at": time.time() - 600,
        "filesystems": [row],
        "profiles": [],
        "snapshots": {"state": "partial", "items": []},
        "engine": {"state": "unavailable", "images": []},
        "workers": {},
        "prepare": {},
        "unattributed_profiles": {},
    }

    def filesystem(_path: Path, scope: str) -> Document:
        return {**row, "scope": scope, "available": 70, "warning": False}

    monkeypatch.setattr(web_storage, "observe_filesystem", filesystem)
    broker_storage.publish_snapshot(runtime / SNAPSHOT_FILE, os.getgid(), doc)
    cache = web_storage.StorageInventory(runtime, tmp_path, tmp_path)
    cache.refresh()
    assert cache.snapshot()["stale"] is True
    assert cache.snapshot()["warning"] is False
    assert cache.snapshot()["filesystems"][0]["available"] == 70
    (runtime / SNAPSHOT_FILE).write_text('{"schema":1,"filesystems":null}')
    cache.refresh()
    assert cache.snapshot()["snapshots"]["state"] == "partial"

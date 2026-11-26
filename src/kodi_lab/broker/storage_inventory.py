"""Collect bounded, read-only observations from the installed broker's mounts."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from uuid import uuid4

from kodi_lab.broker.worker_receipts import RECEIPT_LIMIT, WorkerReceipts
from kodi_lab.managed import RELEASES, reviewed_image_file
from kodi_lab.managed.prepare import (
    ABANDONED_SCRATCH_GRACE,
    PREPARE_CLEANUP_CAPACITY,
    valid_prepare_receipt,
)
from kodi_lab.storage.inventory_snapshot import (
    MAX_SNAPSHOT_BYTES,
    SNAPSHOT_FILE,
    SNAPSHOT_SCHEMA,
    Document,
    observe_filesystem,
    read_file,
)

SAMPLE_INTERVAL = 30
ENTRY_LIMIT = 16_000
SNAPSHOT_LIMIT = 160
ENGINE_TIMEOUT = 1.25
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
SLOT = re.compile(r"[0-9a-f]{32}")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")


class InventoryLimit(Exception):
    pass


class WalkBudget:
    def __init__(self) -> None:
        self.remaining = ENTRY_LIMIT
        self.deadline = time.monotonic() + 0.75

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0 or time.monotonic() >= self.deadline:
            raise InventoryLimit


def directory(path: Path) -> bool:
    return stat.S_ISDIR(path.lstat().st_mode)


def allocated_bytes(root: Path, budget: WalkBudget) -> Document:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    total = 0
    try:
        descriptor = os.open(root, flags)
    except OSError:
        return {"bytes": None, "state": "unavailable"}
    try:
        base = os.fstat(descriptor)
        seen = {(base.st_dev, base.st_ino)}

        def walk(folder: int, depth: int) -> None:
            nonlocal total
            if depth > 32:
                raise InventoryLimit
            with os.scandir(folder) as entries:
                for entry in entries:
                    budget.consume()
                    info = entry.stat(follow_symlinks=False)
                    identity = (info.st_dev, info.st_ino)
                    if info.st_dev != base.st_dev:
                        raise InventoryLimit
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, flags, dir_fd=folder)
                        try:
                            current = os.fstat(child)
                            if (current.st_dev, current.st_ino) != identity:
                                raise InventoryLimit
                            total += info.st_blocks * 512
                            walk(child, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_blocks * 512

        walk(descriptor, 0)
    except FileNotFoundError:
        return {"bytes": None, "state": "changed"}
    except (OSError, InventoryLimit):
        return {"bytes": total if total else None, "state": "partial"}
    finally:
        os.close(descriptor)
    return {"bytes": total, "state": "measured"}


def text_identity(path: Path, expression: re.Pattern[str]) -> str | None:
    try:
        value = read_file(path, 128).decode().strip()
    except (OSError, ValueError):
        return None
    return value if expression.fullmatch(value) else None


def profile_inventory(
    root: Path, runtime_root: Path, budget: WalkBudget
) -> tuple[list[Document], set[str]]:
    result: list[Document] = []
    images: set[str] = set()
    for generation, (major, minor) in RELEASES.items():
        state_root = root if generation == "omega" else root / generation
        pointer = state_root / "profile"
        slot = text_identity(pointer, SLOT)
        pin = text_identity(state_root / "image", IMAGE) if slot else None
        ready = text_identity(runtime_root / reviewed_image_file(generation), IMAGE)
        images.update(identity for identity in (pin, ready) if identity is not None)
        usage: Document = {"bytes": None, "state": "unprepared"}
        if slot:
            usage = allocated_bytes(state_root / "volumes" / slot, budget)
            if text_identity(pointer, SLOT) != slot:
                usage = {"bytes": None, "state": "changed"}
        elif pointer.exists() or pointer.is_symlink():
            usage["state"] = "unavailable"
        result.append(
            {
                "version": f"{generation.title()} {major}.{minor}",
                "generation": generation,
                "profile": usage,
                "image": pin,
                "reviewed_image": ready,
            }
        )
    return result, images


def unattributed_profile_inventory(root: Path) -> Document:
    count = 0
    inspected = 0
    try:
        for generation in RELEASES:
            state_root = root if generation == "omega" else root / generation
            folder = state_root / "volumes"
            if not folder.exists():
                continue
            if not directory(folder):
                raise InventoryLimit
            pointer = state_root / "profile"
            active = text_identity(pointer, SLOT)
            if active is None and (pointer.exists() or pointer.is_symlink()):
                raise InventoryLimit
            with os.scandir(folder) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > 256:
                        raise InventoryLimit
                    if entry.name != active and entry.is_dir(follow_symlinks=False):
                        count += 1
            if text_identity(pointer, SLOT) != active:
                raise InventoryLimit
    except (OSError, InventoryLimit):
        return {"count": None, "bytes": None, "state": "partial"}
    return {"count": count, "bytes": None, "state": "measured"}


def snapshot_directories(root: Path) -> Iterator[tuple[str, Path]]:
    yield ("Administrator", root / "snapshots")
    agents = root / "agents"
    if not directory(agents):
        return
    with os.scandir(agents) as entries:
        for count, entry in enumerate(entries):
            if count >= SNAPSHOT_LIMIT:
                raise InventoryLimit
            if entry.is_dir(follow_symlinks=False):
                yield ("Agent", Path(entry.path) / "snapshots")


def snapshot_inventory(root: Path) -> Document:
    results: list[Document] = []
    observed = 0
    try:
        for owner, folder in snapshot_directories(root):
            if not folder.exists() or not directory(folder):
                continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    observed += 1
                    if observed > SNAPSHOT_LIMIT * 4 or len(results) >= SNAPSHOT_LIMIT:
                        raise InventoryLimit
                    if not entry.name.endswith(".tar.gz") or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                    companion = Path(entry.path).with_suffix(".json")
                    if companion.is_symlink() or not companion.is_file():
                        continue
                    if owner == "Administrator":
                        name = entry.name.removesuffix(".tar.gz")
                    else:
                        # Agent names can contain private project information.
                        name = "Agent snapshot"
                    results.append(
                        {
                            "name": name,
                            "bytes": entry.stat(follow_symlinks=False).st_size,
                        }
                    )
    except FileNotFoundError:
        pass
    except (OSError, InventoryLimit):
        return {"items": results, "state": "partial"}
    return {"items": results, "state": "measured"}


def installer_backlog(root: Path) -> Document:
    folder = root / "reviewed-worker-receipts" / "pending"
    count = 0
    oldest: tuple[float, str] | None = None
    try:
        if not directory(folder):
            raise ValueError("Worker inventory directory unavailable")
        with os.scandir(folder) as entries:
            for index, item in enumerate(entries):
                if index >= RECEIPT_LIMIT * 4:
                    raise InventoryLimit
                if not item.name.endswith(".json") or not SLOT.fullmatch(
                    item.name[:-5]
                ):
                    continue
                count += 1
                changed = item.stat(follow_symlinks=False).st_mtime
                if oldest is not None and changed >= oldest[0]:
                    continue
                try:
                    receipt = WorkerReceipts.read(Path(item.path))
                except (ValueError, OSError):
                    reason = "Worker receipt needs review"
                else:
                    if receipt.diagnostics:
                        reason = "Terminal worker removal pending"
                    elif receipt.container:
                        reason = "Exact worker needs reconciliation"
                    else:
                        reason = "Worker dispatch is unconfirmed"
                oldest = (changed, reason)
    except FileNotFoundError:
        if count:
            return {"count": None, "capacity": RECEIPT_LIMIT, "state": "partial"}
    except (OSError, ValueError, InventoryLimit):
        return {"count": None, "capacity": RECEIPT_LIMIT, "state": "unavailable"}
    return {
        "count": count,
        "capacity": RECEIPT_LIMIT,
        "state": "measured",
        "blocks_new": count >= RECEIPT_LIMIT,
        "oldest_seconds": max(0, int(time.time() - oldest[0])) if oldest else None,
        "reason": oldest[1] if oldest else None,
    }


def prepare_inventory(root: Path) -> Document:
    try:
        journal = json.loads(read_file(root / "operation.json", 96 * 1024))
        if not isinstance(journal, dict) or journal.get("state") not in {
            "pending",
            "complete",
        }:
            raise ValueError("Invalid Prepare journal")
        obligations = journal.get("prepare_cleanup", {})
        if not isinstance(obligations, dict):
            raise ValueError("Invalid Prepare cleanup collection")
    except FileNotFoundError:
        obligations = {}
    except (OSError, ValueError, RecursionError):
        return {
            "count": None,
            "capacity": PREPARE_CLEANUP_CAPACITY,
            "state": "unavailable",
        }
    if len(obligations) > PREPARE_CLEANUP_CAPACITY:
        return {"count": None, "capacity": PREPARE_CLEANUP_CAPACITY, "state": "partial"}
    now = time.time()
    reasons: set[str] = set()
    known_ages: list[int] = []
    grace = 0
    review = 0
    scratch_count = 0
    for operation, receipt in obligations.items():
        if not valid_prepare_receipt(operation, receipt, terminal=True) or (
            receipt["generation"] not in RELEASES
        ):
            reasons.add("Receipt needs review")
            review += 1
            continue
        if "scratch" in receipt:
            scratch_count += 1
            if receipt["scratch_after"]:
                known_ages.append(
                    max(
                        0, int(now - receipt["scratch_after"] + ABANDONED_SCRATCH_GRACE)
                    )
                )
            if receipt.get("scratch_allocation") is None:
                reasons.add("Scratch allocation is unconfirmed")
                review += 1
            if now < receipt["scratch_after"]:
                reasons.add("Scratch is in its grace period")
                grace += 1
        if "engine" in receipt:
            reasons.add(
                "Inspection removal pending"
                if receipt.get("container")
                else "Inspection dispatch is unconfirmed"
            )
    return {
        "state": "measured",
        "count": len(obligations),
        "capacity": PREPARE_CLEANUP_CAPACITY,
        "blocks_new": len(obligations) >= PREPARE_CLEANUP_CAPACITY,
        "grace": grace,
        "review": review,
        "reasons": sorted(reasons),
        "scratch_count": scratch_count,
        "oldest_observed_seconds": max(known_ages, default=None),
    }


def engine_inventory(images: set[str]) -> Document:
    unknown = [{"identity": identity, "bytes": None} for identity in sorted(images)]
    try:
        outcome = subprocess.run(
            [
                *DOCKER,
                "info",
                "--format",
                "{{.OSType}}|{{.OperatingSystem}}|{{.KernelVersion}}",
            ],
            capture_output=True,
            text=True,
            timeout=ENGINE_TIMEOUT,
            check=True,
        )
        system = outcome.stdout.lower()
        if not system.strip():
            raise ValueError("Empty Engine info")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"state": "unavailable", "images": unknown}
    result: Document = {
        "state": "measured",
        "scope": "WSL 2 guest Engine" if "microsoft" in system else "Docker Engine",
        "images": unknown,
    }
    if not images:
        return result
    try:
        details = subprocess.run(
            [
                *DOCKER,
                "image",
                "inspect",
                "--format",
                "{{.Id}} {{.Size}}",
                *sorted(images),
            ],
            capture_output=True,
            text=True,
            timeout=ENGINE_TIMEOUT,
            check=False,
        )
        sizes = {}
        for line in details.stdout.splitlines():
            identifier, size = line.split()
            if identifier in images and size.isdecimal():
                sizes[identifier] = int(size)
        for row in result["images"]:
            row["bytes"] = sizes.get(row["identity"])
        if len(sizes) < len(images):
            result["state"] = "partial"
    except (OSError, subprocess.SubprocessError, ValueError):
        result["state"] = "partial"
    return result


def publish_snapshot(path: Path, group: int, doc: Document) -> None:
    payload = json.dumps(doc, separators=(",", ":")).encode()
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Storage inventory snapshot grew beyond its limit")
    temporary = path.with_name(f".storage-{uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            os.fchown(stream.fileno(), -1, group)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def serve_storage_inventory(
    stopped: Event, managed_root: Path, runtime_root: Path, web_group: int
) -> None:
    while not stopped.is_set():
        try:
            profiles, images = profile_inventory(
                managed_root, runtime_root, WalkBudget()
            )
            carried = {
                "schema": SNAPSHOT_SCHEMA,
                "measured_at": time.time(),
                "filesystems": [observe_filesystem(managed_root, "Managed state")],
                "profiles": profiles,
                "snapshots": snapshot_inventory(managed_root),
                "workers": installer_backlog(runtime_root),
                "prepare": prepare_inventory(managed_root),
                "unattributed_profiles": unattributed_profile_inventory(managed_root),
                "engine": {
                    "state": "collecting",
                    "images": [
                        {"identity": identity, "bytes": None}
                        for identity in sorted(images)
                    ],
                },
            }
            publish_snapshot(runtime_root / SNAPSHOT_FILE, web_group, carried)
            carried["engine"] = engine_inventory(images)
            publish_snapshot(runtime_root / SNAPSHOT_FILE, web_group, carried)
        except (OSError, ValueError, TypeError, RecursionError):
            # Keep the last good snapshot and let the Web report its age.
            pass
        stopped.wait(SAMPLE_INTERVAL)

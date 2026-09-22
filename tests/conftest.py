from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.snapshot.storage import MIN_FREE_BYTES

_SCRATCH_PARENT_ENV = "KODI_LAB_TEST_SNAPSHOT_SCRATCH_PARENT"
_SCRATCH_ROOT_ENV = "KODI_LAB_TEST_SNAPSHOT_SCRATCH_ROOT"
_ROOT_PREFIX = "kodi-lab-snapshot-tests."
_MARKER_NAME = ".kodi-lab-test-scratch.json"


def _process_start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text()
    fields_after_name = raw[raw.rfind(")") + 2 :].split()
    return int(fields_after_name[19])


def _validate_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise pytest.UsageError(
            f"Snapshot test scratch root is not a directory: {path}"
        )
    if metadata.st_uid != os.getuid():
        raise pytest.UsageError(
            f"Snapshot test scratch root has the wrong owner: {path}"
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise pytest.UsageError(f"Snapshot test scratch root is not private: {path}")


def _write_marker(root: Path, run_id: str) -> None:
    marker = root / _MARKER_NAME
    document = {
        "schema": 1,
        "kind": "kodi-lab-snapshot-test-scratch",
        "run_id": run_id,
        "pid": os.getpid(),
        "process_start_ticks": _process_start_ticks(os.getpid()),
        "uid": os.getuid(),
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
    }
    with marker.open("x") as stream:
        json.dump(document, stream, sort_keys=True)
        stream.write("\n")
    marker.chmod(0o600)


def _remove_owned_tree(path: Path, *, parent: Path, run_id: str) -> None:
    if path.parent != parent or path.is_symlink():
        raise RuntimeError(
            f"Refusing to remove unsafe snapshot test scratch path: {path}"
        )
    _validate_private_directory(path)
    marker = path / _MARKER_NAME
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError(f"Snapshot test scratch marker is missing: {path}")
    document = json.loads(marker.read_text())
    if (
        document.get("schema") != 1
        or document.get("kind") != "kodi-lab-snapshot-test-scratch"
        or document.get("run_id") != run_id
        or document.get("uid") != os.getuid()
        or document.get("root") != str(path)
    ):
        raise RuntimeError(f"Snapshot test scratch marker is invalid: {path}")
    shutil.rmtree(path)


@pytest.fixture(scope="session")
def snapshot_scratch_root() -> Iterator[Path]:
    configured = os.environ.get(_SCRATCH_ROOT_ENV)
    owns_root = configured is None
    if configured is None:
        parent = Path(os.environ.get(_SCRATCH_PARENT_ENV, "/tmp"))
        if not parent.is_absolute() or parent.is_symlink() or not parent.is_dir():
            raise pytest.UsageError(
                f"Snapshot test scratch parent is not a safe directory: {parent}"
            )
        root = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX, dir=parent))
    else:
        root = Path(configured)
        if not root.is_absolute():
            raise pytest.UsageError("Snapshot test scratch root must be absolute")
        _validate_private_directory(root)
        if any(root.iterdir()):
            raise pytest.UsageError("Snapshot test scratch root must start empty")

    _validate_private_directory(root)
    run_id = str(uuid4())
    _write_marker(root, run_id)
    if shutil.disk_usage(root).free < MIN_FREE_BYTES:
        if owns_root:
            _remove_owned_tree(root, parent=root.parent, run_id=run_id)
        raise pytest.UsageError(
            "Snapshot test scratch storage does not satisfy the production "
            f"{MIN_FREE_BYTES}-byte free-space reserve"
        )

    try:
        yield root
    finally:
        if owns_root:
            _remove_owned_tree(root, parent=root.parent, run_id=run_id)


@pytest.fixture
def snapshot_data_root(snapshot_scratch_root: Path) -> Iterator[Path]:
    run_id = str(uuid4())
    path = snapshot_scratch_root / f"case.{run_id}"
    path.mkdir(mode=0o700)
    _write_marker(path, run_id)
    try:
        yield path
    finally:
        _remove_owned_tree(path, parent=snapshot_scratch_root, run_id=run_id)

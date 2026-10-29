"""Real application lock inodes survive supported backup and recovery."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab import remote_lifecycle as lifecycle
from kodi_lab.application.managed_agent_lifecycle import ManagedAgentLifecycleService
from kodi_lab.host_bootstrap import HostBootstrapError
from tests.storage.test_remote_lifecycle import _historical, _url, repository_migrations

# Keep the existing real migration fixture when running this file independently.
__all__ = ["repository_migrations"]
SCOPES = ("household-onboarding", "managed-agent-lifecycle", "external-reconnect")


def _lock(root: Path, scope: str) -> Path:
    parent = root / "locks" / scope
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    (root / "locks").chmod(0o700)
    path = parent / f"{uuid4()}.lock"
    path.touch(mode=0o600)
    return path


def _identity(path: Path) -> tuple[int, ...]:
    item = path.lstat()
    return (
        item.st_dev,
        item.st_ino,
        item.st_uid,
        item.st_gid,
        stat.S_IMODE(item.st_mode),
        item.st_nlink,
        item.st_size,
    )


def _dump(root: Path) -> list[str]:
    with sqlite3.connect(root / lifecycle.DATABASE) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return list(db.iterdump())


@pytest.mark.parametrize("scopes", [(s,) for s in SCOPES] + [SCOPES])
def test_backup_restore_preserves_held_and_later_application_locks(
    tmp_path: Path, scopes: tuple[str, ...]
) -> None:
    root = tmp_path / "state"
    _historical(root)
    before_database = _dump(root)
    key = (root / "master-key").read_bytes()
    with ExitStack() as stack:
        locks = [_lock(root, scope) for scope in scopes]
        for lock in locks:
            fd = stack.enter_context(lock.open("r+"))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = {lock: _identity(lock) for lock in locks}
        with lifecycle.lifecycle_lock(root):
            identifier = lifecycle.backup(root, _url(root))
        manifest = lifecycle._verify_backup(root, identifier)
        assert not (root / "backups" / identifier / "locks").exists()
        files = manifest["files"]
        assert isinstance(files, dict)
        assert not any(name.startswith("locks/") for name in files)
        assert {lock: _identity(lock) for lock in locks} == before
        previous, _, upgraded_backup = lifecycle.upgrade(root, _url(root))
        assert upgraded_backup is not None
        assert previous == manifest["revision"]
        later = [_lock(root, scope) for scope in scopes]
        for lock in later:
            fd = stack.enter_context(lock.open("r+"))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before[lock] = _identity(lock)
        lifecycle.restore(root, _url(root), upgraded_backup, upgraded_backup)
        assert {lock: _identity(lock) for lock in locks + later} == before
        for lock in locks + later:
            with lock.open("r+") as competing, pytest.raises(BlockingIOError):
                fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _dump(root) == before_database
        assert (root / "master-key").read_bytes() == key


def test_real_managed_lifecycle_lock_creator_is_backup_compatible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _historical(root)
    # Only the lock context is exercised; no lifecycle dependencies are invoked.
    service = object.__new__(ManagedAgentLifecycleService)
    service._locks = root / "locks" / "managed-agent-lifecycle"
    (root / "locks").mkdir(mode=0o700)
    target_id = uuid4()
    with service._lock(target_id):
        identifier = lifecycle.backup(root, _url(root))
        lock = service._locks / f"{target_id}.lock"
        original = _identity(lock)
        with lock.open("r+") as competing, pytest.raises(BlockingIOError):
            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert _identity(lock) == original
    assert lifecycle._verify_backup(root, identifier)["complete"] is True


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize(
    "unsafe",
    [
        "unknown_scope",
        "invalid_name",
        "uppercase_uuid",
        "root_symlink",
        "scope_symlink",
        "symlink",
        "hardlink",
        "fifo",
        "directory",
        "root_mode",
        "scope_mode",
        "file_mode",
        "content",
        "root_uid",
        "scope_gid",
        "file_uid",
        "file_gid",
    ],
)
def test_backup_rejects_unsafe_application_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, unsafe: str
) -> None:
    root = tmp_path / "state"
    _historical(root)
    lock = _lock(root, scope)
    locks = root / "locks"
    parent = lock.parent
    if unsafe == "unknown_scope":
        parent.rename(locks / "unknown")
    elif unsafe in {"invalid_name", "uppercase_uuid"}:
        name = (
            "unexpected.lock"
            if unsafe == "invalid_name"
            else "ABCDEFAB-1234-1234-1234-123456789ABC.lock"
        )
        lock.rename(parent / name)
    elif unsafe in {"root_symlink", "scope_symlink", "symlink"}:
        path = {"root_symlink": locks, "scope_symlink": parent, "symlink": lock}[unsafe]
        destination = tmp_path / "outside"
        path.rename(destination)
        path.symlink_to(destination, target_is_directory=destination.is_dir())
    elif unsafe == "hardlink":
        os.link(lock, tmp_path / "outside")
    elif unsafe in {"fifo", "directory"}:
        lock.unlink()
        if unsafe == "fifo":
            os.mkfifo(lock, mode=0o600)
        else:
            lock.mkdir(mode=0o700)
    elif unsafe in {"root_mode", "scope_mode", "file_mode"}:
        {"root_mode": locks, "scope_mode": parent, "file_mode": lock}[unsafe].chmod(
            0o755
        )
    elif unsafe == "content":
        lock.write_bytes(b"unexpected")
    else:
        # Real filesystem metadata, checked against a different expected owner.
        # No privileged chown or mocked stat/type/link/mode results are needed.
        original = lifecycle._private
        path = {
            "root_uid": locks,
            "scope_gid": parent,
            "file_uid": lock,
            "file_gid": lock,
        }[unsafe]

        def private(candidate: Path, *, directory: bool) -> None:
            with monkeypatch.context() as context:
                if candidate == path:
                    attribute = "getgid" if unsafe.endswith("gid") else "getuid"
                    context.setattr(os, attribute, lambda: 123456)
                original(candidate, directory=directory)

        monkeypatch.setattr(lifecycle, "_private", private)
    with pytest.raises(HostBootstrapError):
        lifecycle.backup(root, _url(root))
    assert not list((root / "backups").glob("*/manifest.json"))


@pytest.mark.parametrize("recorded", [False, True])
def test_backup_verifier_rejects_archived_application_locks(
    tmp_path: Path, recorded: bool
) -> None:
    root = tmp_path / "state"
    _historical(root)
    identifier = lifecycle.backup(root, _url(root))
    archive = root / "backups" / identifier
    lock = _lock(archive, "managed-agent-lifecycle")
    if recorded:
        manifest_path = archive / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][lock.relative_to(archive).as_posix()] = {
            "sha256": lifecycle._digest(lock),
            "size": 0,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "mode": 0o600,
        }
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(HostBootstrapError, match="inventory"):
        lifecycle._verify_backup(root, identifier)

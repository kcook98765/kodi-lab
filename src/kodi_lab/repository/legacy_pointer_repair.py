"""Repair the one root-owned development pointer left by the reviewed A transition.

This is a networkless, Docker-administrator compatibility operation. It does
not serve requests, repair arbitrary paths, or relax the application backup gate.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from kodi_lab import remote_lifecycle
from kodi_lab.repository.development import load_current as load_development_current
from kodi_lab.repository.development import (
    load_generation as load_development_generation,
)
from kodi_lab.repository.stable import canonical, load_current, sha256
from kodi_lab.repository.transition import (
    _bundle,
    _identity,
    _verify_development_metadata,
    _verify_migration_relationship,
    _write,
)

# The retained, reviewed A candidate whose transition created the bad inode.
# A different installed candidate requires a separate review, not a broad repair.
LEGACY_BUNDLE_SHA256 = (
    "e68e66d271e8a36873f830526c53c1f253ef48afdda260e589a5e5eaaff41709"
)
WEB_OWNER = (10001, 10001)
ROOT_OWNER = (0, 0)
REPAIR_FILE = "DEVELOPMENT_CURRENT_OWNER_REPAIR.json"
PATH_IDENTITY = "development-data/CURRENT"


def _metadata(
    path: Path, *, directory: bool, owner: tuple[int, int], mode: int
) -> os.stat_result:
    item = path.lstat()
    if (
        (
            not stat.S_ISDIR(item.st_mode)
            if directory
            else not stat.S_ISREG(item.st_mode)
        )
        or (item.st_uid, item.st_gid) != owner
        or stat.S_IMODE(item.st_mode) != mode
        or (not directory and item.st_nlink != 1)
    ):
        raise ValueError(f"legacy pointer repair metadata is unsafe: {path.name}")
    return item


def _record(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    _metadata(path, directory=False, owner=ROOT_OWNER, mode=0o600)
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical(value) != raw:
        raise ValueError("legacy pointer repair record is not canonical")
    return value


@contextmanager
def _publication_lock(root: Path) -> Iterator[None]:
    # An existing Web-owned lock is required, so locking cannot create state
    # before the operation has validated the installed repository layout.
    remote_lifecycle._rollback_entry(root / ".publication.lock", directory=False)
    fd = os.open(root / ".publication.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _audit_except_current(root: Path) -> None:
    remote_lifecycle._rollback_entry(root, directory=True)
    if {item.name for item in root.iterdir()} != {
        ".publication.lock",
        "CURRENT",
        "ACK",
        "generations",
        "packages",
        "pending",
    }:
        raise ValueError("development repository root has unexpected entries")
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            remote_lifecycle._rollback_entry(Path(parent) / name, directory=True)
        for name in files:
            if Path(parent) == root and name == "CURRENT":
                continue
            remote_lifecycle._rollback_entry(Path(parent) / name, directory=False)


def _retained_generations(root: Path, base_url: str) -> None:
    expected = {
        "PROVENANCE.json",
        "development",
        "development/addons.xml",
        "development/addons.xml.sha256",
        "install",
        "install/repository.kodi-lab-dev-1.0.0.zip",
    }
    generations = root / "generations"
    for selected in generations.iterdir():
        if selected.is_symlink() or not selected.is_dir():
            raise ValueError("retained development generation path is unsafe")
        actual = {str(item.relative_to(selected)) for item in selected.rglob("*")}
        if actual != expected:
            raise ValueError("retained development generation has unexpected entries")
        load_development_generation(root, selected.name, expected_base_url=base_url)


def _prior_transition(
    repository_root: Path,
    state_root: Path,
    installed_hash: str,
    generation: str,
    base_url: str,
) -> str:
    if (repository_root / "TRANSITION.json").exists() or (
        repository_root / "TRANSITION.json"
    ).is_symlink():
        raise ValueError("repository candidate transition is pending")
    last = _record(repository_root / "LAST_TRANSITION.json")
    if last is None or set(last) != {"operation_id", "receipt_sha256"}:
        raise ValueError("reviewed prior transition is missing")
    operation_id = last["operation_id"]
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", operation_id
    ):
        raise ValueError("prior transition operation is invalid")
    receipts = repository_root / "transitions"
    _metadata(receipts, directory=True, owner=ROOT_OWNER, mode=0o700)
    prior = _record(receipts / f"{operation_id}.json")
    if (
        prior is None
        or last["receipt_sha256"] != sha256(canonical(prior))
        or prior.get("operation_id") != operation_id
        or prior.get("new_bundle_sha256") != installed_hash
        or prior.get("new_development_generation") != generation
        or prior.get("base_url") != base_url
        or prior.get("rollback") is not False
        or not isinstance(prior.get("completed_at"), str)
    ):
        raise ValueError("prior transition does not explain the active pointer")
    with _identity(*WEB_OWNER):
        application = remote_lifecycle._read_json(
            state_root / ".repository-candidate-transition.json"
        )
    if (
        application.get("operation_id") != operation_id
        or application.get("new_bundle_sha256") != installed_hash
    ):
        raise ValueError("application transition evidence differs")
    return operation_id


def _pointer_descriptor(repository_root: Path, expected: bytes) -> tuple[int, int]:
    """Open only the fixed child inode, without following a symlink at any level."""
    root_fd = os.open(repository_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        dev_fd = os.open(
            "development-data",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            pointer_fd = os.open("CURRENT", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dev_fd)
            try:
                item = os.fstat(pointer_fd)
                named = os.stat("CURRENT", dir_fd=dev_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(item.st_mode)
                    or item.st_nlink != 1
                    or (item.st_dev, item.st_ino) != (named.st_dev, named.st_ino)
                    or stat.S_IMODE(item.st_mode) != 0o644
                    or (item.st_uid, item.st_gid) not in (ROOT_OWNER, WEB_OWNER)
                    or os.read(pointer_fd, len(expected) + 1) != expected
                ):
                    raise ValueError(
                        "development CURRENT differs from verified generation"
                    )
                return pointer_fd, dev_fd
            except BaseException:
                os.close(pointer_fd)
                raise
        except BaseException:
            os.close(dev_fd)
            raise
    finally:
        # The directory descriptor keeps its parent mounted; only the selected
        # file and immediate development directory remain needed for mutation.
        os.close(root_fd)


def repair_legacy_development_current(
    installed_bundle: Path,
    admin_bundle: Path,
    repository_root: Path,
    state_root: Path,
    base_url: str,
    expected_installed_bundle: str,
    expected_admin_bundle: str,
) -> dict[str, object]:
    """Verify A and its prior transition, then change only CURRENT's UID/GID."""
    if os.geteuid() != 0 or os.getegid() != 0:
        raise ValueError("legacy pointer repair requires the packaged root job")
    if expected_installed_bundle != LEGACY_BUNDLE_SHA256:
        raise ValueError("bundle is not the reviewed legacy candidate")
    _metadata(repository_root, directory=True, owner=ROOT_OWNER, mode=0o755)
    _metadata(state_root, directory=True, owner=WEB_OWNER, mode=0o700)
    installed = _bundle(installed_bundle, expected_installed_bundle)
    admin = _bundle(admin_bundle, expected_admin_bundle)
    if installed == admin:
        raise ValueError("legacy repair requires a distinct reviewed admin candidate")
    database = state_root / remote_lifecycle.DATABASE
    database_url = f"sqlite:///{database}"
    _verify_migration_relationship(
        database_url, admin["migration_revision"], installed["migration_revision"]
    )
    dev_root = repository_root / "development-data"
    _metadata(dev_root, directory=True, owner=WEB_OWNER, mode=0o755)
    with _identity(*WEB_OWNER), remote_lifecycle.lifecycle_lock(state_root):  # noqa: SIM117
        with _publication_lock(dev_root):
            revision = remote_lifecycle.validate(
                state_root, database_url, require_marker=True
            )
            if revision != installed["migration_revision"]:
                raise ValueError("live database revision differs from installed A")
            _audit_except_current(dev_root)
            _retained_generations(dev_root, base_url)
            remote_lifecycle._verify_repository_pair(database, dev_root, base_url)
            stable = load_current(
                repository_root,
                expected_base_url=base_url,
                expected_bundle_sha256=expected_installed_bundle,
            )[1]
            development = load_development_current(
                dev_root, expected_base_url=base_url
            )[1]
            if any(
                stable.get(field) != installed[field]
                or development.get(field) != installed[field]
                for field in ("bundle_sha256", "source_commit", "source_tree")
            ):
                raise ValueError("repository belongs to another candidate")
            generation = str(development["tree_sha256"])
            with _identity(ROOT_OWNER[0], ROOT_OWNER[1]):
                prior_operation = _prior_transition(
                    repository_root,
                    state_root,
                    expected_installed_bundle,
                    generation,
                    base_url,
                )
                expected = (generation + "\n").encode()
                pointer_fd, dev_fd = _pointer_descriptor(repository_root, expected)
                try:
                    item = os.fstat(pointer_fd)
                    owner = (item.st_uid, item.st_gid)
                    pointer_hash = sha256(expected)
                    record_path = repository_root / REPAIR_FILE
                    record = _record(record_path)
                    common = {
                        "kind": "legacy-development-current-owner-repair-v1",
                        "installed_bundle_sha256": expected_installed_bundle,
                        "admin_bundle_sha256": expected_admin_bundle,
                        "base_url": base_url,
                        "repository_id": "repository.kodi-lab-dev",
                        "path": PATH_IDENTITY,
                        "prior_owner": [0, 0],
                        "result_owner": [*WEB_OWNER],
                        "mode": 0o644,
                        "active_generation": generation,
                        "pointer_sha256": pointer_hash,
                        "prior_transition_operation_id": prior_operation,
                        "requested_by": "packaged-docker-administrator",
                    }
                    if record is not None:
                        keys = set(common) | {"operation_id", "started_at", "status"}
                        if record.get("status") == "completed":
                            keys.add("completed_at")
                        if (
                            set(record) != keys
                            or not isinstance(record.get("operation_id"), str)
                            or not re.fullmatch(
                                r"[0-9a-f]{32}", str(record["operation_id"])
                            )
                            or not isinstance(record.get("started_at"), str)
                        ):
                            raise ValueError("legacy pointer repair record is invalid")
                        if any(
                            record.get(key) != value for key, value in common.items()
                        ):
                            raise ValueError("legacy pointer repair record differs")
                        if record.get("status") == "completed":
                            if owner != WEB_OWNER or not isinstance(
                                record.get("completed_at"), str
                            ):
                                raise ValueError(
                                    "completed repair differs from pointer"
                                )
                            with _identity(*WEB_OWNER):
                                _verify_development_metadata(dev_root)
                            return record
                        if record.get("status") != "pending" or owner not in (
                            ROOT_OWNER,
                            WEB_OWNER,
                        ):
                            raise ValueError("legacy pointer repair is ambiguous")
                    elif owner == WEB_OWNER:
                        with _identity(*WEB_OWNER):
                            _verify_development_metadata(dev_root)
                        return {**common, "status": "already_canonical"}
                    else:
                        if owner != ROOT_OWNER:
                            raise ValueError(
                                "development CURRENT owner is not the legacy owner"
                            )
                        record = {
                            **common,
                            "operation_id": uuid4().hex,
                            "started_at": datetime.now(UTC).isoformat(
                                timespec="seconds"
                            ),
                            "status": "pending",
                        }
                        _write(record_path, record)
                    if owner == ROOT_OWNER:
                        os.fchown(pointer_fd, *WEB_OWNER)
                        os.fsync(pointer_fd)
                        os.fsync(dev_fd)
                    named = os.stat("CURRENT", dir_fd=dev_fd, follow_symlinks=False)
                    final = os.fstat(pointer_fd)
                    if (
                        (named.st_dev, named.st_ino) != (final.st_dev, final.st_ino)
                        or (final.st_uid, final.st_gid) != WEB_OWNER
                        or stat.S_IMODE(final.st_mode) != 0o644
                        or os.lseek(pointer_fd, 0, os.SEEK_SET) != 0
                        or os.read(pointer_fd, len(expected) + 1) != expected
                    ):
                        raise ValueError("development CURRENT changed during repair")
                    with _identity(*WEB_OWNER):
                        _verify_development_metadata(dev_root)
                        remote_lifecycle._verify_repository_pair(
                            database, dev_root, base_url
                        )
                    if record is None:
                        raise ValueError("legacy pointer repair intent is missing")
                    completed = {
                        **record,
                        "status": "completed",
                        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                    _write(record_path, completed)
                    return completed
                finally:
                    os.close(pointer_fd)
                    os.close(dev_fd)

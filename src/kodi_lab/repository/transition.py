"""Explicit, recoverable transition between two reviewed bundle identities."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from alembic.script import ScriptDirectory

from kodi_lab import remote_lifecycle
from kodi_lab.remote_lifecycle import (
    DATABASE,
    _verify_backup,
    _verify_repository_pair,
    _write_json,
    lifecycle_lock,
    validate,
)
from kodi_lab.repository.development import (
    _candidate_identity,
    _generation,
    _pointer,
)
from kodi_lab.repository.development import (
    load_current as load_development_current,
)
from kodi_lab.repository.development import (
    load_generation as load_development_generation,
)
from kodi_lab.repository.stable import (
    CURRENT_BRIDGE,
    HEX_PATTERN,
    ReviewedBridge,
    _read_reviewed_bridge,
    _switch_pointer,
    canonical,
    generate,
    load_current,
    load_generation,
    reviewed_bridge_from_bundle,
    sha256,
)
from kodi_lab.storage import packaged_migrations

PENDING = "TRANSITION.json"
RECEIPT = ".repository-candidate-transition.json"


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: dict[str, object]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_development_metadata(root: Path) -> None:
    """Apply the unchanged application backup's ownership gate to live data."""
    remote_lifecycle._rollback_entry(root, directory=True)
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            remote_lifecycle._rollback_entry(Path(current) / name, directory=True)
        for name in files:
            remote_lifecycle._rollback_entry(Path(current) / name, directory=False)


@contextmanager
def _identity(uid: int, gid: int) -> Iterator[None]:
    previous_uids, previous_gids = os.getresuid(), os.getresgid()
    changed = (uid, gid) != (previous_uids[0], previous_gids[0])
    if changed:
        if 0 not in previous_uids:
            raise ValueError("candidate transition requires the state owner")
        os.setresgid(gid, gid, previous_gids[2])
        os.setresuid(uid, uid, previous_uids[2])
    try:
        yield
    finally:
        if changed:
            os.setresgid(*previous_gids)
            os.setresuid(*previous_uids)


def _state(database: Path) -> tuple[str, str]:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT current_generation,current_index_sha256,pending_generation "
            "FROM development_repository_state WHERE id=1"
        ).fetchone()
    if row is None or row[2] is not None:
        raise ValueError("development repository has an unfinished operation")
    return str(row[0]), str(row[1])


def _update_state(database: Path, old_tree: str, new_tree: str, index: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT current_generation,pending_generation FROM "
            "development_repository_state WHERE id=1"
        ).fetchone()
        if row == (new_tree, None):
            return
        if row != (old_tree, None):
            raise ValueError("development repository state changed during transition")
        connection.execute(
            "UPDATE development_repository_state SET current_generation=?, "
            "current_index_sha256=?, updated_at=? WHERE id=1",
            (new_tree, index, datetime.now(UTC).isoformat(timespec="seconds")),
        )


def _bundle(
    bundle: Path, expected_hash: str, *, require_current: bool = False
) -> dict[str, str]:
    if not HEX_PATTERN.fullmatch(expected_hash) or bundle.is_symlink():
        raise ValueError("candidate bundle identity is invalid")
    review = reviewed_bridge_from_bundle(bundle, expected_hash)
    if require_current and review != CURRENT_BRIDGE:
        raise ValueError("replacement candidate reviewed inputs differ")
    bridge, manifest, _ = _read_reviewed_bridge(bundle, review)
    identity = _candidate_identity(bundle)
    if (
        identity["bundle_sha256"] != expected_hash
        or sha256(bridge) != review.sha256
        or identity["source_commit"] != manifest["source_commit"]
        or identity["source_tree"] != manifest["source_tree"]
    ):
        raise ValueError("candidate reviewed inputs differ")
    repository = manifest.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("candidate migration identity is missing")
    revision = repository.get("migration_head")
    if revision is None:
        # The retained pre-0057 candidate records its last migration under
        # the repository capability name. New bundles record the head directly.
        if "development_capability_amendment_migration" in repository:
            revision = repository["development_capability_amendment_migration"]
        else:
            revision = repository.get("workspace_deployment_migration")
    if not isinstance(revision, str) or not re.fullmatch(
        r"[0-9]{4}_[a-z][a-z0-9_]*", revision
    ):
        raise ValueError("candidate migration identity is invalid")
    return {
        **identity,
        "migration_revision": revision,
        "bridge_version": review.version,
        "bridge_sha256": review.sha256,
        "bridge_fingerprint": review.fingerprint,
    }


def _review(identity: dict[str, str]) -> ReviewedBridge:
    return ReviewedBridge(
        identity["bridge_version"],
        identity["bridge_sha256"],
        identity["bridge_fingerprint"],
    )


def _verify_migration_relationship(
    database_url: str, installed_head: str, ancestor: str
) -> None:
    """Require one unbranched reviewed ancestry path in the installed wheel."""
    with packaged_migrations.packaged_config(database_url) as config:
        scripts = ScriptDirectory.from_config(config)
        if scripts.get_current_head() != installed_head:
            raise ValueError("candidate migration head differs from installed wheel")
        current = installed_head
        while current != ancestor:
            script = scripts.get_revision(current)
            if script is None or not isinstance(script.down_revision, str):
                raise ValueError("candidate migrations are unrelated or branched")
            current = script.down_revision


def _development_records(
    root: Path, provenance: dict[str, object]
) -> list[dict[str, str]]:
    records = cast(list[dict[str, str]], provenance["packages"])
    return [
        {
            **record,
            "path": str(
                root
                / "packages"
                / record["id"]
                / f"{record['id']}-{record['version']}.zip"
            ),
        }
        for record in records
    ]


def transition(
    previous_bundle: Path,
    replacement_bundle: Path,
    repository_root: Path,
    state_root: Path,
    base_url: str,
    expected_old_bundle: str,
    expected_new_bundle: str,
    backup_id: str,
    *,
    rollback: bool = False,
    interrupt_after: str | None = None,
) -> dict[str, object]:
    """Transition a stopped installation; rerunning the exact intent recovers it."""
    if repository_root.is_symlink() or state_root.is_symlink():
        raise ValueError("repository or application state root is unsafe")
    old = _bundle(previous_bundle, expected_old_bundle, require_current=rollback)
    new = _bundle(replacement_bundle, expected_new_bundle, require_current=not rollback)
    old_review, new_review = _review(old), _review(new)
    if old == new:
        raise ValueError("candidate transition requires two distinct identities")
    dev_root = repository_root / "development-data"
    database = state_root / DATABASE
    old_revision = old["migration_revision"]
    new_revision = new["migration_revision"]
    _verify_migration_relationship(
        f"sqlite:///{database}",
        old_revision if rollback else new_revision,
        new_revision if rollback else old_revision,
    )
    owner = state_root.stat()
    process_identity = (os.geteuid(), os.getegid())
    with _identity(owner.st_uid, owner.st_gid), lifecycle_lock(state_root):
        revision = validate(
            state_root,
            f"sqlite:///{database}",
            require_marker=True,
        )
        backup = _verify_backup(state_root, backup_id)
        if backup["revision"] != (new_revision if rollback else old_revision):
            raise ValueError("backup revision differs from selected old candidate")
        if revision not in {old_revision, new_revision}:
            raise ValueError("live database revision differs from candidate transition")
        if rollback:
            forward = remote_lifecycle._read_json(state_root / RECEIPT)
            if any(
                forward.get(field) != value
                for field, value in (
                    ("backup_id", backup_id),
                    ("old_bundle_sha256", expected_new_bundle),
                    ("new_bundle_sha256", expected_old_bundle),
                    ("old_migration_revision", new_revision),
                    ("new_migration_revision", old_revision),
                )
            ):
                raise ValueError("rollback lacks the matching forward transition")
        backup_repository = backup.get("repository_identity")
        if (
            not isinstance(backup_repository, dict)
            or backup_repository.get("base_url") != base_url
        ):
            raise ValueError("backup repository origin differs")
        _verify_development_metadata(dev_root)
        with _identity(*process_identity):
            pending_path = repository_root / PENDING
            complete_dir = repository_root / "transitions"
            last_path = repository_root / "LAST_TRANSITION.json"
            if (
                pending_path.is_symlink()
                or complete_dir.is_symlink()
                or last_path.is_symlink()
            ):
                raise ValueError("repository transition path is unsafe")
            key = {
                "old_bundle_sha256": expected_old_bundle,
                "new_bundle_sha256": expected_new_bundle,
                "old_migration_revision": old_revision,
                "new_migration_revision": new_revision,
                "base_url": base_url,
                "backup_id": backup_id,
                "rollback": rollback,
            }
            if last_path.exists() and not pending_path.exists():
                last = json.loads(last_path.read_bytes())
                if not re.fullmatch(r"[0-9a-f]{32}", str(last.get("operation_id"))):
                    raise ValueError("last transition identity is invalid")
                previous_receipt = complete_dir / f"{last['operation_id']}.json"
                if previous_receipt.is_symlink():
                    raise ValueError("transition receipt is unsafe")
                receipt = json.loads(previous_receipt.read_bytes())
                if sha256(canonical(receipt)) != last.get("receipt_sha256"):
                    raise ValueError("last transition receipt differs")
                if {field: receipt.get(field) for field in key} == key:
                    stable_now = load_current(
                        repository_root,
                        expected_base_url=base_url,
                        reviewed_bridge=new_review,
                    )[1]
                    development_now = load_development_current(
                        dev_root, expected_base_url=base_url
                    )[1]
                    if (
                        stable_now["bundle_sha256"] == expected_new_bundle
                        and development_now["bundle_sha256"] == expected_new_bundle
                    ):
                        with _identity(owner.st_uid, owner.st_gid):
                            _verify_development_metadata(dev_root)
                        return cast(dict[str, object], receipt)
            if not rollback and revision != old_revision:
                raise ValueError("live database revision differs from old candidate")
            if pending_path.exists():
                if pending_path.is_symlink():
                    raise ValueError("transition intent is unsafe")
                intent = json.loads(pending_path.read_bytes())
                if {field: intent.get(field) for field in key} != key:
                    raise ValueError("another candidate transition is pending")
                operation_id = str(intent["operation_id"])
                if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                    raise ValueError("transition operation identity is invalid")
            else:
                operation_id = uuid4().hex
                old_stable = load_current(
                    repository_root,
                    expected_base_url=base_url,
                    expected_bundle_sha256=expected_old_bundle,
                    reviewed_bridge=old_review,
                )[1]
                old_dev = load_development_current(
                    dev_root, expected_base_url=base_url
                )[1]
                if any(
                    old_stable.get(field) != old[field]
                    or old_dev.get(field) != old[field]
                    for field in ("bundle_sha256", "source_commit", "source_tree")
                ):
                    raise ValueError("repository belongs to another candidate")
                if (
                    not rollback
                    and backup_repository.get("stable_tree_sha256")
                    != old_stable["tree_sha256"]
                ):
                    raise ValueError("backup is not bound to current stable repository")
                _verify_repository_pair(database, dev_root, base_url)
                new_stable = generate(
                    replacement_bundle,
                    repository_root,
                    base_url,
                    expected_previous_bundle_sha256=expected_old_bundle,
                    activate=False,
                    reviewed_bridge=new_review,
                    previous_bridge=old_review,
                )
                with _identity(owner.st_uid, owner.st_gid):
                    new_dev = _generation(
                        dev_root,
                        base_url,
                        _development_records(dev_root, old_dev),
                        candidate_identity={
                            field: new[field]
                            for field in (
                                "bundle_sha256",
                                "source_commit",
                                "source_tree",
                            )
                        },
                    )
                    _verify_development_metadata(dev_root)
                if (
                    rollback
                    and backup_repository.get("stable_tree_sha256")
                    != new_stable["tree_sha256"]
                ):
                    raise ValueError(
                        "rollback backup is not bound to selected candidate"
                    )
                complete_dir.mkdir(mode=0o700, exist_ok=True)
                intent = {
                    **key,
                    "operation_id": operation_id,
                    "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "old_stable_generation": old_stable["tree_sha256"],
                    "new_stable_generation": new_stable["tree_sha256"],
                    "old_development_generation": old_dev["tree_sha256"],
                    "new_development_generation": new_dev["tree_sha256"],
                    "development_index_sha256": new_dev["addons_xml_sha256"],
                    "bridge_zip_sha256": new_review.sha256,
                }
                _write(pending_path, intent)
            complete_path = complete_dir / f"{operation_id}.json"
            old_stable_tree = str(intent["old_stable_generation"])
            new_stable_tree = str(intent["new_stable_generation"])
            old_dev_tree = str(intent["old_development_generation"])
            new_dev_tree = str(intent["new_development_generation"])
            previous_stable = load_generation(
                repository_root,
                old_stable_tree,
                expected_base_url=base_url,
                expected_bundle_sha256=expected_old_bundle,
                reviewed_bridge=old_review,
            )[1]
            next_stable = load_generation(
                repository_root,
                new_stable_tree,
                expected_base_url=base_url,
                expected_bundle_sha256=expected_new_bundle,
                reviewed_bridge=new_review,
            )[1]
            previous_dev = load_development_generation(
                dev_root, old_dev_tree, expected_base_url=base_url
            )[1]
            next_dev = load_development_generation(
                dev_root, new_dev_tree, expected_base_url=base_url
            )[1]
            if (
                any(
                    previous_stable.get(field) != old[field]
                    or previous_dev.get(field) != old[field]
                    or next_stable.get(field) != new[field]
                    or next_dev.get(field) != new[field]
                    for field in ("bundle_sha256", "source_commit", "source_tree")
                )
                or previous_dev["packages"] != next_dev["packages"]
                or next_dev["addons_xml_sha256"] != intent["development_index_sha256"]
                or intent["bridge_zip_sha256"] != new_review.sha256
            ):
                raise ValueError("prepared transition generation differs")
            current_stable = (repository_root / "CURRENT").read_text().strip()
            current_dev = (dev_root / "CURRENT").read_text().strip()
            if current_stable not in {
                old_stable_tree,
                new_stable_tree,
            } or current_dev not in {old_dev_tree, new_dev_tree}:
                raise ValueError("repository state changed during transition")
            with _identity(owner.st_uid, owner.st_gid):
                db_tree, db_index = _state(database)
                if (
                    db_tree not in {old_dev_tree, new_dev_tree}
                    or db_index != intent["development_index_sha256"]
                ):
                    raise ValueError("development database changed during transition")
            _switch_pointer(repository_root, new_stable_tree)
            if interrupt_after == "stable":
                raise RuntimeError("injected interruption after stable switch")
            with _identity(owner.st_uid, owner.st_gid):
                (dev_root / "ACK").unlink(missing_ok=True)
                _pointer(dev_root / "CURRENT", new_dev_tree)
                _verify_development_metadata(dev_root)
            if interrupt_after == "development":
                raise RuntimeError("injected interruption after development switch")
            with _identity(owner.st_uid, owner.st_gid):
                _update_state(
                    database,
                    old_dev_tree,
                    new_dev_tree,
                    str(intent["development_index_sha256"]),
                )
                if interrupt_after == "database":
                    raise RuntimeError("injected interruption after database update")
                _pointer(dev_root / "ACK", new_dev_tree)
                _verify_repository_pair(database, dev_root, base_url)
                _verify_development_metadata(dev_root)
                if not rollback:
                    _write_json(
                        state_root / RECEIPT,
                        {
                            "backup_id": backup_id,
                            "operation_id": operation_id,
                            "old_bundle_sha256": expected_old_bundle,
                            "new_bundle_sha256": expected_new_bundle,
                            "old_migration_revision": old_revision,
                            "new_migration_revision": new_revision,
                        },
                    )
            load_current(
                repository_root,
                expected_base_url=base_url,
                expected_bundle_sha256=expected_new_bundle,
                reviewed_bridge=new_review,
            )
            load_development_current(dev_root, expected_base_url=base_url)
            if complete_path.exists():
                if complete_path.is_symlink():
                    raise ValueError("transition receipt is unsafe")
                receipt = json.loads(complete_path.read_bytes())
                if any(receipt.get(field) != value for field, value in intent.items()):
                    raise ValueError("transition receipt differs from approved intent")
            else:
                receipt = {
                    **intent,
                    "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                _write(complete_path, receipt)
            _write(
                last_path,
                {
                    "operation_id": operation_id,
                    "receipt_sha256": sha256(canonical(receipt)),
                },
            )
            pending_path.unlink()
            _fsync(repository_root)
            return cast(dict[str, object], receipt)

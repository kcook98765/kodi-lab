"""Stopped-installation origin migration with immutable feeds and replayable intent."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from kodi_lab import remote_lifecycle
from kodi_lab.repository import development, stable
from kodi_lab.repository.transition import (
    _development_records,
    _identity,
    _verify_development_metadata,
    _write,
)

PENDING = "ORIGIN-MIGRATION.json"


def _read(path: Path) -> bytes:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    )
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
            or metadata.st_mode & 0o022
        ):
            raise ValueError("origin migration configuration is unsafe")
        result = stream.read(1024 * 1024 + 1)
        following = os.fstat(stream.fileno())
        if len(result) != metadata.st_size or (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ) != (following.st_size, following.st_mtime_ns, following.st_ctime_ns):
            raise ValueError("origin migration configuration changed during read")
        return result


def _replace(path: Path, content: bytes, *, expected_sha256: str) -> None:
    metadata = path.stat()
    descriptor, name = tempfile.mkstemp(prefix=".origin-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if stable.sha256(_read(path)) != expected_sha256:
            raise ValueError("site configuration changed before replacement")
        os.replace(temporary, path)
        stable_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(stable_fd)
        finally:
            os.close(stable_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _environment(raw: bytes, old: str, new: str, bind: str | None) -> bytes:
    lines = raw.decode("utf-8").splitlines(keepends=True)
    key = "KODI_LAB_REPOSITORY_BASE_URL="
    if sum(line.startswith(key) for line in lines) != 1:
        raise ValueError("site environment needs one explicit repository origin")
    if next(line[len(key) :].strip() for line in lines if line.startswith(key)) != old:
        raise ValueError("site environment differs from the approved old origin")
    result = [key + new + "\n" if line.startswith(key) else line for line in lines]
    if bind is not None:
        key = "KODI_LAB_REPOSITORY_BIND="
        if sum(line.startswith(key) for line in lines) != 1:
            raise ValueError("site environment needs one explicit repository bind")
        result = [
            key + bind + "\n" if line.startswith(key) else line for line in result
        ]
    return "".join(result).encode()


def _configuration(
    path: Path,
    old: str,
    new: str,
    bind: str | None,
    operation_id: str,
    *,
    receipt: bool,
) -> tuple[bytes, bytes]:
    before = _read(path)
    if receipt:
        value = json.loads(before)
        if (
            not isinstance(value, dict)
            or value.get("repository_base_url") != old
            or not isinstance(value.get("bundle_sha256"), str)
        ):
            raise ValueError("installation receipt differs from the approved origin")
        value["repository_base_url"] = new
        if bind is not None:
            value["repository_bind"] = bind
        value["repository_origin_migration"] = operation_id
        after = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    else:
        after = _environment(before, old, new, bind)
    return before, after


def migrate_origin(
    bundle: Path,
    repository_root: Path,
    state_root: Path,
    *,
    old_origin: str,
    new_origin: str,
    version: str,
    operation_id: UUID,
    backup_id: str,
    site_environment: Path,
    installation_receipt: Path | None = None,
    repository_bind: str | None = None,
    interrupt_after: str | None = None,
) -> dict[str, Any]:
    """Run only with Web, broker and repository stopped; repeat exact args to recover.

    A reverse move is another migration with a newer repository version. Old feed
    generations and all client/deployment evidence remain immutable.
    """
    stable.validate_config(repository_bind or "192.168.0.1", 8110, old_origin)
    stable.validate_config(repository_bind or "192.168.0.1", 8110, new_origin)
    stable.repository_version(version)
    if old_origin == new_origin:
        raise ValueError("origin migration requires different origins")
    for directory_path in (
        bundle,
        repository_root,
        state_root,
        site_environment.parent,
        *((installation_receipt.parent,) if installation_receipt else ()),
    ):
        if (
            not directory_path.is_absolute()
            or directory_path.is_symlink()
            or not directory_path.is_dir()
        ):
            raise ValueError("origin migration paths must be explicit safe directories")
    if (repository_root / "TRANSITION.json").exists() or (
        repository_root / "TRANSITION.json"
    ).is_symlink():
        raise ValueError("candidate transition must finish before origin migration")
    owner = state_root.stat()
    database = state_root / remote_lifecycle.DATABASE
    development_root = repository_root / "development-data"
    journal = repository_root / PENDING
    completed = repository_root / "origin-migrations" / f"{operation_id}.json"
    bundle_identity = development._candidate_identity(bundle)
    stable._read_reviewed_bridge(bundle)
    key = {
        "schema": "kodi-lab-repository-origin-migration-v1",
        "operation_id": str(operation_id),
        "old_origin": old_origin,
        "new_origin": new_origin,
        "repository_version": version,
        "bundle_sha256": bundle_identity["bundle_sha256"],
        "backup_id": backup_id,
        "site_environment": str(site_environment),
        "installation_receipt": (
            None if installation_receipt is None else str(installation_receipt)
        ),
        "repository_bind": repository_bind,
    }
    # The lifecycle lock spans validation, preparation, both publications and
    # configuration replacement. The operator keeps all services stopped.
    with remote_lifecycle.lifecycle_lock(state_root):
        with _identity(owner.st_uid, owner.st_gid):
            remote_lifecycle.validate(
                state_root, f"sqlite:///{database}", require_head=True
            )
            backup = remote_lifecycle._verify_backup(state_root, backup_id)
            backup_identity = backup.get("repository_identity")
            if (
                not isinstance(backup_identity, dict)
                or backup_identity.get("base_url") != old_origin
            ):
                raise ValueError(
                    "origin migration needs a verified backup of the old origin"
                )
        if completed.exists() and not journal.exists():
            receipt = json.loads(_read(completed))
            if any(receipt.get(field) != value for field, value in key.items()):
                raise ValueError(
                    "origin operation identifier belongs to another intent"
                )
            _verify_complete(
                repository_root,
                database,
                site_environment,
                installation_receipt,
                receipt,
            )
            return dict(receipt)
        if journal.exists() or journal.is_symlink():
            intent = json.loads(_read(journal))
            if any(intent.get(field) != value for field, value in key.items()):
                raise ValueError("a different origin migration is unfinished")
        else:
            _, previous_stable = stable.load_current(
                repository_root,
                expected_base_url=old_origin,
                expected_bundle_sha256=bundle_identity["bundle_sha256"],
            )
            with (
                _identity(owner.st_uid, owner.st_gid),
                development.publication_lock(development_root),
            ):
                _, previous_development = development.load_current(
                    development_root, expected_base_url=old_origin
                )
                remote_lifecycle._verify_repository_pair(
                    database, development_root, old_origin
                )
                _verify_development_metadata(development_root)
            if any(
                previous_development.get(field) != value
                for field, value in bundle_identity.items()
            ):
                raise ValueError(
                    "development candidate differs from the selected bundle"
                )
            if any(
                stable.repository_version(version)
                <= stable.repository_version(str(feed["repository_version"]))
                for feed in (previous_stable, previous_development)
            ):
                raise ValueError("both repository add-ons need a newer version")
            if (
                backup_identity.get("stable_tree_sha256")
                != previous_stable["tree_sha256"]
            ):
                raise ValueError("backup no longer names the current stable generation")
            with _identity(owner.st_uid, owner.st_gid):
                backed_up_development = development.load_current(
                    state_root
                    / "backups"
                    / backup_id
                    / remote_lifecycle.REPOSITORY_BACKUP_ROOT
                    / "development-data",
                    expected_base_url=old_origin,
                )[1]
            if backed_up_development != previous_development:
                raise ValueError(
                    "backup no longer names the current development generation"
                )
            _verify_database(
                database, development_root, previous_development, previous_development
            )
            configurations = []
            for path, is_receipt in (
                (site_environment, False),
                (installation_receipt, True),
            ):
                if path is None:
                    continue
                before, after = _configuration(
                    path,
                    old_origin,
                    new_origin,
                    repository_bind,
                    str(operation_id),
                    receipt=is_receipt,
                )
                if (
                    is_receipt
                    and json.loads(before)["bundle_sha256"]
                    != bundle_identity["bundle_sha256"]
                ):
                    raise ValueError("installation receipt candidate differs")
                configurations.append(
                    {
                        "path": str(path),
                        "before_sha256": stable.sha256(before),
                        "after_sha256": stable.sha256(after),
                        "receipt": is_receipt,
                    }
                )
            next_stable = stable.generate(
                bundle,
                repository_root,
                new_origin,
                activate=False,
                version=version,
                expected_previous_base_url=old_origin,
            )
            with _identity(owner.st_uid, owner.st_gid):
                next_development: dict[str, Any] = development._generation(
                    development_root,
                    new_origin,
                    _development_records(development_root, previous_development),
                    candidate_identity=bundle_identity,
                    version=version,
                )
            intent = {
                **key,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "old_stable_generation": previous_stable["tree_sha256"],
                "new_stable_generation": next_stable["tree_sha256"],
                "old_development_generation": previous_development["tree_sha256"],
                "new_development_generation": next_development["tree_sha256"],
                "configurations": configurations,
            }
            _write(journal, intent)
        # Verify every old and prepared generation before resuming any publication.
        previous_stable = stable.load_generation(
            repository_root,
            intent["old_stable_generation"],
            expected_base_url=old_origin,
            expected_bundle_sha256=bundle_identity["bundle_sha256"],
        )[1]
        next_stable = stable.load_generation(
            repository_root,
            intent["new_stable_generation"],
            expected_base_url=new_origin,
            expected_bundle_sha256=bundle_identity["bundle_sha256"],
        )[1]
        previous_development = development.load_generation(
            development_root,
            intent["old_development_generation"],
            expected_base_url=old_origin,
        )[1]
        next_development = development.load_generation(
            development_root,
            intent["new_development_generation"],
            expected_base_url=new_origin,
        )[1]
        if (
            previous_development["packages"] != next_development["packages"]
            or any(
                next_development.get(field) != value
                for field, value in bundle_identity.items()
            )
            or any(
                feed["repository_version"] != version
                for feed in (next_stable, next_development)
            )
        ):
            raise ValueError("prepared origin migration evidence differs")
        for root, name in (
            (repository_root, "stable"),
            (development_root, "development"),
        ):
            if (root / "CURRENT").is_symlink() or (
                root / "CURRENT"
            ).read_text().strip() not in {
                intent[f"old_{name}_generation"],
                intent[f"new_{name}_generation"],
            }:
                raise ValueError("repository changed during origin migration")
        for configuration in intent["configurations"]:
            if stable.sha256(_read(Path(configuration["path"]))) not in {
                configuration["before_sha256"],
                configuration["after_sha256"],
            }:
                raise ValueError("site configuration changed during origin migration")
        _verify_database(
            database, development_root, previous_development, next_development
        )
        if interrupt_after == "prepared":
            raise RuntimeError("injected interruption after preparation")
        stable._switch_pointer(repository_root, intent["new_stable_generation"])
        if interrupt_after == "stable":
            raise RuntimeError("injected interruption after stable publication")
        with (
            _identity(owner.st_uid, owner.st_gid),
            development.publication_lock(development_root),
        ):
            (development_root / "ACK").unlink(missing_ok=True)
            development._pointer(
                development_root / "CURRENT", intent["new_development_generation"]
            )
            if interrupt_after == "development":
                raise RuntimeError(
                    "injected interruption after development publication"
                )
            with sqlite3.connect(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT base_url,current_generation,pending_generation "
                    "FROM development_repository_state WHERE id=1"
                ).fetchone()
                if row not in {
                    (old_origin, intent["old_development_generation"], None),
                    (new_origin, intent["new_development_generation"], None),
                }:
                    raise ValueError("database changed during origin migration")
                connection.execute(
                    "UPDATE development_repository_state SET base_url=?,"
                    "bootstrap_sha256=?,"
                    "bootstrap_tree_sha256=?,current_generation=?,"
                    "current_index_sha256=?,updated_at=? WHERE id=1",
                    (
                        new_origin,
                        next_development["bootstrap_zip_sha256"],
                        next_development["bootstrap_tree_sha256"],
                        next_development["tree_sha256"],
                        next_development["addons_xml_sha256"],
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
            if interrupt_after == "database":
                raise RuntimeError("injected interruption after database publication")
            development._pointer(
                development_root / "ACK", intent["new_development_generation"]
            )
            remote_lifecycle._verify_repository_pair(
                database, development_root, new_origin
            )
        for configuration in intent["configurations"]:
            path = Path(configuration["path"])
            if stable.sha256(_read(path)) == configuration["before_sha256"]:
                _, after = _configuration(
                    path,
                    old_origin,
                    new_origin,
                    repository_bind,
                    str(operation_id),
                    receipt=configuration["receipt"],
                )
                if stable.sha256(after) != configuration["after_sha256"]:
                    raise ValueError("prepared configuration bytes differ")
                _replace(path, after, expected_sha256=configuration["before_sha256"])
            if interrupt_after == (
                "receipt" if configuration["receipt"] else "environment"
            ):
                raise RuntimeError(
                    "injected interruption after configuration publication"
                )
        _verify_complete(
            repository_root, database, site_environment, installation_receipt, intent
        )
        completed.parent.mkdir(mode=0o700, exist_ok=True)
        result = {
            **intent,
            "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if completed.exists():
            prior = json.loads(_read(completed))
            if any(prior.get(field) != value for field, value in intent.items()):
                raise ValueError("origin completion receipt differs")
            result = prior
        else:
            _write(completed, result)
        journal.unlink()
        development._fsync(repository_root)
        return dict(result)


def _verify_database(
    database: Path, root: Path, previous: dict[str, Any], following: dict[str, Any]
) -> None:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        if (
            connection.execute(
                "SELECT 1 FROM repository_workspace_deployments "
                "WHERE state NOT IN ('succeeded','failed') LIMIT 1"
            ).fetchone()
            is not None
            or connection.execute(
                "SELECT 1 FROM mcp_workspace_deployment_operations "
                "WHERE deployment_mode='repository' "
                "AND state NOT IN ('succeeded','failed') LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise ValueError(
                "finish or reconcile repository deployments before migration"
            )
        row = connection.execute(
            "SELECT base_url,bootstrap_sha256,bootstrap_tree_sha256,"
            "current_generation,current_index_sha256,pending_generation "
            "FROM development_repository_state WHERE id=1"
        ).fetchone()
        allowed = {
            (
                feed["base_url"],
                feed["bootstrap_zip_sha256"],
                feed["bootstrap_tree_sha256"],
                feed["tree_sha256"],
                feed["addons_xml_sha256"],
                None,
            )
            for feed in (previous, following)
        }
        if row not in allowed:
            raise ValueError("database changed during origin migration")
        actual = set(
            connection.execute(
                "SELECT id,addon_id,version,zip_sha256,tree_sha256,status "
                "FROM development_publications WHERE status IN ('current','superseded')"
            )
        )
        expected = {
            (
                item["publication_id"],
                item["id"],
                item["version"],
                item["sha256"],
                item["tree_sha256"],
                item["status"],
            )
            for item in previous["packages"]
        }
        if actual != expected:
            raise ValueError("publications changed during origin migration")
        development.verify_artifact_inventory(
            root,
            list(
                connection.execute(
                    "SELECT id,addon_id,version,zip_sha256,status "
                    "FROM development_publications"
                )
            ),
        )


def _verify_complete(
    root: Path,
    database: Path,
    environment: Path,
    receipt: Path | None,
    intent: dict[str, Any],
) -> None:
    stable_feed = stable.load_current(
        root,
        expected_base_url=intent["new_origin"],
        expected_bundle_sha256=intent["bundle_sha256"],
    )[1]
    development_feed = development.load_current(
        root / "development-data", expected_base_url=intent["new_origin"]
    )[1]
    if (
        stable_feed["tree_sha256"] != intent["new_stable_generation"]
        or development_feed["tree_sha256"] != intent["new_development_generation"]
    ):
        raise ValueError("completed origin migration no longer matches active feeds")
    remote_lifecycle._verify_repository_pair(
        database, root / "development-data", intent["new_origin"]
    )
    expected_paths = {str(environment)} | ({str(receipt)} if receipt else set())
    if {item["path"] for item in intent["configurations"]} != expected_paths or any(
        stable.sha256(_read(Path(item["path"]))) != item["after_sha256"]
        for item in intent["configurations"]
    ):
        raise ValueError("completed origin migration configuration differs")

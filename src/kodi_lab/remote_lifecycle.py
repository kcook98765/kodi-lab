"""Fail-closed application rollback for the shared, mixed-owner state volume."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from alembic.script import ScriptDirectory

from kodi_lab.build_identity import installed_identity
from kodi_lab.config import Settings
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    ensure_private_directory,
    generate_master_key,
    initialize_state,
)
from kodi_lab.repository.development import (
    load_current as load_development_current,
)
from kodi_lab.repository.development import (
    verify_artifact_inventory,
)
from kodi_lab.repository.stable import load_current as load_stable_current
from kodi_lab.snapshot.recovery import published_snapshot_inventory
from kodi_lab.storage.packaged_migrations import migrate_database, packaged_config
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    ExternalCredentialStore,
    SecretStoreError,
    decode_master_key,
)

FORMAT = 1
DATABASE = Path("database/kodi-lab.db")
EXCLUDED = frozenset(
    {
        "backups",
        "quarantine",
        "run",
        ".lifecycle.lock",
        ".upgrade-pending.json",
        ".restore-pending.json",
        ".recovery-pending.json",
        "snapshots",
        "screenshots",
        "development-source-staging",
        "locks",
    }
)
ROLLBACK_ROOTS = frozenset(
    {
        ".last-upgrade.json",
        ".repository-candidate-transition.json",
        "database",
        "secrets",
        "master-key",
        ".state.json",
        "addon-zips",
        "agent-workspaces",
        "source-promotions",
    }
)
NESTED_STAGING = frozenset(
    {
        "addon-zips/staging",
        "addon-zips/replacement-staging",
        "addon-zips/install-staging",
    }
)
BACKUP_ID = re.compile(r"[0-9a-f]{32}\Z")
REPOSITORY_BACKUP_ROOT = "repository-data"


def _repository_root() -> Path | None:
    configured = os.getenv("KODI_LAB_REPOSITORY_DATA_ROOT")
    if configured is None:
        return None
    path = Path(configured)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise HostBootstrapError("configured repository data is unavailable")
    return path


def _repository_identity(repository_root: Path) -> dict[str, str]:
    if any(
        (repository_root / name).exists() or (repository_root / name).is_symlink()
        for name in ("TRANSITION.json", "ORIGIN-MIGRATION.json")
    ):
        raise HostBootstrapError("repository candidate transition is incomplete")
    try:
        _, stable = load_stable_current(
            repository_root,
            expected_base_url=os.getenv("KODI_LAB_REPOSITORY_BASE_URL") or None,
            expected_bundle_sha256=os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256")
            or None,
        )
        return {
            "base_url": str(stable["base_url"]),
            "stable_tree_sha256": str(stable["tree_sha256"]),
            "stable_bootstrap_sha256": str(stable["bootstrap_zip_sha256"]),
        }
    except (OSError, ValueError, KeyError) as error:
        raise HostBootstrapError(
            "stable repository provenance is unverified"
        ) from error


def _verify_repository_pair(
    database: Path, development_root: Path, base_url: str
) -> None:
    """Bind one SQLite snapshot to exact acknowledged development bytes."""
    try:
        _, feed = load_development_current(development_root, expected_base_url=base_url)
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT base_url,bootstrap_sha256,bootstrap_tree_sha256,"
                "current_generation,current_index_sha256,pending_generation "
                "FROM development_repository_state WHERE id=1"
            ).fetchone()
            if row != (
                base_url,
                feed["bootstrap_zip_sha256"],
                feed["bootstrap_tree_sha256"],
                feed["tree_sha256"],
                feed["addons_xml_sha256"],
                None,
            ):
                raise ValueError("development repository state differs")
            published_rows = connection.execute(
                "SELECT id,addon_id,version,zip_sha256,tree_sha256,status "
                "FROM development_publications "
                "WHERE status IN ('current','superseded')"
            )
            records = {tuple(row) for row in published_rows}
            actual = {
                (
                    item["publication_id"],
                    item["id"],
                    item["version"],
                    item["sha256"],
                    item["tree_sha256"],
                    item["status"],
                )
                for item in cast(list[dict[str, str]], feed["packages"])
            }
            if records != actual:
                raise ValueError("development publication inventory differs")
            pending = {
                f"{identifier}.zip": zip_hash
                for identifier, zip_hash in connection.execute(
                    "SELECT id,zip_sha256 FROM development_publications "
                    "WHERE status='prepared'"
                )
            }
            actual_pending = {
                path.name: _digest(path)
                for path in (development_root / "pending").glob("*.zip")
            }
            if pending != actual_pending:
                raise ValueError("development pending artifact inventory differs")
            storage_rows = connection.execute(
                "SELECT id,addon_id,version,zip_sha256,status "
                "FROM development_publications"
            )
            verify_artifact_inventory(development_root, list(storage_rows))
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        raise HostBootstrapError(
            "development repository and database disagree"
        ) from error


def _copy_repository_backup(
    repository_root: Path, staging: Path, database: Path
) -> dict[str, str]:
    identity = _repository_identity(repository_root)
    source = repository_root / "development-data"
    if not source.is_dir() or source.is_symlink():
        raise HostBootstrapError("development repository data is unavailable")
    _verify_repository_pair(database, source, identity["base_url"])
    for current, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            _rollback_entry(Path(current) / name, directory=name in directories)
    target = staging / REPOSITORY_BACKUP_ROOT / "development-data"
    target.parent.mkdir(mode=0o700)
    shutil.copytree(source, target, symlinks=False)
    _verify_repository_pair(database, target, identity["base_url"])
    return identity


def _has_development_repository_tables(database: Path) -> bool:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    return (
        "development_publications" in names or "development_repository_state" in names
    )


def _development_repository_has_state(database: Path) -> bool:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM development_repository_state LIMIT 1"
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM development_publications LIMIT 1"
            ).fetchone()
        )


def _restore_repository(
    repository_root: Path, backup: Path, identity: dict[str, str]
) -> None:
    if _repository_identity(repository_root) != identity:
        raise HostBootstrapError("stable repository differs from backup binding")
    target = repository_root / "development-data"
    source = backup / REPOSITORY_BACKUP_ROOT / "development-data"
    if not target.is_dir() or target.is_symlink() or not source.is_dir():
        raise HostBootstrapError("development repository restore path is unavailable")
    (target / "ACK").unlink(missing_ok=True)
    temporary = target / f".restore-{uuid4().hex}"
    shutil.copytree(source, temporary)

    def remove_tree(path: Path) -> None:
        item = path.lstat()
        if stat.S_ISDIR(item.st_mode):
            os.chmod(path, 0o700, follow_symlinks=False)
            for child in path.iterdir():
                remove_tree(child)
            path.rmdir()
        else:
            path.unlink()

    for child in list(target.iterdir()):
        if child == temporary:
            continue
        remove_tree(child)
    for child in sorted(temporary.iterdir(), key=lambda item: item.name == "ACK"):
        os.rename(child, target / child.name)
    temporary.rmdir()
    _fsync(target)


def _private(path: Path, *, directory: bool) -> None:
    item = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not kind(item.st_mode)
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
        or item.st_gid != os.getgid()
    ):
        raise HostBootstrapError(f"private state metadata is unsafe: {path}")


def _root(root: Path) -> None:
    _private(root, directory=True)


@contextmanager
def lifecycle_lock(root: Path) -> Iterator[int]:
    """A Web process holds this lock for its entire lifetime; jobs fail if busy."""

    _root(root)
    fd = os.open(
        root / ".lifecycle.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        _private(root / ".lifecycle.lock", directory=False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HostBootstrapError(
                "remote state is in use; stop Web or the other lifecycle command"
            ) from error
        yield fd
    finally:
        os.close(fd)


def _head(database_url: str) -> str:
    with packaged_config(database_url) as config:
        head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise HostBootstrapError("packaged migrations have no head")
    return head


def _revision(database: Path) -> str:
    _private(database, directory=False)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise HostBootstrapError("SQLite integrity check failed")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "alembic_version" not in tables:
            raise HostBootstrapError("database has no Alembic revision")
        revisions = [
            row[0] for row in db.execute("SELECT version_num FROM alembic_version")
        ]
        if len(revisions) != 1 or not isinstance(revisions[0], str):
            raise HostBootstrapError("database has an ambiguous Alembic revision")
        return revisions[0]


def _key_digest(root: Path) -> str:
    path = root / "master-key"
    _private(path, directory=False)
    content = path.read_bytes()
    if len(content) > 128 or not content.endswith(b"\n"):
        raise HostBootstrapError("master key file is invalid")
    try:
        decoded = decode_master_key(content.decode("ascii"))
    except (UnicodeError, RuntimeError) as error:
        raise HostBootstrapError("master key file is invalid") from error
    if decoded is None:
        raise HostBootstrapError("master key file is invalid")
    return hashlib.sha256(decoded).hexdigest()


def _validate_legacy_secrets(root: Path) -> None:
    """Prove a markerless Phase 1 key decrypts referenced remote secrets."""

    key = decode_master_key((root / "master-key").read_text(encoding="ascii"))
    version = Settings(_env_file=None).external_target_master_key_version  # type: ignore[call-arg]
    targets = ExternalCredentialStore(
        root / "secrets" / "external-targets", master_key=key, key_version=version
    )
    bridges = ExternalBridgeSecretStore(
        root / "secrets" / "external-bridges", master_key=key, key_version=version
    )
    database = root / DATABASE
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "external_kodi_targets" in tables:
                for target_id, reference in db.execute(
                    "SELECT target_id, secret_reference FROM external_kodi_targets "
                    "WHERE secret_reference IS NOT NULL"
                ):
                    targets.read(UUID(target_id), UUID(reference))
            if "external_bridge_pairings" in tables:
                for target_id, token, secret in db.execute(
                    "SELECT target_id, pairing_token_reference, "
                    "bridge_secret_reference "
                    "FROM external_bridge_pairings"
                ):
                    if token is not None:
                        bridges.read(UUID(target_id), UUID(token), "pairing_token")
                    if secret is not None:
                        bridges.read(
                            UUID(target_id), UUID(secret), "request_authentication"
                        )
    except (sqlite3.Error, SecretStoreError, ValueError) as error:
        raise HostBootstrapError(
            "Phase 1 master key cannot decrypt referenced remote secrets"
        ) from error


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, object]:
    _private(path, directory=False)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HostBootstrapError("lifecycle metadata is invalid")
    return value


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate(
    root: Path,
    database_url: str,
    *,
    require_head: bool = False,
    require_marker: bool = False,
    allow_pending: bool = False,
) -> str:
    _root(root)
    pending = root / ".upgrade-pending.json"
    if (pending.exists() or pending.is_symlink()) and not allow_pending:
        raise HostBootstrapError(
            "upgrade is incomplete; restore its backup before starting Web"
        )
    restoring = root / ".restore-pending.json"
    if (restoring.exists() or restoring.is_symlink()) and not allow_pending:
        raise HostBootstrapError(
            "restore is incomplete; retry the exact backup before starting Web"
        )
    recovery = root / ".recovery-pending.json"
    if (recovery.exists() or recovery.is_symlink()) and not allow_pending:
        raise HostBootstrapError(
            "clean-volume recovery is incomplete; Web startup is blocked"
        )
    for directory in (
        "database",
        "secrets",
        "secrets/external-targets",
        "secrets/external-bridges",
    ):
        _private(root / directory, directory=True)
    digest = _key_digest(root)
    revision = _revision(root / DATABASE)
    marker = root / ".state.json"
    if marker.exists() or marker.is_symlink():
        metadata = _read_json(marker)
        if metadata != {"format": FORMAT, "key_sha256": digest}:
            raise HostBootstrapError("state marker and master key do not match")
    else:
        _validate_legacy_secrets(root)
        if require_marker:
            raise HostBootstrapError(
                "state marker is missing; run explicit upgrade for a Phase 1 volume"
            )
    if require_head and revision != _head(database_url):
        raise HostBootstrapError(
            f"database revision {revision} does not match installed head"
        )
    return revision


def verify_candidate_transition_completion(
    root: Path, database_url: str, *, before_upgrade: bool = False
) -> dict[str, object] | None:
    """Bind an installed schema to its reviewed repository transition."""
    path = root / ".repository-candidate-transition.json"
    if not path.exists() and not path.is_symlink():
        last_upgrade = root / ".last-upgrade.json"
        if (
            last_upgrade.exists() or last_upgrade.is_symlink()
        ) and "repository_transition_operation_id" in _read_json(last_upgrade):
            raise HostBootstrapError("candidate transition receipt is missing")
        if before_upgrade:
            repository_root = _repository_root()
            if repository_root is not None and (repository_root / "CURRENT").exists():
                raise HostBootstrapError("candidate transition receipt is missing")
        return None
    receipt = _read_json(path)
    if set(receipt) != {
        "backup_id",
        "operation_id",
        "old_bundle_sha256",
        "new_bundle_sha256",
        "old_migration_revision",
        "new_migration_revision",
    } or any(not isinstance(receipt[field], str) for field in receipt):
        raise HostBootstrapError("candidate transition receipt is invalid")
    if not BACKUP_ID.fullmatch(str(receipt["operation_id"])) or any(
        not re.fullmatch(r"[0-9a-f]{64}", str(receipt[field]))
        for field in ("old_bundle_sha256", "new_bundle_sha256")
    ):
        raise HostBootstrapError("candidate transition identity is invalid")
    old_revision = str(receipt["old_migration_revision"])
    new_revision = str(receipt["new_migration_revision"])
    if (
        new_revision != _head(database_url)
        or receipt["new_bundle_sha256"]
        != os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256")
        or _revision(root / DATABASE)
        != (old_revision if before_upgrade else new_revision)
    ):
        raise HostBootstrapError("candidate transition schema or bundle differs")
    backup_metadata = _verify_backup(root, str(receipt["backup_id"]))
    if backup_metadata["revision"] != old_revision:
        raise HostBootstrapError("candidate transition backup revision differs")
    try:
        backup_repository_identity = backup_metadata.get("repository_identity")
        if not isinstance(backup_repository_identity, dict):
            raise ValueError("old candidate backup has no repository identity")
        old_development = load_development_current(
            root
            / "backups"
            / str(receipt["backup_id"])
            / REPOSITORY_BACKUP_ROOT
            / "development-data",
            expected_base_url=str(backup_repository_identity["base_url"]),
        )[1]
        if old_development["bundle_sha256"] != receipt["old_bundle_sha256"]:
            raise ValueError("old candidate differs")
    except (OSError, ValueError, KeyError) as error:
        raise HostBootstrapError(
            "candidate transition backup candidate differs"
        ) from error
    repository_root = _repository_root()
    if repository_root is None:
        raise HostBootstrapError("candidate transition repository is unavailable")
    if any(
        (repository_root / name).exists() or (repository_root / name).is_symlink()
        for name in ("TRANSITION.json", "ORIGIN-MIGRATION.json")
    ):
        raise HostBootstrapError(
            "repository transition or origin migration is incomplete"
        )
    try:
        load_stable_current(
            repository_root,
            expected_base_url=os.getenv("KODI_LAB_REPOSITORY_BASE_URL"),
            expected_bundle_sha256=str(receipt["new_bundle_sha256"]),
        )
        development = load_development_current(
            repository_root / "development-data",
            expected_base_url=os.getenv("KODI_LAB_REPOSITORY_BASE_URL"),
        )[1]
        if development["bundle_sha256"] != receipt["new_bundle_sha256"]:
            raise ValueError("development candidate differs")
        _verify_repository_pair(
            root / DATABASE,
            repository_root / "development-data",
            str(development["base_url"]),
        )
    except (OSError, ValueError, KeyError) as error:
        raise HostBootstrapError("candidate transition repository differs") from error
    if not before_upgrade and old_revision != new_revision:
        completed = _read_json(root / ".last-upgrade.json")
        if any(
            completed.get(field) != value
            for field, value in (
                ("from_revision", old_revision),
                ("to_revision", new_revision),
                ("repository_transition_operation_id", receipt["operation_id"]),
            )
        ):
            raise HostBootstrapError("candidate transition upgrade is incomplete")
    return receipt


def initialize(root: Path, database_url: str) -> str:
    ensure_private_directory(root)
    with lifecycle_lock(root):
        members = {entry.name for entry in root.iterdir()} - {".lifecycle.lock"}
        if members:
            # Repeating initialization is safe only for a complete installation at head.
            return validate(root, database_url, require_head=True, require_marker=True)
        initialize_state(root)
        generate_master_key(root)
        previous_umask = os.umask(0o077)
        try:
            current, _ = migrate_database(root / DATABASE, database_url)
        finally:
            os.umask(previous_umask)
        _write_json(
            root / ".state.json", {"format": FORMAT, "key_sha256": _key_digest(root)}
        )
        validate(root, database_url, require_head=True, require_marker=True)
        return current


def _rollback_entry(path: Path, *, directory: bool) -> os.stat_result:
    """Require exact numeric Web identity and safe, class-specific POSIX modes."""

    item = path.lstat()
    mode = stat.S_IMODE(item.st_mode)
    if (
        (
            not stat.S_ISDIR(item.st_mode)
            if directory
            else not stat.S_ISREG(item.st_mode)
        )
        or item.st_uid != os.getuid()
        or item.st_gid != os.getgid()
        or (not directory and item.st_nlink != 1)
        or mode & 0o7022
    ):
        raise HostBootstrapError(f"rollback state metadata is unsafe: {path}")
    core = bool({"database", "secrets"} & set(path.parts)) or path.name in {
        "master-key",
        ".state.json",
    }
    if core:
        safe_mode = mode == (0o700 if directory else 0o600)
    elif "addon-zips" in path.parts:
        suffix = path.parts[path.parts.index("addon-zips") + 1 :]
        expected = (
            0o700 if directory and len(suffix) <= 1 else (0o500 if directory else 0o400)
        )
        safe_mode = mode == expected
    elif directory and path.name in {"agent-workspaces", "source-promotions"}:
        safe_mode = mode == 0o700
    elif directory:
        safe_mode = mode & 0o500 == 0o500
    else:
        safe_mode = bool(mode & 0o400)
    if not safe_mode:
        raise HostBootstrapError(f"rollback state mode is unsafe: {path}")
    return item


def _skip(path: Path) -> bool:
    return (
        path.as_posix() in NESTED_STAGING
        or (
            len(path.parts) == 3
            and path.parts[:2] == ("addon-zips", "artifacts")
            and path.parts[2].startswith(".publishing-")
        )
        or (
            len(path.parts) == 2
            and path.parts[0] == "agent-workspaces"
            and path.parts[1].startswith(".staging-")
        )
        or (
            len(path.parts) == 3
            and path.parts[0] == "source-promotions"
            and path.parts[2].startswith(".staging-")
        )
    )


def _application_locks(path: Path) -> None:
    """Validate only known ephemeral locks; never replace their live inodes."""

    _private(path, directory=True)
    for scope in path.iterdir():
        if scope.name not in {
            "household-onboarding",
            "managed-agent-lifecycle",
            "external-reconnect",
        }:
            raise HostBootstrapError("unclassified application lock scope")
        _private(scope, directory=True)
        for lock in scope.iterdir():
            if not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.lock", lock.name
            ):
                raise HostBootstrapError("unclassified onboarding lock file")
            _private(lock, directory=False)
            item = lock.lstat()
            if item.st_nlink != 1 or item.st_size != 0:
                raise HostBootstrapError("onboarding lock metadata is unsafe")


def _top_level(root: Path) -> None:
    for child in root.iterdir():
        if child.name == "locks":
            _application_locks(child)
        if child.name in ROLLBACK_ROOTS | EXCLUDED:
            continue
        if child.name.startswith("external-deployment-"):
            continue
        raise HostBootstrapError(f"unclassified state root: {child.name}")


def _files(root: Path, *, exclude_management: bool = True) -> list[Path]:
    """Inventory only the application rollback set without following links."""

    if exclude_management:
        _top_level(root)
    result: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        parent = Path(current)
        relative_parent = parent.relative_to(root)
        if relative_parent == Path(".") and exclude_management:
            dirs[:] = [name for name in dirs if name in ROLLBACK_ROOTS]
            files = [name for name in files if name in ROLLBACK_ROOTS]
        dirs[:] = [name for name in dirs if not _skip(relative_parent / name)]
        files = [name for name in files if not _skip(relative_parent / name)]
        if relative_parent == Path("database"):
            files = [
                name
                for name in files
                if name
                not in {"kodi-lab.db-wal", "kodi-lab.db-shm", "kodi-lab.db-journal"}
            ]
        for name in dirs:
            _rollback_entry(parent / name, directory=True)
        for name in files:
            path = parent / name
            _rollback_entry(path, directory=False)
            result.append(path.relative_to(root))
    return sorted(result)


def _directories(root: Path, *, exclude_management: bool = True) -> list[Path]:
    result: list[Path] = []
    for current, dirs, _files_found in os.walk(root, followlinks=False):
        parent = Path(current)
        if parent == root and exclude_management:
            dirs[:] = [name for name in dirs if name in ROLLBACK_ROOTS]
        relative_parent = parent.relative_to(root)
        dirs[:] = [name for name in dirs if not _skip(relative_parent / name)]
        for name in dirs:
            path = parent / name
            _rollback_entry(path, directory=True)
            result.append(path.relative_to(root))
    return sorted(result)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup(root: Path, database_url: str) -> str:
    revision = validate(root, database_url)
    backup_root = root / "backups"
    ensure_private_directory(backup_root)
    identifier = uuid4().hex
    staging = backup_root / f".{identifier}.tmp"
    staging.mkdir(mode=0o700)
    try:
        files = _files(root)
        directories = _directories(root)
        if DATABASE not in files or Path("master-key") not in files:
            raise HostBootstrapError("required state is absent")
        for relative in directories:
            (staging / relative).mkdir(parents=True, mode=0o700, exist_ok=True)
        manifest_files: dict[str, dict[str, object]] = {}
        for relative in files:
            source = root / relative
            destination = staging / relative
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            source_mode = stat.S_IMODE(_rollback_entry(source, directory=False).st_mode)
            if relative == DATABASE:
                with (
                    sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as db,
                    sqlite3.connect(destination) as copy,
                ):
                    db.backup(copy)
                    copy.execute("PRAGMA journal_mode=DELETE")
            else:
                shutil.copyfile(source, destination, follow_symlinks=False)
            os.chmod(destination, source_mode)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            manifest_files[relative.as_posix()] = {
                "sha256": _digest(destination),
                "size": destination.stat().st_size,
                "uid": os.getuid(),
                "gid": os.getgid(),
                "mode": source_mode,
            }
        repository_identity: dict[str, str] | None = None
        repository_root = _repository_root()
        if _has_development_repository_tables(staging / DATABASE):
            if repository_root is None:
                if _development_repository_has_state(staging / DATABASE):
                    raise HostBootstrapError("Phase 2 backup requires repository data")
            else:
                repository_identity = _copy_repository_backup(
                    repository_root, staging, staging / DATABASE
                )
                copied = staging / REPOSITORY_BACKUP_ROOT
                repository_files = [
                    Path(REPOSITORY_BACKUP_ROOT) / path
                    for path in _files(copied, exclude_management=False)
                ]
                repository_directories = [Path(REPOSITORY_BACKUP_ROOT)] + [
                    Path(REPOSITORY_BACKUP_ROOT) / path
                    for path in _directories(copied, exclude_management=False)
                ]
                for relative in repository_files:
                    item = staging / relative
                    manifest_files[relative.as_posix()] = {
                        "sha256": _digest(item),
                        "size": item.stat().st_size,
                        "uid": os.getuid(),
                        "gid": os.getgid(),
                        "mode": stat.S_IMODE(item.stat().st_mode),
                    }
                directories += repository_directories
        directory_modes = {
            relative.as_posix(): stat.S_IMODE(
                _rollback_entry(
                    (staging if relative.parts[0] == REPOSITORY_BACKUP_ROOT else root)
                    / relative,
                    directory=True,
                ).st_mode
            )
            for relative in directories
        }
        for relative in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            os.chmod(staging / relative, directory_modes[relative.as_posix()])
        if _revision(staging / DATABASE) != revision:
            raise HostBootstrapError("backup database revision changed")
        metadata: dict[str, object] = {
            "format": FORMAT,
            "id": identifier,
            "created_at": datetime.now(UTC).isoformat(),
            "application": installed_identity(),
            "revision": revision,
            "key_sha256": _key_digest(root),
            "files": manifest_files,
            "directories": [path.as_posix() for path in directories],
            "directory_modes": directory_modes,
            "backup_set": "application-rollback-v2",
            "snapshot_dependencies": published_snapshot_inventory(
                root, web_gid=os.getgid()
            ),
            "repository_identity": repository_identity,
            "complete": True,
        }
        _write_json(staging / "manifest.json", metadata)
        _fsync(staging)
        _verify_backup(root, identifier, directory_override=staging)
        os.rename(staging, backup_root / identifier)
        _fsync(backup_root)
        return identifier
    except Exception:
        shutil.rmtree(staging)
        raise


def _verify_backup(
    root: Path, identifier: str, *, directory_override: Path | None = None
) -> dict[str, object]:
    if not BACKUP_ID.fullmatch(identifier):
        raise HostBootstrapError("backup ID is invalid")
    _private(root / "backups", directory=True)
    directory = directory_override or root / "backups" / identifier
    _private(directory, directory=True)
    metadata = _read_json(directory / "manifest.json")
    if (
        metadata.get("format") != FORMAT
        or metadata.get("id") != identifier
        or metadata.get("complete") is not True
    ):
        raise HostBootstrapError("backup metadata is incomplete")
    entries = metadata.get("files")
    if (
        not isinstance(entries, dict)
        or "master-key" not in entries
        or DATABASE.as_posix() not in entries
    ):
        raise HostBootstrapError("backup inventory is incomplete")

    def valid_name(name: object) -> bool:
        return (
            isinstance(name, str)
            and name != ""
            and not name.startswith("/")
            and "\\" not in name
            and all(part not in {"", ".", ".."} for part in name.split("/"))
            and Path(name).as_posix() == name
            and Path(name).parts[0] in ROLLBACK_ROOTS | {REPOSITORY_BACKUP_ROOT}
            and not _skip(Path(name))
        )

    if any(not valid_name(name) for name in entries):
        raise HostBootstrapError("backup inventory contains reserved state")
    actual = {
        path.as_posix() for path in _files(directory, exclude_management=False)
    } - {"manifest.json"}
    if actual != set(entries):
        raise HostBootstrapError("backup inventory differs from manifest")
    recorded_directories = metadata.get("directories")
    if not isinstance(recorded_directories, list) or not all(
        isinstance(path, str) for path in recorded_directories
    ):
        raise HostBootstrapError("backup directory inventory is invalid")
    if any(not valid_name(name) for name in recorded_directories):
        raise HostBootstrapError("backup directories contain reserved state")
    actual_directories = {
        path.as_posix() for path in _directories(directory, exclude_management=False)
    }
    if actual_directories != set(recorded_directories):
        raise HostBootstrapError("backup directories differ from manifest")
    modern = metadata.get("backup_set") == "application-rollback-v2"
    if metadata.get("backup_set") not in (None, "application-rollback-v2"):
        raise HostBootstrapError("backup set is invalid")
    snapshot_dependencies = metadata.get("snapshot_dependencies")
    if modern:
        if (
            not isinstance(snapshot_dependencies, dict)
            or len(snapshot_dependencies) > 10000
        ):
            raise HostBootstrapError("snapshot dependency inventory is invalid")
        for name, hashes in snapshot_dependencies.items():
            try:
                canonical = str(UUID(name)) == name
            except (TypeError, ValueError, AttributeError):
                canonical = False
            if (
                not canonical
                or not isinstance(hashes, dict)
                or set(hashes) != {"manifest.json", "state.tar"}
                or any(
                    not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for digest in hashes.values()
                )
            ):
                raise HostBootstrapError("snapshot dependency inventory is invalid")
    elif snapshot_dependencies is not None:
        raise HostBootstrapError("snapshot dependency inventory is invalid")
    directory_modes = metadata.get("directory_modes")
    if modern:
        if (
            not isinstance(directory_modes, dict)
            or set(directory_modes) != set(recorded_directories)
            or any(
                not isinstance(mode, int) or isinstance(mode, bool)
                for mode in directory_modes.values()
            )
        ):
            raise HostBootstrapError("backup directory metadata is invalid")
    elif directory_modes is not None:
        raise HostBootstrapError("backup directory metadata is invalid")
    for name in recorded_directories:
        item = _rollback_entry(directory / name, directory=True)
        expected = 0o700
        if modern:
            assert isinstance(directory_modes, dict)
            expected = directory_modes[name]
        if stat.S_IMODE(item.st_mode) != expected:
            raise HostBootstrapError("backup directory ownership differs from manifest")
    sidecars = ("-wal", "-shm", "-journal")
    if any(
        (directory / "database" / f"kodi-lab.db{suffix}").exists()
        for suffix in sidecars
    ):
        raise HostBootstrapError("backup contains SQLite sidecars")
    for name, record in entries.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise HostBootstrapError("backup inventory is invalid")
        path = directory / name
        item = _rollback_entry(path, directory=False)
        if modern:
            if (
                record.get("uid") != os.getuid()
                or record.get("gid") != os.getgid()
                or record.get("mode") != stat.S_IMODE(item.st_mode)
            ):
                raise HostBootstrapError("backup ownership differs from manifest")
        elif stat.S_IMODE(item.st_mode) != 0o600:
            raise HostBootstrapError("legacy backup mode is invalid")
        if _digest(path) != record.get("sha256") or path.stat().st_size != record.get(
            "size"
        ):
            raise HostBootstrapError("backup content differs from manifest")
    if _revision(directory / DATABASE) != metadata.get("revision"):
        raise HostBootstrapError("backup database revision differs from manifest")
    if _key_digest(directory) != metadata.get("key_sha256"):
        raise HostBootstrapError("backup master key differs from manifest")
    repository_identity = metadata.get("repository_identity")
    if repository_identity is not None:
        if not isinstance(repository_identity, dict) or set(repository_identity) != {
            "base_url",
            "stable_tree_sha256",
            "stable_bootstrap_sha256",
        }:
            raise HostBootstrapError("backup repository identity is invalid")
        _verify_repository_pair(
            directory / DATABASE,
            directory / REPOSITORY_BACKUP_ROOT / "development-data",
            str(repository_identity["base_url"]),
        )
    elif any(name.startswith(REPOSITORY_BACKUP_ROOT + "/") for name in entries) or (
        _has_development_repository_tables(directory / DATABASE)
        and _development_repository_has_state(directory / DATABASE)
    ):
        raise HostBootstrapError("backup repository inventory lacks binding")
    return metadata


def upgrade(root: Path, database_url: str) -> tuple[str, str, str | None]:
    with lifecycle_lock(root):
        current = validate(root, database_url)
        head = _head(database_url)
        transition_receipt = verify_candidate_transition_completion(
            root, database_url, before_upgrade=current != head
        )
        marker = root / ".state.json"
        if current == head and marker.exists():
            return current, head, None
        identifier = backup(root, database_url)
        _verify_backup(root, identifier)
        _write_json(
            root / ".upgrade-pending.json",
            {"backup_id": identifier, "from_revision": current, "to_revision": head},
        )
        if current == head:
            migrated = current
        else:
            previous_umask = os.umask(0o077)
            try:
                migrated, _ = migrate_database(root / DATABASE, database_url)
            finally:
                os.umask(previous_umask)
        if migrated != head:
            raise HostBootstrapError("migration did not reach head")
        _write_json(
            root / ".state.json", {"format": FORMAT, "key_sha256": _key_digest(root)}
        )
        _write_json(
            root / ".last-upgrade.json",
            {
                "backup_id": identifier,
                "from_revision": current,
                "to_revision": head,
                **(
                    {
                        "repository_transition_operation_id": transition_receipt[
                            "operation_id"
                        ]
                    }
                    if transition_receipt is not None
                    else {}
                ),
            },
        )
        (root / ".upgrade-pending.json").unlink()
        _fsync(root)
        validate(root, database_url, require_head=True, require_marker=True)
        verify_candidate_transition_completion(root, database_url)
        return current, head, identifier


def _verify_restored(root: Path, metadata: dict[str, object]) -> None:
    files = metadata["files"]
    directories = metadata["directories"]
    assert isinstance(files, dict) and isinstance(directories, list)
    application_files = {
        name for name in files if not name.startswith(REPOSITORY_BACKUP_ROOT + "/")
    }
    application_directories = {
        name
        for name in directories
        if name != REPOSITORY_BACKUP_ROOT
        and not name.startswith(REPOSITORY_BACKUP_ROOT + "/")
    }
    if {path.as_posix() for path in _files(root)} != application_files:
        raise HostBootstrapError("restored file inventory differs from backup")
    if {path.as_posix() for path in _directories(root)} != application_directories:
        raise HostBootstrapError("restored directory inventory differs from backup")
    for name, record in files.items():
        assert isinstance(name, str) and isinstance(record, dict)
        if name.startswith(REPOSITORY_BACKUP_ROOT + "/"):
            continue
        item = _rollback_entry(root / name, directory=False)
        expected_mode = record.get("mode", 0o600)
        if (
            stat.S_IMODE(item.st_mode) != expected_mode
            or item.st_size != record["size"]
            or _digest(root / name) != record["sha256"]
        ):
            raise HostBootstrapError("restored content differs from backup")
    directory_modes = metadata.get("directory_modes") or {}
    assert isinstance(directory_modes, dict)
    for name in directories:
        assert isinstance(name, str)
        if name == REPOSITORY_BACKUP_ROOT or name.startswith(
            REPOSITORY_BACKUP_ROOT + "/"
        ):
            continue
        item = _rollback_entry(root / name, directory=True)
        if stat.S_IMODE(item.st_mode) != directory_modes.get(name, 0o700):
            raise HostBootstrapError("restored directory mode differs from backup")


def _verify_snapshot_dependencies(root: Path, metadata: dict[str, object]) -> None:
    actual = published_snapshot_inventory(root, web_gid=os.getgid())
    expected = metadata.get("snapshot_dependencies", {})
    if actual != expected:
        raise HostBootstrapError(
            "published snapshots differ from backup dependency set"
        )


def recover_clean(
    root: Path, database_url: str, identifier: str, confirmation: str
) -> str:
    """Restore one verified application backup into a fresh, private volume.

    An operator copies the private backup directory into ``backups/ID`` first.
    Published broker snapshots may be restored separately before this command.
    No receipt from a destroyed volume or historical container is required.
    """

    with lifecycle_lock(root):
        if identifier != confirmation:
            raise HostBootstrapError("recovery confirmation must repeat the backup ID")
        members = {entry.name for entry in root.iterdir()}
        if members - {".lifecycle.lock", "backups", "snapshots"}:
            raise HostBootstrapError("clean recovery requires empty application state")
        metadata = _verify_backup(root, identifier)
        _verify_snapshot_dependencies(root, metadata)
        repository_identity = metadata.get("repository_identity")
        repository_root = (
            _repository_root() if repository_identity is not None else None
        )
        if repository_identity is not None and (
            repository_root is None
            or _repository_identity(repository_root) != repository_identity
        ):
            raise HostBootstrapError(
                "repository recovery requires matching stable feed"
            )
        _write_json(root / ".recovery-pending.json", {"backup_id": identifier})
        source = root / "backups" / identifier
        for child in source.iterdir():
            if child.name not in {"manifest.json", REPOSITORY_BACKUP_ROOT}:
                if child.is_dir():
                    shutil.copytree(child, root / child.name)
                else:
                    shutil.copyfile(child, root / child.name, follow_symlinks=False)
                    os.chmod(root / child.name, 0o600)
        if repository_identity is not None:
            assert repository_root is not None and isinstance(repository_identity, dict)
            _restore_repository(repository_root, source, repository_identity)
        _fsync(root)
        _verify_restored(root, metadata)
        if repository_identity is not None:
            assert repository_root is not None and isinstance(repository_identity, dict)
            _verify_repository_pair(
                root / DATABASE,
                repository_root / "development-data",
                str(repository_identity["base_url"]),
            )
        revision = validate(root, database_url, allow_pending=True)
        if (
            revision != metadata["revision"]
            or _key_digest(root) != metadata["key_sha256"]
        ):
            raise HostBootstrapError("clean recovery state differs from backup")
        (root / ".recovery-pending.json").unlink()
        _fsync(root)
        return revision


def restore(
    root: Path, database_url: str, identifier: str, confirmation: str
) -> tuple[str, str]:
    with lifecycle_lock(root):
        if identifier != confirmation:
            raise HostBootstrapError(
                "restore confirmation must repeat the exact backup ID"
            )
        restore_pending = root / ".restore-pending.json"
        pending_path = root / ".upgrade-pending.json"
        repository_transition = root / ".repository-candidate-transition.json"
        receipt_path = (
            restore_pending
            if restore_pending.exists() or restore_pending.is_symlink()
            else (
                pending_path
                if pending_path.exists() or pending_path.is_symlink()
                else (
                    repository_transition
                    if repository_transition.exists()
                    or repository_transition.is_symlink()
                    else root / ".last-upgrade.json"
                )
            )
        )
        if not receipt_path.exists() and not receipt_path.is_symlink():
            raise HostBootstrapError("no recorded upgrade to restore")
        receipt = _read_json(receipt_path)
        if receipt.get("backup_id") != identifier:
            raise HostBootstrapError("backup does not match the most recent upgrade")
        metadata = _verify_backup(root, identifier)
        _verify_snapshot_dependencies(root, metadata)
        repository_identity = metadata.get("repository_identity")
        repository_root = (
            _repository_root() if repository_identity is not None else None
        )
        if repository_identity is not None and (
            repository_root is None
            or _repository_identity(repository_root) != repository_identity
        ):
            raise HostBootstrapError("repository restore requires matching stable feed")
        _top_level(root)
        if receipt_path != restore_pending:
            _write_json(restore_pending, {"backup_id": identifier})
        quarantine = root / "quarantine"
        ensure_private_directory(quarantine)
        quarantine_id = uuid4().hex
        destination = quarantine / quarantine_id
        destination.mkdir(mode=0o700)
        # Verify the complete backup before moving any live state. Rename each
        # top-level item within the same volume; a crash leaves startup blocked.
        preserved = {
            "backups",
            "quarantine",
            "run",
            ".lifecycle.lock",
            ".restore-pending.json",
        } | (EXCLUDED - {".upgrade-pending.json", ".last-upgrade.json"})
        for child in list(root.iterdir()):
            if child.name not in preserved and not child.name.startswith(
                "external-deployment-"
            ):
                os.rename(child, destination / child.name)
        source = root / "backups" / identifier
        for child in source.iterdir():
            if child.name not in {"manifest.json", REPOSITORY_BACKUP_ROOT}:
                if child.is_dir():
                    shutil.copytree(child, root / child.name)
                else:
                    shutil.copyfile(child, root / child.name, follow_symlinks=False)
                    os.chmod(root / child.name, 0o600)
        if repository_identity is not None:
            assert repository_root is not None and isinstance(repository_identity, dict)
            _restore_repository(repository_root, source, repository_identity)
        _fsync(root)
        _verify_restored(root, metadata)
        if repository_identity is not None:
            assert repository_root is not None and isinstance(repository_identity, dict)
            _verify_repository_pair(
                root / DATABASE,
                repository_root / "development-data",
                str(repository_identity["base_url"]),
            )
        restored = validate(root, database_url, allow_pending=True)
        if (
            restored != metadata["revision"]
            or _key_digest(root) != metadata["key_sha256"]
        ):
            raise HostBootstrapError("restored state verification failed")
        restore_pending.unlink()
        _fsync(root)
        return restored, quarantine_id

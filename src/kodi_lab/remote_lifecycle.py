"""Fail-closed lifecycle for the single-volume, single-process remote Web host."""

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
        ".last-upgrade.json",
    }
)
BACKUP_ID = re.compile(r"[0-9a-f]{32}\Z")


def _private(path: Path, *, directory: bool) -> None:
    item = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    mode = 0o700 if directory else 0o600
    if (
        not kind(item.st_mode)
        or stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
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


def _files(root: Path, *, exclude_management: bool = True) -> list[Path]:
    """Inventory all state, including artifacts, without following links."""

    result: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        parent = Path(current)
        relative_parent = parent.relative_to(root)
        if relative_parent == Path(".") and exclude_management:
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
            files = [name for name in files if name not in EXCLUDED]
        if relative_parent == Path("database"):
            files = [
                name
                for name in files
                if name
                not in {"kodi-lab.db-wal", "kodi-lab.db-shm", "kodi-lab.db-journal"}
            ]
        for name in dirs:
            _private(parent / name, directory=True)
        for name in files:
            path = parent / name
            _private(path, directory=False)
            result.append(path.relative_to(root))
    return sorted(result)


def _directories(root: Path, *, exclude_management: bool = True) -> list[Path]:
    result: list[Path] = []
    for current, dirs, _files_found in os.walk(root, followlinks=False):
        parent = Path(current)
        if parent == root and exclude_management:
            dirs[:] = [name for name in dirs if name not in EXCLUDED]
        for name in dirs:
            path = parent / name
            _private(path, directory=True)
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
            if relative == DATABASE:
                with (
                    sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as db,
                    sqlite3.connect(destination) as copy,
                ):
                    db.backup(copy)
                    copy.execute("PRAGMA journal_mode=DELETE")
                os.chmod(destination, 0o600)
            else:
                shutil.copyfile(source, destination, follow_symlinks=False)
                os.chmod(destination, 0o600)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
            manifest_files[relative.as_posix()] = {
                "sha256": _digest(destination),
                "size": destination.stat().st_size,
            }
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
    if any(
        not Path(name).parts
        or name == "manifest.json"
        or Path(name).parts[0] in EXCLUDED
        for name in entries
    ):
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
    if any(
        not Path(name).parts or Path(name).parts[0] in EXCLUDED
        for name in recorded_directories
    ):
        raise HostBootstrapError("backup directories contain reserved state")
    actual_directories = {
        path.as_posix() for path in _directories(directory, exclude_management=False)
    }
    if actual_directories != set(recorded_directories):
        raise HostBootstrapError("backup directories differ from manifest")
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
        if _digest(path) != record.get("sha256") or path.stat().st_size != record.get(
            "size"
        ):
            raise HostBootstrapError("backup content differs from manifest")
    if _revision(directory / DATABASE) != metadata.get("revision"):
        raise HostBootstrapError("backup database revision differs from manifest")
    if _key_digest(directory) != metadata.get("key_sha256"):
        raise HostBootstrapError("backup master key differs from manifest")
    return metadata


def upgrade(root: Path, database_url: str) -> tuple[str, str, str | None]:
    with lifecycle_lock(root):
        current = validate(root, database_url)
        head = _head(database_url)
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
            {"backup_id": identifier, "from_revision": current, "to_revision": head},
        )
        (root / ".upgrade-pending.json").unlink()
        _fsync(root)
        validate(root, database_url, require_head=True, require_marker=True)
        return current, head, identifier


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
        receipt_path = (
            restore_pending
            if restore_pending.exists() or restore_pending.is_symlink()
            else (
                pending_path
                if pending_path.exists() or pending_path.is_symlink()
                else root / ".last-upgrade.json"
            )
        )
        if not receipt_path.exists() and not receipt_path.is_symlink():
            raise HostBootstrapError("no recorded upgrade to restore")
        receipt = _read_json(receipt_path)
        if receipt.get("backup_id") != identifier:
            raise HostBootstrapError("backup does not match the most recent upgrade")
        metadata = _verify_backup(root, identifier)
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
        }
        for child in list(root.iterdir()):
            if child.name not in preserved:
                os.rename(child, destination / child.name)
        source = root / "backups" / identifier
        for child in source.iterdir():
            if child.name != "manifest.json":
                if child.is_dir():
                    shutil.copytree(child, root / child.name)
                else:
                    shutil.copyfile(child, root / child.name, follow_symlinks=False)
                    os.chmod(root / child.name, 0o600)
        _fsync(root)
        restored = validate(root, database_url, allow_pending=True)
        if (
            restored != metadata["revision"]
            or _key_digest(root) != metadata["key_sha256"]
        ):
            raise HostBootstrapError("restored state verification failed")
        restore_pending.unlink()
        _fsync(root)
        return restored, quarantine_id

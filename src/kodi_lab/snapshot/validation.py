"""Independent validation for broker-published immutable snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path, PurePosixPath
from typing import Any, cast

from kodi_lab.snapshot.storage import (
    KODI_ARCHIVE_GID,
    KODI_ARCHIVE_UID,
    MANIFEST_VERSION,
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_MEMBERS,
    SnapshotArtifactIdentity,
    SnapshotCaptureContext,
    SnapshotMemberRecord,
    SnapshotStorageError,
    _classify_path,
    _IteratorReader,
    _normalized_source_path,
    _safe_source_member,
    snapshot_directory,
)


@dataclass(frozen=True, slots=True)
class SnapshotValidation:
    artifact: SnapshotArtifactIdentity
    recognized_databases_checked: int
    members: tuple[SnapshotMemberRecord, ...] = ()


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        size = path.stat(follow_symlinks=False).st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise SnapshotStorageError(
                "snapshot_manifest_invalid", "Snapshot manifest size is invalid."
            )
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("manifest root")
        canonical = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        if payload != canonical:
            raise SnapshotStorageError(
                "snapshot_manifest_not_canonical",
                "Snapshot manifest is not in canonical form.",
            )
        return (
            cast(dict[str, Any], document),
            payload,
            f"sha256:{hashlib.sha256(payload).hexdigest()}",
        )
    except SnapshotStorageError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot manifest is invalid."
        ) from error


def _require_exact_keys(document: dict[str, Any], keys: set[str]) -> None:
    if set(document) != keys:
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot manifest fields are invalid."
        )


def _validate_manifest_identity(
    document: dict[str, Any], context: SnapshotCaptureContext
) -> tuple[list[dict[str, Any]], str, int, int]:
    _require_exact_keys(
        document,
        {
            "archive",
            "captured_at",
            "captured_fixture_definition_id",
            "kodi_release",
            "manifest_version",
            "members",
            "prior_runtime_state",
            "runtime_revision_id",
            "snapshot_id",
            "source_managed_instance_id",
        },
    )
    fixture_id = (
        None
        if context.captured_fixture_definition_id is None
        else str(context.captured_fixture_definition_id)
    )
    expected = {
        "captured_fixture_definition_id": fixture_id,
        "kodi_release": context.kodi_release,
        "manifest_version": MANIFEST_VERSION,
        "prior_runtime_state": context.prior_runtime_state,
        "runtime_revision_id": str(context.runtime_revision_id),
        "snapshot_id": str(context.snapshot_id),
        "source_managed_instance_id": str(context.source_managed_instance_id),
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise SnapshotStorageError(
            "snapshot_identity_mismatch",
            "Snapshot manifest identity differs from trusted state.",
        )
    captured_at = (
        context.captured_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if document.get("captured_at") != captured_at:
        raise SnapshotStorageError(
            "snapshot_identity_mismatch",
            "Snapshot capture timestamp differs from trusted state.",
        )
    archive = document.get("archive")
    members = document.get("members")
    if not isinstance(archive, dict) or not isinstance(members, list):
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot manifest inventory is invalid."
        )
    archive = cast(dict[str, Any], archive)
    _require_exact_keys(archive, {"byte_size", "member_count", "sha256"})
    digest = archive.get("sha256")
    archive_size = archive.get("byte_size")
    member_count = archive.get("member_count")
    if (
        not isinstance(digest, str)
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in digest[7:])
        or not isinstance(archive_size, int)
        or isinstance(archive_size, bool)
        or archive_size <= 0
        or archive_size > MAX_ARCHIVE_BYTES
        or not isinstance(member_count, int)
        or isinstance(member_count, bool)
        or member_count < 1
        or member_count > MAX_MEMBERS
        or member_count != len(members)
    ):
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot archive identity is invalid."
        )
    return cast(list[dict[str, Any]], members), digest, archive_size, member_count


def _validate_storage_identity(
    data_root: Path, context: SnapshotCaptureContext, owner_uid: int, owner_gid: int
) -> tuple[Path, Path]:
    directory = snapshot_directory(data_root, context.snapshot_id)
    manifest = directory / "manifest.json"
    archive = directory / "state.tar"
    try:
        directory_stat = directory.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != owner_uid
            or directory_stat.st_gid != owner_gid
            or stat.S_IMODE(directory_stat.st_mode) != 0o750
        ):
            raise SnapshotStorageError(
                "snapshot_storage_identity_invalid",
                "Snapshot storage identity or permissions are invalid.",
            )
        if {entry.name for entry in os.scandir(directory)} != {
            "manifest.json",
            "state.tar",
        }:
            raise SnapshotStorageError(
                "snapshot_storage_identity_invalid",
                "Snapshot storage contains unexpected entries.",
            )
        for path in (manifest, archive):
            item = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_nlink != 1
                or item.st_uid != owner_uid
                or item.st_gid != owner_gid
                or stat.S_IMODE(item.st_mode) != 0o440
            ):
                raise SnapshotStorageError(
                    "snapshot_storage_identity_invalid",
                    "Snapshot artifact identity or permissions are invalid.",
                )
    except SnapshotStorageError:
        raise
    except OSError as error:
        raise SnapshotStorageError(
            "snapshot_storage_unavailable", "Snapshot artifact is unavailable."
        ) from error
    return manifest, archive


def _recognized_database(path: str) -> bool:
    item = PurePosixPath(path)
    return (
        len(item.parts) == 3
        and item.parts[0:2] == ("userdata", "Database")
        and item.name.startswith("MyVideos")
        and item.name.endswith(".db")
    )


def _validate_member_document(value: object, previous: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot member record is invalid."
        )
    member = cast(dict[str, Any], value)
    _require_exact_keys(member, {"mode", "path", "sha256", "size", "type"})
    path = member.get("path")
    member_type = member.get("type")
    size = member.get("size")
    mode = member.get("mode")
    digest = member.get("sha256")
    if (
        not isinstance(path, str)
        or not path
        or _normalized_source_path(f".kodi/{path}") != path
        or _classify_path(path) != "include"
        or (previous is not None and path <= previous)
        or member_type not in {"file", "directory"}
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or size > MAX_FILE_BYTES
        or not isinstance(mode, int)
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o777
    ):
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot member record is invalid."
        )
    if member_type == "directory":
        valid_digest = digest is None and size == 0
    else:
        valid_digest = (
            isinstance(digest, str)
            and len(digest) == 71
            and digest.startswith("sha256:")
            and not any(char not in "0123456789abcdef" for char in digest[7:])
        )
    if not valid_digest:
        raise SnapshotStorageError(
            "snapshot_manifest_invalid", "Snapshot member digest is invalid."
        )
    return member


def _check_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise SnapshotStorageError(
            "snapshot_database_invalid",
            "A recognized Kodi database failed an integrity check.",
        ) from error
    if result != [("ok",)]:
        raise SnapshotStorageError(
            "snapshot_database_invalid",
            "A recognized Kodi database failed an integrity check.",
        )


def validate_published_snapshot(
    *,
    data_root: Path,
    context: SnapshotCaptureContext,
    owner_uid: int = 0,
    owner_gid: int,
    database_validation_started: Callable[[], None] | None = None,
) -> SnapshotValidation:
    """Independently validate storage, manifest, archive, and recognized DBs."""

    manifest_path, archive_path = _validate_storage_identity(
        data_root, context, owner_uid, owner_gid
    )
    document, _manifest_bytes, manifest_digest = _load_manifest(manifest_path)
    expected_members, expected_digest, expected_size, expected_count = (
        _validate_manifest_identity(document, context)
    )
    archive_hasher = hashlib.sha256()
    checked = 0
    previous: str | None = None
    recognized_databases: list[Path] = []
    try:
        actual_size = archive_path.stat(follow_symlinks=False).st_size
        if actual_size != expected_size or actual_size > MAX_ARCHIVE_BYTES:
            raise SnapshotStorageError(
                "snapshot_archive_invalid", "Snapshot archive size is invalid."
            )
        with archive_path.open("rb") as archive_file:
            while chunk := archive_file.read(1024 * 1024):
                archive_hasher.update(chunk)
        actual_digest = f"sha256:{archive_hasher.hexdigest()}"
        if actual_digest != expected_digest:
            raise SnapshotStorageError(
                "snapshot_archive_invalid", "Snapshot archive digest is invalid."
            )
        with (
            tempfile.TemporaryDirectory(prefix="kodi-lab-snapshot-validate-") as temp,
            tarfile.open(archive_path, mode="r:") as archive,
        ):
            infos = archive.getmembers()
            if len(infos) != expected_count:
                raise SnapshotStorageError(
                    "snapshot_archive_invalid",
                    "Snapshot archive member count is invalid.",
                )
            for index, (info, raw_expected) in enumerate(
                zip(infos, expected_members, strict=True)
            ):
                expected = _validate_member_document(raw_expected, previous)
                path = cast(str, expected["path"])
                previous = path
                if (
                    info.name != path
                    or info.type
                    not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}
                    or (info.isdir() and expected["type"] != "directory")
                    or (info.isfile() and expected["type"] != "file")
                    or info.size != expected["size"]
                    or stat.S_IMODE(info.mode) != expected["mode"]
                    or info.uid != KODI_ARCHIVE_UID
                    or info.gid != KODI_ARCHIVE_GID
                    or info.uname != "kodi"
                    or info.gname != "kodi"
                    or info.mtime != 0
                ):
                    raise SnapshotStorageError(
                        "snapshot_archive_invalid",
                        "Snapshot archive member metadata is invalid.",
                    )
                if info.isfile():
                    source = archive.extractfile(info)
                    if source is None:
                        raise SnapshotStorageError(
                            "snapshot_archive_invalid",
                            "Snapshot archive member could not be read.",
                        )
                    hasher = hashlib.sha256()
                    database_target: Path | None = None
                    database_file = None
                    if _recognized_database(path):
                        database_target = Path(temp) / f"database-{index}.db"
                        database_file = database_target.open("xb")
                    try:
                        while chunk := source.read(1024 * 1024):
                            hasher.update(chunk)
                            if database_file is not None:
                                database_file.write(chunk)
                    finally:
                        if database_file is not None:
                            database_file.close()
                    if f"sha256:{hasher.hexdigest()}" != expected["sha256"]:
                        raise SnapshotStorageError(
                            "snapshot_archive_invalid",
                            "Snapshot archive member digest is invalid.",
                        )
                    if database_target is not None:
                        recognized_databases.append(database_target)
            if database_validation_started is not None:
                database_validation_started()
            for database_target in recognized_databases:
                _check_sqlite(database_target)
                checked += 1
    except SnapshotStorageError:
        raise
    except (OSError, tarfile.TarError, ValueError) as error:
        raise SnapshotStorageError(
            "snapshot_archive_invalid", "Snapshot archive is invalid."
        ) from error
    validated_records = tuple(
        SnapshotMemberRecord(
            path=cast(str, member["path"]),
            member_type=cast(Any, member["type"]),
            size=cast(int, member["size"]),
            mode=cast(int, member["mode"]),
            sha256=cast(str | None, member["sha256"]),
        )
        for member in expected_members
    )
    return SnapshotValidation(
        artifact=SnapshotArtifactIdentity(
            snapshot_id=context.snapshot_id,
            archive_digest=expected_digest,
            manifest_digest=manifest_digest,
            archive_size=expected_size,
            member_count=expected_count,
        ),
        recognized_databases_checked=checked,
        members=validated_records,
    )


def validate_restored_snapshot_source(
    *,
    source_chunks: Iterable[bytes],
    expected_members: tuple[SnapshotMemberRecord, ...],
) -> int:
    """Verify the fixed restored profile readback against the trusted manifest."""

    expected_index = 0
    checked = 0
    try:
        with (
            tempfile.TemporaryDirectory(prefix="kodi-lab-restore-validate-") as temp,
            _IteratorReader(source_chunks) as reader,
            tarfile.open(fileobj=cast(Any, reader), mode="r|*") as archive,
        ):
            for source_index, info in enumerate(archive):
                source_member = _safe_source_member(info)
                if source_member is None:
                    continue
                if expected_index >= len(expected_members):
                    raise SnapshotStorageError(
                        "snapshot_restore_verification_failed",
                        "Restored profile contains unexpected approved state.",
                    )
                expected = expected_members[expected_index]
                expected_index += 1
                if (
                    source_member.path != expected.path
                    or source_member.member_type != expected.member_type
                    or source_member.size != expected.size
                    or source_member.mode != expected.mode
                ):
                    raise SnapshotStorageError(
                        "snapshot_restore_verification_failed",
                        "Restored profile differs from the snapshot manifest.",
                    )
                if info.isfile():
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        raise SnapshotStorageError(
                            "snapshot_restore_verification_failed",
                            "Restored profile member could not be read.",
                        )
                    hasher = hashlib.sha256()
                    database_target: Path | None = None
                    database_file = None
                    if _recognized_database(expected.path):
                        database_target = Path(temp) / f"database-{source_index}.db"
                        database_file = database_target.open("xb")
                    try:
                        while chunk := extracted.read(1024 * 1024):
                            hasher.update(chunk)
                            if database_file is not None:
                                database_file.write(chunk)
                    finally:
                        if database_file is not None:
                            database_file.close()
                    if f"sha256:{hasher.hexdigest()}" != expected.sha256:
                        raise SnapshotStorageError(
                            "snapshot_restore_verification_failed",
                            "Restored profile member digest differs.",
                        )
                    if database_target is not None:
                        _check_sqlite(database_target)
                        checked += 1
        if expected_index != len(expected_members):
            raise SnapshotStorageError(
                "snapshot_restore_verification_failed",
                "Restored profile is missing snapshot members.",
            )
    except SnapshotStorageError:
        raise
    except (OSError, tarfile.TarError, ValueError) as error:
        raise SnapshotStorageError(
            "snapshot_restore_verification_failed",
            "Restored profile could not be verified safely.",
        ) from error
    return checked

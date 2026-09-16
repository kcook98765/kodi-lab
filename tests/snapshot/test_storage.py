from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from kodi_lab.snapshot import storage
from kodi_lab.snapshot.storage import (
    SnapshotArtifactIdentity,
    SnapshotCaptureContext,
    SnapshotStorageError,
    enumerate_snapshot_source,
    publish_snapshot_from_source,
)
from kodi_lab.snapshot.validation import validate_published_snapshot

SNAPSHOT_ID = UUID("90000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("90000000-0000-4000-8000-000000000002")
REVISION_ID = UUID("90000000-0000-4000-8000-000000000003")
FIXTURE_ID = UUID("90000000-0000-4000-8000-000000000004")


def context(snapshot_id: UUID = SNAPSHOT_ID) -> SnapshotCaptureContext:
    return SnapshotCaptureContext(
        snapshot_id=snapshot_id,
        source_managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        kodi_release="21.3",
        captured_fixture_definition_id=FIXTURE_ID,
        captured_at=datetime(2026, 9, 15, 12, tzinfo=UTC),
        prior_runtime_state="stopped",
    )


def source_tar(
    members: list[tuple[str, bytes | None, bytes]],
) -> bytes:
    """Build Docker-shaped input; tuple is name, data, tar type."""

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as archive:
        root = tarfile.TarInfo(".kodi")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, data, member_type in members:
            info = tarfile.TarInfo(f".kodi/{name}")
            info.type = member_type
            info.mode = 0o6755
            info.size = 0 if data is None else len(data)
            if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = ".kodi/userdata"
            archive.addfile(info, None if data is None else io.BytesIO(data))
    return output.getvalue()


def approved_tar() -> bytes:
    return source_tar(
        [
            ("addons", None, tarfile.DIRTYPE),
            ("addons/plugin.example", None, tarfile.DIRTYPE),
            ("addons/plugin.example/settings.xml", b"settings", tarfile.REGTYPE),
            ("fixture-media", None, tarfile.DIRTYPE),
            ("fixture-media/movie.mkv", b"synthetic", tarfile.REGTYPE),
            ("temp", None, tarfile.DIRTYPE),
            ("temp/kodi.log", b"excluded", tarfile.REGTYPE),
            ("userdata", None, tarfile.DIRTYPE),
            ("userdata/Database", None, tarfile.DIRTYPE),
            ("userdata/guisettings.xml", b"<settings/>", tarfile.REGTYPE),
        ]
    )


def publish(
    tmp_path: Path, payload: bytes, snapshot_id: UUID = SNAPSHOT_ID
) -> SnapshotArtifactIdentity:
    inventory = enumerate_snapshot_source([payload])
    return publish_snapshot_from_source(
        data_root=tmp_path,
        context=context(snapshot_id),
        expected_members=inventory,
        source_chunks=[payload],
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )


def test_deterministic_sorted_tar_and_manifest_validation(tmp_path: Path) -> None:
    payload = approved_tar()
    first = publish(tmp_path / "first", payload, SNAPSHOT_ID)
    second_id = UUID("90000000-0000-4000-8000-000000000099")
    second = publish(tmp_path / "second", payload, second_id)
    first_tar = tmp_path / "first/snapshots" / str(SNAPSHOT_ID) / "state.tar"
    second_tar = tmp_path / "second/snapshots" / str(second_id) / "state.tar"
    assert first_tar.read_bytes() == second_tar.read_bytes()
    assert first.archive_digest == second.archive_digest
    with tarfile.open(first_tar, "r:") as archive:
        names = archive.getnames()
        assert names == sorted(names)
        assert all(not name.startswith("temp") for name in names)
        assert all(member.uid == 1000 and member.gid == 1000 for member in archive)
        assert all(member.mtime == 0 for member in archive)
        assert all(stat.S_IMODE(member.mode) == 0o755 for member in archive)
    validated = validate_published_snapshot(
        data_root=tmp_path / "first",
        context=context(),
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert validated.artifact == first
    assert validated.recognized_databases_checked == 0


@pytest.mark.parametrize(
    ("name", "member_type"),
    [
        ("userdata/link", tarfile.SYMTYPE),
        ("userdata/hard", tarfile.LNKTYPE),
        ("userdata/fifo", tarfile.FIFOTYPE),
        ("userdata/device", tarfile.CHRTYPE),
    ],
)
def test_unsafe_filesystem_types_are_rejected(name: str, member_type: bytes) -> None:
    payload = source_tar(
        [("userdata", None, tarfile.DIRTYPE), (name, None, member_type)]
    )
    with pytest.raises(SnapshotStorageError, match=r"unsupported|link"):
        enumerate_snapshot_source([payload])


@pytest.mark.parametrize(
    "raw_name",
    ["/.kodi/userdata/file", ".kodi/../escape", ".kodi//userdata/file"],
)
def test_unsafe_paths_are_rejected(raw_name: str) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        root = tarfile.TarInfo(".kodi")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        info = tarfile.TarInfo(raw_name)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(SnapshotStorageError, match="unsafe"):
        enumerate_snapshot_source([output.getvalue()])


@pytest.mark.parametrize("populated", [False, True])
def test_other_unknown_top_level_state_fails_closed(populated: bool) -> None:
    members: list[tuple[str, bytes | None, bytes]] = [
        ("mystery", None, tarfile.DIRTYPE)
    ]
    if populated:
        members.append(("mystery/file", b"secret", tarfile.REGTYPE))
    payload = source_tar(members)
    with pytest.raises(SnapshotStorageError) as captured:
        enumerate_snapshot_source([payload])
    assert captured.value.code == "unknown_snapshot_profile_state"


@pytest.mark.parametrize("scaffolding", ["media", "system"])
def test_empty_runtime_scaffolding_is_accepted_and_omitted(
    tmp_path: Path, scaffolding: str
) -> None:
    payload = source_tar(
        [
            (scaffolding, None, tarfile.DIRTYPE),
            ("userdata", None, tarfile.DIRTYPE),
            ("userdata/guisettings.xml", b"<settings/>", tarfile.REGTYPE),
        ]
    )
    inventory = enumerate_snapshot_source([payload])
    assert scaffolding not in {member.path for member in inventory}
    publish(tmp_path, payload)
    snapshot_root = tmp_path / "snapshots" / str(SNAPSHOT_ID)
    manifest = json.loads((snapshot_root / "manifest.json").read_text())
    assert scaffolding not in {member["path"] for member in manifest["members"]}
    with tarfile.open(snapshot_root / "state.tar", "r:") as archive:
        assert scaffolding not in archive.getnames()


def test_both_empty_runtime_scaffolding_directories_are_accepted() -> None:
    payload = source_tar(
        [
            ("media", None, tarfile.DIRTYPE),
            ("system", None, tarfile.DIRTYPE),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    inventory = enumerate_snapshot_source([payload])
    assert {member.path for member in inventory} == {"userdata"}


@pytest.mark.parametrize("scaffolding", ["media", "system"])
@pytest.mark.parametrize(
    ("suffix", "data", "member_type"),
    [
        ("file", b"secret", tarfile.REGTYPE),
        ("nested", None, tarfile.DIRTYPE),
    ],
)
def test_nonempty_runtime_scaffolding_is_rejected(
    scaffolding: str,
    suffix: str,
    data: bytes | None,
    member_type: bytes,
) -> None:
    payload = source_tar(
        [
            (scaffolding, None, tarfile.DIRTYPE),
            (f"{scaffolding}/{suffix}", data, member_type),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    with pytest.raises(SnapshotStorageError) as captured:
        enumerate_snapshot_source([payload])
    assert captured.value.code == "nonempty_snapshot_scaffolding"
    assert suffix not in captured.value.safe_message


@pytest.mark.parametrize("scaffolding", ["media", "system"])
@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.FIFOTYPE])
def test_unsafe_runtime_scaffolding_entry_is_rejected(
    scaffolding: str, member_type: bytes
) -> None:
    payload = source_tar(
        [
            (scaffolding, None, tarfile.DIRTYPE),
            (f"{scaffolding}/unsafe", None, member_type),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    with pytest.raises(SnapshotStorageError) as captured:
        enumerate_snapshot_source([payload])
    assert captured.value.code == "unsafe_snapshot_member"
    assert "unsafe" not in captured.value.safe_message


def test_scaffolding_change_between_passes_fails_before_publication(
    tmp_path: Path,
) -> None:
    empty = source_tar(
        [
            ("media", None, tarfile.DIRTYPE),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    changed = source_tar(
        [
            ("media", None, tarfile.DIRTYPE),
            ("media/future-state", b"secret", tarfile.REGTYPE),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    inventory = enumerate_snapshot_source([empty])
    with pytest.raises(SnapshotStorageError) as captured:
        publish_snapshot_from_source(
            data_root=tmp_path,
            context=context(),
            expected_members=inventory,
            source_chunks=[changed],
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert captured.value.code == "nonempty_snapshot_scaffolding"
    assert not (tmp_path / "snapshots" / str(SNAPSHOT_ID)).exists()


def test_exact_runtime_logs_are_excluded_without_broad_log_ignore() -> None:
    payload = source_tar(
        [
            ("kodi-stdout.log", b"excluded", tarfile.REGTYPE),
            ("userdata", None, tarfile.DIRTYPE),
            ("xvfb.log", b"excluded", tarfile.REGTYPE),
        ]
    )
    inventory = enumerate_snapshot_source([payload])
    assert {member.path for member in inventory} == {"userdata"}

    unknown = source_tar(
        [
            ("mystery.log", b"secret", tarfile.REGTYPE),
            ("userdata", None, tarfile.DIRTYPE),
        ]
    )
    with pytest.raises(SnapshotStorageError) as captured:
        enumerate_snapshot_source([unknown])
    assert captured.value.code == "unknown_snapshot_profile_state"


def test_exact_exclusions_are_not_manifested(tmp_path: Path) -> None:
    payload = source_tar(
        [
            ("addons", None, tarfile.DIRTYPE),
            ("addons/packages", None, tarfile.DIRTYPE),
            ("addons/packages/cache.zip", b"excluded", tarfile.REGTYPE),
            ("addons/temp", None, tarfile.DIRTYPE),
            ("userdata", None, tarfile.DIRTYPE),
            ("userdata/Database", None, tarfile.DIRTYPE),
            ("userdata/Database/MyVideos121.db", b"opaque", tarfile.REGTYPE),
            ("userdata/Database/Textures13.db", b"excluded", tarfile.REGTYPE),
            ("userdata/Thumbnails", None, tarfile.DIRTYPE),
            ("userdata/Thumbnails/a.jpg", b"excluded", tarfile.REGTYPE),
        ]
    )
    inventory = enumerate_snapshot_source([payload])
    paths = {item.path for item in inventory}
    assert "userdata/Database/MyVideos121.db" in paths
    assert not any("packages" in path for path in paths)
    assert not any("Thumbnails" in path for path in paths)
    assert not any("Textures" in path for path in paths)


def test_file_size_member_count_and_path_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    too_large = source_tar(
        [("userdata", None, tarfile.DIRTYPE), ("userdata/file", b"xx", tarfile.REGTYPE)]
    )
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 1)
    with pytest.raises(SnapshotStorageError) as file_error:
        enumerate_snapshot_source([too_large])
    assert file_error.value.code == "snapshot_file_too_large"
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 8 * 1024**3)
    monkeypatch.setattr(storage, "MAX_MEMBERS", 1)
    with pytest.raises(SnapshotStorageError) as count_error:
        enumerate_snapshot_source([too_large])
    assert count_error.value.code == "snapshot_member_limit_exceeded"
    monkeypatch.setattr(storage, "MAX_MEMBERS", 250_000)
    monkeypatch.setattr(storage, "MAX_RELATIVE_PATH_BYTES", 10)
    with pytest.raises(SnapshotStorageError) as path_error:
        enumerate_snapshot_source([too_large])
    assert path_error.value.code == "snapshot_path_too_long"


def test_disk_reserve_fails_before_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = approved_tar()
    inventory = enumerate_snapshot_source([payload])
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(
        "kodi_lab.snapshot.storage.shutil.disk_usage",
        lambda _path: usage_type(0, 0, 0),
    )
    with pytest.raises(SnapshotStorageError) as captured:
        publish_snapshot_from_source(
            data_root=tmp_path,
            context=context(),
            expected_members=inventory,
            source_chunks=[payload],
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert captured.value.code == "insufficient_snapshot_storage"
    assert not (tmp_path / "snapshots/.staging").exists()


def test_atomic_permissions_collision_and_staging_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = approved_tar()
    artifact = publish(tmp_path, payload)
    directory = tmp_path / "snapshots" / str(artifact.snapshot_id)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert stat.S_IMODE((directory / "state.tar").stat().st_mode) == 0o440
    assert stat.S_IMODE((directory / "manifest.json").stat().st_mode) == 0o440
    with pytest.raises(SnapshotStorageError) as collision:
        publish(tmp_path, payload)
    assert collision.value.code == "snapshot_already_exists"

    other = UUID("90000000-0000-4000-8000-000000000077")
    inventory = enumerate_snapshot_source([payload])
    monkeypatch.setattr(storage, "MAX_MANIFEST_BYTES", 1)
    with pytest.raises(SnapshotStorageError) as failed:
        publish_snapshot_from_source(
            data_root=tmp_path,
            context=context(other),
            expected_members=inventory,
            source_chunks=[payload],
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert failed.value.code == "snapshot_manifest_too_large"
    assert not (tmp_path / "snapshots/.staging" / str(other)).exists()
    assert directory.is_dir()


def test_manifest_tamper_is_rejected(tmp_path: Path) -> None:
    payload = approved_tar()
    publish(tmp_path, payload)
    manifest = tmp_path / "snapshots" / str(SNAPSHOT_ID) / "manifest.json"
    document = json.loads(manifest.read_text())
    document["kodi_release"] = "22.0"
    manifest.chmod(0o640)
    manifest.write_text(json.dumps(document))
    manifest.chmod(0o440)
    with pytest.raises(SnapshotStorageError):
        validate_published_snapshot(
            data_root=tmp_path,
            context=context(),
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_recognized_kodi_sqlite_integrity_is_checked(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE movie (id INTEGER PRIMARY KEY, title TEXT)")
    connection.execute("INSERT INTO movie (title) VALUES ('synthetic')")
    connection.commit()
    connection.close()
    payload = source_tar(
        [
            ("userdata", None, tarfile.DIRTYPE),
            ("userdata/Database", None, tarfile.DIRTYPE),
            (
                "userdata/Database/MyVideos131.db",
                database.read_bytes(),
                tarfile.REGTYPE,
            ),
        ]
    )
    publish(tmp_path / "valid", payload)
    result = validate_published_snapshot(
        data_root=tmp_path / "valid",
        context=context(),
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    assert result.recognized_databases_checked == 1

    corrupt_payload = source_tar(
        [
            ("userdata", None, tarfile.DIRTYPE),
            ("userdata/Database", None, tarfile.DIRTYPE),
            (
                "userdata/Database/MyVideos131.db",
                b"not sqlite",
                tarfile.REGTYPE,
            ),
        ]
    )
    publish(tmp_path / "corrupt", corrupt_payload)
    with pytest.raises(SnapshotStorageError) as captured:
        validate_published_snapshot(
            data_root=tmp_path / "corrupt",
            context=context(),
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert captured.value.code == "snapshot_database_invalid"

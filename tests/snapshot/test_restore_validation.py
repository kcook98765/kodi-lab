from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from kodi_lab.snapshot.storage import SnapshotMemberRecord, SnapshotStorageError
from kodi_lab.snapshot.validation import validate_restored_snapshot_source


def source_tar(
    *,
    content: bytes = b"settings",
    path: str = ".kodi/userdata/guisettings.xml",
    member_type: bytes = tarfile.REGTYPE,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        root = tarfile.TarInfo(".kodi")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        userdata = tarfile.TarInfo(".kodi/userdata")
        userdata.type = tarfile.DIRTYPE
        userdata.mode = 0o755
        archive.addfile(userdata)
        item = tarfile.TarInfo(path)
        item.type = member_type
        item.mode = 0o600
        item.size = len(content) if member_type == tarfile.REGTYPE else 0
        if member_type == tarfile.SYMTYPE:
            item.linkname = "/escape"
        archive.addfile(
            item,
            io.BytesIO(content) if member_type == tarfile.REGTYPE else None,
        )
    return output.getvalue()


def expected(
    content: bytes = b"settings", *, path: str = "userdata/guisettings.xml"
) -> tuple[SnapshotMemberRecord, ...]:
    return (
        SnapshotMemberRecord("userdata", "directory", 0, 0o755, None),
        SnapshotMemberRecord(
            path,
            "file",
            len(content),
            0o600,
            "sha256:" + hashlib.sha256(content).hexdigest(),
        ),
    )


def test_restored_readback_proves_inventory_mode_size_and_digest() -> None:
    assert (
        validate_restored_snapshot_source(
            source_chunks=[source_tar()], expected_members=expected()
        )
        == 0
    )


def test_restored_readback_rejects_digest_mode_and_missing_inventory() -> None:
    with pytest.raises(SnapshotStorageError):
        validate_restored_snapshot_source(
            source_chunks=[source_tar(content=b"changed")],
            expected_members=expected(),
        )
    wrong_mode = list(expected())
    wrong_mode[1] = SnapshotMemberRecord(
        "userdata/guisettings.xml",
        "file",
        8,
        0o644,
        wrong_mode[1].sha256,
    )
    with pytest.raises(SnapshotStorageError):
        validate_restored_snapshot_source(
            source_chunks=[source_tar()], expected_members=tuple(wrong_mode)
        )
    with pytest.raises(SnapshotStorageError):
        validate_restored_snapshot_source(
            source_chunks=[source_tar()], expected_members=expected()[:1]
        )


def test_restored_readback_rejects_unsafe_member() -> None:
    with pytest.raises(SnapshotStorageError, match="link"):
        validate_restored_snapshot_source(
            source_chunks=[source_tar(member_type=tarfile.SYMTYPE)],
            expected_members=expected(),
        )


def test_restored_readback_checks_recognized_sqlite_without_modification() -> None:
    content = b"not a sqlite database"
    with pytest.raises(SnapshotStorageError, match="integrity"):
        validate_restored_snapshot_source(
            source_chunks=[
                source_tar(
                    content=content,
                    path=".kodi/userdata/Database/MyVideos131.db",
                )
            ],
            expected_members=(
                SnapshotMemberRecord("userdata", "directory", 0, 0o755, None),
                SnapshotMemberRecord(
                    "userdata/Database/MyVideos131.db",
                    "file",
                    len(content),
                    0o600,
                    "sha256:" + hashlib.sha256(content).hexdigest(),
                ),
            ),
        )


@pytest.mark.parametrize("fault", [None, "file_uid", "directory_gid", "special_mode"])
def test_recovery_readback_requires_actual_file_and_directory_owner_and_safe_mode(
    fault: str | None,
) -> None:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, directory in (
            (".kodi", True),
            (".kodi/userdata", True),
            (".kodi/userdata/guisettings.xml", False),
        ):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
            info.uid = 0 if fault == "file_uid" and not directory else 1000
            info.gid = 0 if fault == "directory_gid" and directory else 1000
            info.mode = (
                0o755 if directory else (0o4600 if fault == "special_mode" else 0o600)
            )
            info.size = 0 if directory else len(b"settings")
            archive.addfile(info, None if directory else io.BytesIO(b"settings"))
    if fault is None:
        assert (
            validate_restored_snapshot_source(
                source_chunks=[output.getvalue()],
                expected_members=expected(),
                expected_owner=(1000, 1000),
            )
            == 0
        )
    else:
        with pytest.raises(SnapshotStorageError, match="ownership or safe mode"):
            validate_restored_snapshot_source(
                source_chunks=[output.getvalue()],
                expected_members=expected(),
                expected_owner=(1000, 1000),
            )

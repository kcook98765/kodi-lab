from __future__ import annotations

import os
import stat
import struct
import unicodedata
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kodi_lab.development.addon_zip import (
    MAX_CENTRAL_DIRECTORY_BYTES,
    MAX_COMPRESSED_BYTES,
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    AddonZipValidationError,
    ValidatedAddonZip,
    addon_zip_artifact_directory,
    create_upload_stage,
    finish_upload_stage,
    revalidate_addon_zip_artifact,
    validate_and_publish_addon_zip,
    write_upload_chunk,
)
from kodi_lab.domain import AddonZipArtifact

ADDON_ID = "plugin.video.ziptest"


def descriptor(
    *, addon_id: str = ADDON_ID, extension: str = "plugin", dependency: str = ""
) -> bytes:
    executable = (
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        if extension == "plugin"
        else f'<extension point="{extension}" library="service.py" />'
    )
    return (
        f'<addon id="{addon_id}" name="ZIP Test" version="1.2.3">'
        f"<requires>{dependency}</requires>{executable}"
        '<extension point="xbmc.addon.metadata" /></addon>'
    ).encode()


def make_zip(
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    addon_id: str = ADDON_ID,
    extension: str = "plugin",
    dependency: str = "",
    extra: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(
            f"{addon_id}/addon.xml",
            descriptor(addon_id=addon_id, extension=extension, dependency=dependency),
        )
        archive.writestr(f"{addon_id}/default.py", b"print('bounded')\n")
        for name, value in extra:
            archive.writestr(name, value)
    return stream.getvalue()


def publish(tmp_path: Path, payload: bytes) -> tuple[UUID, ValidatedAddonZip]:
    artifact_id = uuid4()
    fd, _path = create_upload_stage(tmp_path, artifact_id)
    try:
        total = write_upload_chunk(fd, payload, 0)
        finish_upload_stage(fd, total)
    finally:
        os.close(fd)
    return artifact_id, validate_and_publish_addon_zip(tmp_path, artifact_id)


def test_published_limits_are_exact() -> None:
    assert MAX_COMPRESSED_BYTES == 256 * 1024 * 1024
    assert MAX_UNCOMPRESSED_BYTES == 1024 * 1024 * 1024
    assert MAX_FILE_BYTES == 256 * 1024 * 1024
    assert MAX_ENTRIES == 50_000
    assert MAX_CENTRAL_DIRECTORY_BYTES == 16 * 1024 * 1024


def test_upload_stream_rejects_compressed_overflow(tmp_path: Path) -> None:
    artifact_id = uuid4()
    fd, _path = create_upload_stage(tmp_path, artifact_id)
    try:
        with pytest.raises(AddonZipValidationError) as caught:
            write_upload_chunk(fd, b"x", MAX_COMPRESSED_BYTES)
    finally:
        os.close(fd)
    assert caught.value.code == "addon_zip_compressed_size_exceeded"


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_valid_stored_and_deflate_publish_normalized_payload(
    tmp_path: Path, compression: int
) -> None:
    artifact_id, result = publish(tmp_path, make_zip(compression=compression))

    assert result.addon_id == ADDON_ID
    assert result.addon_version == "1.2.3"
    assert result.member_count == 2
    assert result.dependencies == ()
    root = addon_zip_artifact_directory(tmp_path, artifact_id)
    assert stat.S_IMODE(root.stat().st_mode) == 0o500
    assert stat.S_IMODE((root / "original.zip").stat().st_mode) == 0o400
    assert stat.S_IMODE((root / "payload/default.py").stat().st_mode) == 0o400


def test_dependency_declarations_are_normalized(tmp_path: Path) -> None:
    _artifact_id, result = publish(
        tmp_path,
        make_zip(dependency='<import addon="script.module.missing" optional="true" />'),
    )
    assert [(item.dependency_id, item.optional) for item in result.dependencies] == [
        ("script.module.missing", True)
    ]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ((("../escape", b"x"),), "addon_zip_unsafe_path"),
        ((("/absolute", b"x"),), "addon_zip_unsafe_path"),
        (((f"{ADDON_ID}\\bad", b"x"),), "addon_zip_unsafe_path"),
        (((f"{ADDON_ID}/.git/config", b"x"),), "addon_zip_vcs_path"),
        ((("extra/file", b"x"),), "addon_zip_layout"),
        ((("addon.xml", b"x"),), "addon_zip_layout"),
    ],
)
def test_unsafe_layout_and_names_are_rejected(
    tmp_path: Path, extra: tuple[tuple[str, bytes], ...], code: str
) -> None:
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, make_zip(extra=extra))
    assert caught.value.code == code


def test_non_nfc_name_is_rejected(tmp_path: Path) -> None:
    decomposed = unicodedata.normalize("NFD", "café")
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, make_zip(extra=((f"{ADDON_ID}/{decomposed}", b"x"),)))
    assert caught.value.code == "addon_zip_non_nfc_name"


def test_duplicate_and_case_collision_are_rejected(tmp_path: Path) -> None:
    with pytest.warns(UserWarning):
        duplicate = make_zip(extra=((f"{ADDON_ID}/default.py", b"other"),))
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, duplicate)
    assert caught.value.code == "addon_zip_duplicate_name"

    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, make_zip(extra=((f"{ADDON_ID}/DEFAULT.py", b"x"),)))
    assert caught.value.code == "addon_zip_case_collision"

    with pytest.raises(AddonZipValidationError) as caught:
        publish(
            tmp_path,
            make_zip(
                extra=(
                    (f"{ADDON_ID}/Dir/one", b"1"),
                    (f"{ADDON_ID}/dir/two", b"2"),
                )
            ),
        )
    assert caught.value.code == "addon_zip_case_collision"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        (f"{ADDON_ID}/bad\x01name", "addon_zip_control_name"),
        (f"{ADDON_ID}/{'a' * 256}", "addon_zip_component_bound"),
        (
            f"{ADDON_ID}/" + "/".join("d" for _ in range(32)),
            "addon_zip_path_bound",
        ),
    ],
)
def test_name_bounds_are_rejected(tmp_path: Path, name: str, code: str) -> None:
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, make_zip(extra=((name, b"x"),)))
    assert caught.value.code == code


def test_declared_size_and_count_limits_reject_without_large_archives(
    tmp_path: Path,
) -> None:
    base = bytearray(make_zip(compression=zipfile.ZIP_STORED))
    central = base.index(b"PK\x01\x02")
    local = base.index(b"PK\x03\x04")
    changed = bytearray(base)
    struct.pack_into("<I", changed, local + 22, MAX_FILE_BYTES + 1)
    struct.pack_into("<I", changed, central + 24, MAX_FILE_BYTES + 1)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_file_bound"

    changed = bytearray(base)
    eocd = len(changed) - 22
    struct.pack_into("<H", changed, eocd + 8, MAX_ENTRIES + 1)
    struct.pack_into("<H", changed, eocd + 10, MAX_ENTRIES + 1)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_central_bound"

    changed = bytearray(base)
    struct.pack_into("<I", changed, eocd + 12, MAX_CENTRAL_DIRECTORY_BYTES + 1)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_central_bound"


def test_compression_ratio_and_decompressed_length_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    base = bytearray(make_zip(compression=zipfile.ZIP_DEFLATED))
    central = base.index(b"PK\x01\x02")
    local = base.index(b"PK\x03\x04")
    changed = bytearray(base)
    declared = 2 * 1024 * 1024
    struct.pack_into("<I", changed, local + 22, declared)
    struct.pack_into("<I", changed, central + 24, declared)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_compression_ratio"

    changed = bytearray(base)
    current = struct.unpack_from("<I", changed, local + 22)[0]
    struct.pack_into("<I", changed, local + 22, current + 1)
    struct.pack_into("<I", changed, central + 24, current + 1)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_length_mismatch"


def test_extra_fields_are_rejected(tmp_path: Path) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        info = zipfile.ZipInfo(f"{ADDON_ID}/addon.xml")
        info.extra = b"\x01\x00\x00\x00"
        archive.writestr(info, descriptor())
        archive.writestr(f"{ADDON_ID}/default.py", b"pass\n")
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, stream.getvalue())
    assert caught.value.code == "addon_zip_extra_field"


@pytest.mark.parametrize(
    "file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK]
)
def test_links_and_special_unix_types_are_rejected(
    tmp_path: Path, file_type: int
) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(f"{ADDON_ID}/addon.xml", descriptor())
        info = zipfile.ZipInfo(f"{ADDON_ID}/default.py")
        info.create_system = 3
        info.external_attr = (file_type | 0o644) << 16
        archive.writestr(info, b"x")
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, stream.getvalue())
    assert caught.value.code == "addon_zip_unsupported_file_type"


def test_repository_and_service_descriptors_are_rejected(tmp_path: Path) -> None:
    for point in ("xbmc.addon.repository", "xbmc.service"):
        with pytest.raises(AddonZipValidationError) as caught:
            publish(tmp_path, make_zip(extension=point))
        assert caught.value.code == "addon_zip_unsupported_addon_type"


def test_directory_must_equal_descriptor_id(tmp_path: Path) -> None:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("plugin.video.other/addon.xml", descriptor())
        archive.writestr("plugin.video.other/default.py", b"pass\n")
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, stream.getvalue())
    assert caught.value.code == "addon_zip_id_directory_mismatch"


def test_encryption_data_descriptor_unsupported_compression_and_zip64_are_rejected(
    tmp_path: Path,
) -> None:
    base = bytearray(make_zip(compression=zipfile.ZIP_STORED))
    central = base.index(b"PK\x01\x02")
    local = base.index(b"PK\x03\x04")
    for mask, expected in (
        (1, "addon_zip_encrypted"),
        (8, "addon_zip_data_descriptor"),
    ):
        changed = bytearray(base)
        struct.pack_into("<H", changed, local + 6, mask)
        struct.pack_into("<H", changed, central + 8, mask)
        with pytest.raises(AddonZipValidationError) as caught:
            publish(tmp_path, bytes(changed))
        assert caught.value.code == expected
    changed = bytearray(base)
    struct.pack_into("<H", changed, local + 8, 99)
    struct.pack_into("<H", changed, central + 10, 99)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_compression_unsupported"
    changed = bytearray(base)
    struct.pack_into("<H", changed, central + 6, 45)
    with pytest.raises(AddonZipValidationError) as caught:
        publish(tmp_path, bytes(changed))
    assert caught.value.code == "addon_zip_zip64"


def test_multidisk_comment_local_mismatch_overlap_and_crc_are_rejected(
    tmp_path: Path,
) -> None:
    base = bytearray(make_zip(compression=zipfile.ZIP_STORED))
    eocd = len(base) - 22
    central = base.index(b"PK\x01\x02")
    local = base.index(b"PK\x03\x04")
    cases: list[tuple[bytearray, str]] = []
    changed = bytearray(base)
    struct.pack_into("<H", changed, eocd + 4, 1)
    cases.append((changed, "addon_zip_multidisk"))
    changed = bytearray(base)
    struct.pack_into("<H", changed, eocd + 20, 1)
    cases.append((changed, "addon_zip_comment_forbidden"))
    changed = bytearray(base)
    struct.pack_into("<I", changed, local + 14, 0)
    cases.append((changed, "addon_zip_header_mismatch"))
    changed = bytearray(base)
    second = changed.index(b"PK\x01\x02", central + 4)
    second_local = struct.unpack_from("<I", changed, second + 42)[0]
    first_name_length = struct.unpack_from("<H", changed, local + 26)[0]
    first_data = local + 30 + first_name_length
    overlapping_size = second_local - first_data + 1
    struct.pack_into("<I", changed, local + 18, overlapping_size)
    struct.pack_into("<I", changed, local + 22, overlapping_size)
    struct.pack_into("<I", changed, central + 20, overlapping_size)
    struct.pack_into("<I", changed, central + 24, overlapping_size)
    cases.append((changed, "addon_zip_overlapping_member"))
    changed = bytearray(base)
    name_length = struct.unpack_from("<H", changed, local + 26)[0]
    changed[local + 30 + name_length] ^= 1
    cases.append((changed, "addon_zip_crc_mismatch"))
    for payload, code in cases:
        with pytest.raises(AddonZipValidationError) as caught:
            publish(tmp_path, bytes(payload))
        assert caught.value.code == code


def test_storage_revalidation_detects_drift(tmp_path: Path) -> None:
    artifact_id, result = publish(tmp_path, make_zip())
    artifact = AddonZipArtifact(
        id=artifact_id,
        logical_label="Safe label",
        sha256_digest=result.sha256_digest,
        compressed_size=result.compressed_size,
        total_uncompressed_bytes=result.total_uncompressed_bytes,
        member_count=result.member_count,
        addon_id=result.addon_id,
        addon_name=result.addon_name,
        addon_version=result.addon_version,
        extension_classification=result.extension_classification,
        dependencies=result.dependencies,
        payload_manifest_digest=result.payload_manifest_digest,
        imported_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    revalidate_addon_zip_artifact(tmp_path, artifact)
    target = addon_zip_artifact_directory(tmp_path, artifact_id) / "payload/default.py"
    target.chmod(0o600)
    target.write_bytes(b"drift")
    target.chmod(0o400)
    with pytest.raises(AddonZipValidationError) as caught:
        revalidate_addon_zip_artifact(tmp_path, artifact)
    assert caught.value.code == "addon_zip_artifact_drift"

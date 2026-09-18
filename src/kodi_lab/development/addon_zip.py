"""Closed uploaded-ZIP validation and immutable private artifact storage."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import stat
import struct
import unicodedata
import zlib
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from uuid import UUID

from kodi_lab.development.sources import (
    MAX_ADDON_XML_BYTES,
    DevelopmentSourceValidationError,
    copied_video_plugin_library,
    parse_captured_addon_xml,
)
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipDependency,
    AddonZipExtensionClassification,
)

MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_REGULAR_FILES = 25_000
MAX_ENTRIES = 50_000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PATH_BYTES = 512
MAX_COMPONENT_BYTES = 255
MAX_DEPTH = 32
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MEMBER_RATIO_LIMIT = 200
MEMBER_RATIO_FLOOR = 1024 * 1024
TOTAL_RATIO_LIMIT = 100
TOTAL_RATIO_FLOOR = 4 * 1024 * 1024

_EOCD = struct.Struct("<4s4H2IH")
_CENTRAL = struct.Struct("<4s6H3I5H2I")
_LOCAL = struct.Struct("<4s5H3I2H")
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_UTF8_FLAG = 0x0800
_DATA_DESCRIPTOR_FLAG = 0x0008
_ENCRYPTED_FLAGS = 0x0041
_ALLOWED_FLAGS = _UTF8_FLAG | 0x0006
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_VCS_COMPONENTS = frozenset({".git", ".hg", ".svn"})


class AddonZipValidationError(ValueError):
    """One bounded, safe uploaded-ZIP rejection."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


@dataclass(frozen=True, slots=True)
class ZipMember:
    path: str
    is_directory: bool
    compression: int
    flags: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_offset: int
    data_offset: int
    mode: int


@dataclass(frozen=True, slots=True)
class PayloadManifestEntry:
    path: str
    entry_type: Literal["directory", "file"]
    mode: int
    size: int
    sha256: str | None

    def canonical(self) -> dict[str, object]:
        value: dict[str, object] = {
            "mode": self.mode,
            "path": self.path,
            "size": self.size,
            "type": self.entry_type,
        }
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        return value


@dataclass(frozen=True, slots=True)
class ValidatedAddonZip:
    sha256_digest: str
    compressed_size: int
    total_uncompressed_bytes: int
    member_count: int
    addon_id: str
    addon_name: str
    addon_version: str
    extension_classification: AddonZipExtensionClassification
    dependencies: tuple[AddonZipDependency, ...]
    payload_manifest_digest: str
    entries: tuple[PayloadManifestEntry, ...]
    manifest_bytes: bytes


def addon_zip_root(data_root: Path) -> Path:
    return data_root / "addon-zips"


def addon_zip_staging_root(data_root: Path) -> Path:
    return addon_zip_root(data_root) / "staging"


def addon_zip_artifacts_root(data_root: Path) -> Path:
    return addon_zip_root(data_root) / "artifacts"


def addon_zip_artifact_directory(data_root: Path, artifact_id: UUID) -> Path:
    return addon_zip_artifacts_root(data_root) / str(artifact_id)


def addon_zip_upload_path(data_root: Path, artifact_id: UUID) -> Path:
    return addon_zip_staging_root(data_root) / f"{artifact_id}.zip"


def _ensure_private_directory(data_root: Path, *components: str) -> None:
    """Create/open a fixed private subtree through no-follow directory handles."""

    try:
        current_fd = os.open(data_root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise AddonZipValidationError(
            "addon_zip_storage_unsafe", "Private ZIP storage is unavailable."
        ) from error
    try:
        for component in components:
            if component in {"", ".", ".."} or "/" in component:
                raise AddonZipValidationError(
                    "addon_zip_storage_unsafe", "Private ZIP storage is unavailable."
                )
            with suppress(FileExistsError):
                os.mkdir(component, 0o700, dir_fd=current_fd)
            child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            metadata = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                os.close(child_fd)
                raise AddonZipValidationError(
                    "addon_zip_storage_unsafe", "Private ZIP storage is unavailable."
                )
            os.close(current_fd)
            current_fd = child_fd
    except OSError as error:
        raise AddonZipValidationError(
            "addon_zip_storage_unsafe", "Private ZIP storage is unavailable."
        ) from error
    finally:
        os.close(current_fd)


def _write_all(fd: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short payload write")
        view = view[written:]


def create_upload_stage(data_root: Path, artifact_id: UUID) -> tuple[int, Path]:
    """Create one server-UUID named, no-follow, exclusive upload file."""

    _ensure_private_directory(data_root, "addon-zips", "staging")
    path = addon_zip_upload_path(data_root, artifact_id)
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as error:
        raise AddonZipValidationError(
            "addon_zip_staging_unavailable", "Upload staging is unavailable."
        ) from error
    return fd, path


def write_upload_chunk(fd: int, chunk: bytes, total: int) -> int:
    next_total = total + len(chunk)
    if next_total > MAX_COMPRESSED_BYTES:
        raise AddonZipValidationError(
            "addon_zip_compressed_size_exceeded",
            "Uploaded ZIP exceeds the 256 MiB compressed limit.",
        )
    try:
        _write_all(fd, chunk)
    except OSError as error:
        raise AddonZipValidationError(
            "addon_zip_staging_failed", "Uploaded ZIP could not be staged."
        ) from error
    return next_total


def finish_upload_stage(fd: int, total: int) -> None:
    if total <= 0:
        raise AddonZipValidationError("addon_zip_empty", "Uploaded ZIP is empty.")
    os.fsync(fd)


def remove_upload_stage(data_root: Path, artifact_id: UUID) -> None:
    """Remove only exact server-derived failed staging identities."""

    staging = addon_zip_staging_root(data_root)
    for path in (
        addon_zip_upload_path(data_root, artifact_id),
        staging / f"{artifact_id}.publishing",
    ):
        if path.parent != staging:
            continue
        with suppress(FileNotFoundError, OSError):
            if path.is_dir() and not path.is_symlink():
                for root, directories, files in os.walk(path, topdown=False):
                    for name in files:
                        os.chmod(Path(root) / name, 0o600, follow_symlinks=False)
                        (Path(root) / name).unlink()
                    for name in directories:
                        os.chmod(Path(root) / name, 0o700, follow_symlinks=False)
                        (Path(root) / name).rmdir()
                os.chmod(path, 0o700, follow_symlinks=False)
                path.rmdir()
            else:
                path.unlink()


def discard_unaccepted_addon_zip_artifact(data_root: Path, artifact_id: UUID) -> None:
    """Remove one exact server-derived publication after catalog failure."""

    directory = addon_zip_artifact_directory(data_root, artifact_id)
    if directory.parent != addon_zip_artifacts_root(data_root):
        return
    with suppress(FileNotFoundError, OSError):
        for root, directories, files in os.walk(directory, topdown=False):
            os.chmod(root, 0o700, follow_symlinks=False)
            for name in files:
                os.chmod(Path(root) / name, 0o600, follow_symlinks=False)
                (Path(root) / name).unlink()
            for name in directories:
                os.chmod(Path(root) / name, 0o700, follow_symlinks=False)
                (Path(root) / name).rmdir()
        os.chmod(directory, 0o700, follow_symlinks=False)
        directory.rmdir()


def _decode_name(raw: bytes, flags: int) -> str:
    try:
        if flags & _UTF8_FLAG:
            value = raw.decode("utf-8", "strict")
        else:
            value = raw.decode("ascii", "strict")
    except UnicodeDecodeError as error:
        raise AddonZipValidationError(
            "addon_zip_name_encoding", "ZIP member names must be UTF-8 or ASCII."
        ) from error
    if unicodedata.normalize("NFC", value) != value:
        raise AddonZipValidationError(
            "addon_zip_non_nfc_name", "ZIP member names must use NFC Unicode."
        )
    return value


def _validate_member_path(value: str, *, directory: bool) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise AddonZipValidationError(
            "addon_zip_unsafe_path", "ZIP member path is unsafe."
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise AddonZipValidationError(
            "addon_zip_control_name", "ZIP member names must not contain controls."
        )
    raw = value[:-1] if directory and value.endswith("/") else value
    pure = PurePosixPath(raw)
    parts = pure.parts
    if (
        pure.is_absolute()
        or not parts
        or raw.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise AddonZipValidationError(
            "addon_zip_unsafe_path", "ZIP member path is absolute or traversing."
        )
    if len(parts) > MAX_DEPTH or len(raw.encode("utf-8")) > MAX_PATH_BYTES:
        raise AddonZipValidationError(
            "addon_zip_path_bound", "ZIP member path exceeds fixed bounds."
        )
    if any(len(part.encode("utf-8")) > MAX_COMPONENT_BYTES for part in parts):
        raise AddonZipValidationError(
            "addon_zip_component_bound", "ZIP member component exceeds 255 bytes."
        )
    if any(part.casefold() in _VCS_COMPONENTS for part in parts):
        raise AddonZipValidationError(
            "addon_zip_vcs_path", "ZIP packages must not contain VCS metadata."
        )
    return raw


def _unix_mode(create_system: int, external_attributes: int, directory: bool) -> int:
    raw_mode = (external_attributes >> 16) & 0xFFFF if create_system == 3 else 0
    file_type = stat.S_IFMT(raw_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise AddonZipValidationError(
            "addon_zip_unsupported_file_type",
            "ZIP packages may contain only regular files and directories.",
        )
    if directory and file_type not in {0, stat.S_IFDIR}:
        raise AddonZipValidationError(
            "addon_zip_type_mismatch", "ZIP directory metadata is inconsistent."
        )
    if not directory and file_type == stat.S_IFDIR:
        raise AddonZipValidationError(
            "addon_zip_type_mismatch", "ZIP file metadata is inconsistent."
        )
    return 0o755 if directory else 0o644 | (raw_mode & 0o111)


def _parse_members(stream: BinaryIO, file_size: int) -> tuple[ZipMember, ...]:
    if file_size < _EOCD.size or file_size > MAX_COMPRESSED_BYTES:
        raise AddonZipValidationError(
            "addon_zip_size_invalid", "ZIP compressed size is outside fixed bounds."
        )
    stream.seek(file_size - _EOCD.size)
    values = _EOCD.unpack(stream.read(_EOCD.size))
    (
        signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        comment_length,
    ) = values
    if signature != _EOCD_SIGNATURE:
        raise AddonZipValidationError(
            "addon_zip_eocd_invalid", "ZIP end record is missing or ambiguous."
        )
    if comment_length != 0:
        raise AddonZipValidationError(
            "addon_zip_comment_forbidden", "ZIP comments are not supported."
        )
    if disk_number or central_disk or disk_entries != total_entries:
        raise AddonZipValidationError(
            "addon_zip_multidisk", "Multi-disk ZIP files are not supported."
        )
    if (
        total_entries in {0, 0xFFFF}
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise AddonZipValidationError(
            "addon_zip_zip64", "ZIP64 packages are not supported."
        )
    if total_entries > MAX_ENTRIES or central_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise AddonZipValidationError(
            "addon_zip_central_bound", "ZIP central directory exceeds fixed bounds."
        )
    if central_offset + central_size != file_size - _EOCD.size:
        raise AddonZipValidationError(
            "addon_zip_central_offset", "ZIP central directory offsets are invalid."
        )
    stream.seek(central_offset)
    central = stream.read(central_size)
    if len(central) != central_size:
        raise AddonZipValidationError(
            "addon_zip_truncated", "ZIP central directory is truncated."
        )
    members: list[ZipMember] = []
    position = 0
    seen_exact: set[str] = set()
    seen_folded: set[str] = set()
    regular_files = 0
    total_uncompressed = 0
    total_compressed = 0
    local_regions: list[tuple[int, int]] = []
    for _index in range(total_entries):
        if position + _CENTRAL.size > len(central):
            raise AddonZipValidationError(
                "addon_zip_central_invalid", "ZIP central directory is malformed."
            )
        values = _CENTRAL.unpack_from(central, position)
        position += _CENTRAL.size
        (
            signature,
            version_made,
            version_needed,
            flags,
            compression,
            _mtime,
            _mdate,
            crc32,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            entry_comment_length,
            disk_start,
            _internal_attributes,
            external_attributes,
            local_offset,
        ) = values
        if signature != _CENTRAL_SIGNATURE:
            raise AddonZipValidationError(
                "addon_zip_central_invalid", "ZIP central directory is malformed."
            )
        if (
            any(
                value in {0xFFFF, 0xFFFFFFFF}
                for value in (compressed_size, uncompressed_size, local_offset)
            )
            or version_needed >= 45
        ):
            raise AddonZipValidationError(
                "addon_zip_zip64", "ZIP64 packages are not supported."
            )
        if disk_start != 0:
            raise AddonZipValidationError(
                "addon_zip_multidisk", "Multi-disk ZIP files are not supported."
            )
        if extra_length != 0:
            raise AddonZipValidationError(
                "addon_zip_extra_field", "ZIP extra fields are not supported."
            )
        if entry_comment_length != 0:
            raise AddonZipValidationError(
                "addon_zip_comment_forbidden", "ZIP comments are not supported."
            )
        if flags & _ENCRYPTED_FLAGS:
            raise AddonZipValidationError(
                "addon_zip_encrypted", "Encrypted ZIP entries are not supported."
            )
        if flags & _DATA_DESCRIPTOR_FLAG:
            raise AddonZipValidationError(
                "addon_zip_data_descriptor", "ZIP data descriptors are not supported."
            )
        if flags & ~_ALLOWED_FLAGS or compression not in {0, 8}:
            raise AddonZipValidationError(
                "addon_zip_compression_unsupported",
                "Only stored and deflate ZIP entries are supported.",
            )
        if compression == 0 and (flags & 0x0006):
            raise AddonZipValidationError(
                "addon_zip_flags_unsupported", "ZIP entry flags are unsupported."
            )
        end = position + name_length
        if name_length == 0 or end > len(central):
            raise AddonZipValidationError(
                "addon_zip_name_invalid", "ZIP member name is missing."
            )
        raw_name = central[position:end]
        position = end
        decoded = _decode_name(raw_name, flags)
        directory = decoded.endswith("/")
        path = _validate_member_path(decoded, directory=directory)
        folded = path.casefold()
        if path in seen_exact:
            raise AddonZipValidationError(
                "addon_zip_duplicate_name", "ZIP member names must be unique."
            )
        if folded in seen_folded:
            raise AddonZipValidationError(
                "addon_zip_case_collision",
                "ZIP member names must not collide by Unicode case.",
            )
        seen_exact.add(path)
        seen_folded.add(folded)
        if directory:
            if compressed_size or uncompressed_size:
                raise AddonZipValidationError(
                    "addon_zip_directory_data", "ZIP directories must not contain data."
                )
        else:
            regular_files += 1
            total_uncompressed += uncompressed_size
            total_compressed += compressed_size
            if regular_files > MAX_REGULAR_FILES or uncompressed_size > MAX_FILE_BYTES:
                raise AddonZipValidationError(
                    "addon_zip_file_bound", "ZIP file bounds were exceeded."
                )
            if uncompressed_size > max(
                MEMBER_RATIO_FLOOR, compressed_size * MEMBER_RATIO_LIMIT
            ):
                raise AddonZipValidationError(
                    "addon_zip_compression_ratio",
                    "ZIP compression ratio exceeds the fixed member limit.",
                )
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise AddonZipValidationError(
                "addon_zip_total_uncompressed", "ZIP uncompressed size exceeds 1 GiB."
            )
        stream.seek(local_offset)
        local_raw = stream.read(_LOCAL.size)
        if len(local_raw) != _LOCAL.size:
            raise AddonZipValidationError(
                "addon_zip_local_header", "ZIP local header is truncated."
            )
        local = _LOCAL.unpack(local_raw)
        (
            local_signature,
            local_version,
            local_flags,
            local_compression,
            local_mtime,
            local_mdate,
            local_crc32,
            local_compressed_size,
            local_uncompressed_size,
            local_name_length,
            local_extra_length,
        ) = local
        local_name = stream.read(local_name_length)
        if (
            local_signature != _LOCAL_SIGNATURE
            or local_version != version_needed
            or local_flags != flags
            or local_compression != compression
            or local_mtime != _mtime
            or local_mdate != _mdate
            or local_crc32 != crc32
            or local_compressed_size != compressed_size
            or local_uncompressed_size != uncompressed_size
            or local_name != raw_name
            or local_extra_length != 0
        ):
            raise AddonZipValidationError(
                "addon_zip_header_mismatch",
                "ZIP local and central headers are inconsistent.",
            )
        data_offset = local_offset + _LOCAL.size + local_name_length
        region_end = data_offset + compressed_size
        if local_offset >= central_offset or region_end > central_offset:
            raise AddonZipValidationError(
                "addon_zip_member_offset", "ZIP member offsets are invalid."
            )
        local_regions.append((local_offset, region_end))
        create_system = version_made >> 8
        members.append(
            ZipMember(
                path=path,
                is_directory=directory,
                compression=compression,
                flags=flags,
                crc32=crc32,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                local_offset=local_offset,
                data_offset=data_offset,
                mode=_unix_mode(create_system, external_attributes, directory),
            )
        )
    if position != len(central):
        raise AddonZipValidationError(
            "addon_zip_central_invalid", "ZIP central directory has unexpected bytes."
        )
    ordered_regions = sorted(local_regions)
    if any(
        left_end > right_start
        for (_, left_end), (right_start, _) in pairwise(ordered_regions)
    ):
        raise AddonZipValidationError(
            "addon_zip_overlapping_member", "ZIP member data overlaps."
        )
    if total_uncompressed > max(
        TOTAL_RATIO_FLOOR, total_compressed * TOTAL_RATIO_LIMIT
    ):
        raise AddonZipValidationError(
            "addon_zip_total_compression_ratio",
            "ZIP aggregate compression ratio exceeds the fixed limit.",
        )
    return tuple(members)


def _validate_logical_tree(members: tuple[ZipMember, ...]) -> None:
    """Reject implicit directory type and Unicode-case collisions before writes."""

    paths: dict[str, tuple[str, Literal["directory", "file"]]] = {}
    for member in members:
        components = member.path.split("/")
        for index in range(1, len(components) + 1):
            path = "/".join(components[:index])
            entry_type: Literal["directory", "file"] = (
                "directory"
                if index < len(components) or member.is_directory
                else "file"
            )
            folded = path.casefold()
            existing = paths.get(folded)
            if existing is not None and existing[0] != path:
                raise AddonZipValidationError(
                    "addon_zip_case_collision",
                    "ZIP member names must not collide by Unicode case.",
                )
            if existing is not None and existing[1] != entry_type:
                raise AddonZipValidationError(
                    "addon_zip_type_collision",
                    "ZIP file and directory paths collide.",
                )
            paths[folded] = (path, entry_type)


def _copy_member(
    stream: BinaryIO, member: ZipMember, destination_fd: int
) -> tuple[str, bytes | None]:
    stream.seek(member.data_offset)
    digest = hashlib.sha256()
    crc = 0
    observed = 0
    descriptor = bytearray() if member.path.endswith("/addon.xml") else None
    remaining = member.compressed_size
    decompressor = zlib.decompressobj(-15) if member.compression == 8 else None
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        if not chunk:
            raise AddonZipValidationError(
                "addon_zip_truncated", "ZIP member data is truncated."
            )
        remaining -= len(chunk)
        try:
            output = chunk if decompressor is None else decompressor.decompress(chunk)
        except zlib.error as error:
            raise AddonZipValidationError(
                "addon_zip_deflate_invalid", "ZIP deflate stream is invalid."
            ) from error
        if output:
            observed += len(output)
            if observed > member.uncompressed_size:
                raise AddonZipValidationError(
                    "addon_zip_length_mismatch", "ZIP member length is inconsistent."
                )
            crc = binascii.crc32(output, crc)
            digest.update(output)
            if descriptor is not None:
                descriptor.extend(output)
                if len(descriptor) > MAX_ADDON_XML_BYTES:
                    raise AddonZipValidationError(
                        "addon_zip_addon_xml_bound", "addon.xml exceeds 1 MiB."
                    )
            _write_all(destination_fd, output)
    if decompressor is not None:
        try:
            tail = decompressor.flush()
        except zlib.error as error:
            raise AddonZipValidationError(
                "addon_zip_deflate_invalid", "ZIP deflate stream is invalid."
            ) from error
        if tail:
            observed += len(tail)
            if observed > member.uncompressed_size:
                raise AddonZipValidationError(
                    "addon_zip_length_mismatch", "ZIP member length is inconsistent."
                )
            crc = binascii.crc32(tail, crc)
            digest.update(tail)
            if descriptor is not None:
                descriptor.extend(tail)
                if len(descriptor) > MAX_ADDON_XML_BYTES:
                    raise AddonZipValidationError(
                        "addon_zip_addon_xml_bound", "addon.xml exceeds 1 MiB."
                    )
            _write_all(destination_fd, tail)
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise AddonZipValidationError(
                "addon_zip_deflate_invalid", "ZIP deflate stream is ambiguous."
            )
    if observed != member.uncompressed_size:
        raise AddonZipValidationError(
            "addon_zip_length_mismatch", "ZIP member length is inconsistent."
        )
    if (crc & 0xFFFFFFFF) != member.crc32:
        raise AddonZipValidationError(
            "addon_zip_crc_mismatch", "ZIP member CRC does not match."
        )
    return digest.hexdigest(), None if descriptor is None else bytes(descriptor)


def _canonical_manifest(entries: tuple[PayloadManifestEntry, ...]) -> bytes:
    document = {
        "format": "kodi-lab-addon-zip-payload-v1",
        "mode_policy": "directories-0755-files-0644-plus-source-execute",
        "members": [entry.canonical() for entry in entries],
    }
    value = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(value) > MAX_MANIFEST_BYTES:
        raise AddonZipValidationError(
            "addon_zip_manifest_bound", "Canonical payload manifest exceeds 16 MiB."
        )
    return value


def _safe_payload_parent(payload_root: Path, relative: str) -> Path:
    path = payload_root.joinpath(*relative.split("/"))
    if path == payload_root or payload_root not in path.parents:
        raise AddonZipValidationError(
            "addon_zip_unsafe_path", "ZIP member path is unsafe."
        )
    return path


def validate_and_publish_addon_zip(
    data_root: Path, artifact_id: UUID
) -> ValidatedAddonZip:
    """Validate every archive structure and byte before immutable publication."""

    upload = addon_zip_upload_path(data_root, artifact_id)
    staging_root = addon_zip_staging_root(data_root)
    publishing = staging_root / f"{artifact_id}.publishing"
    final = addon_zip_artifact_directory(data_root, artifact_id)
    if final.exists():
        raise AddonZipValidationError(
            "addon_zip_artifact_exists", "Artifact identity already exists."
        )
    _ensure_private_directory(data_root, "addon-zips", "artifacts")
    try:
        publishing.mkdir(mode=0o700)
        payload_root = publishing / "payload"
        payload_root.mkdir(mode=0o700)
        upload_fd = os.open(upload, _FILE_READ_FLAGS)
        with os.fdopen(upload_fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
            ):
                raise AddonZipValidationError(
                    "addon_zip_staging_unsafe", "Upload staging identity is unsafe."
                )
            file_size = metadata.st_size
            members = _parse_members(stream, file_size)
            _validate_logical_tree(members)
            top_levels = {member.path.split("/", 1)[0] for member in members}
            if len(top_levels) != 1:
                raise AddonZipValidationError(
                    "addon_zip_layout",
                    "ZIP must contain exactly one top-level add-on directory.",
                )
            top_level = next(iter(top_levels))
            if not any(
                member.path == f"{top_level}/addon.xml" and not member.is_directory
                for member in members
            ):
                raise AddonZipValidationError(
                    "addon_zip_addon_xml_missing",
                    "ZIP must contain top-level-directory/addon.xml.",
                )
            entries_by_path: dict[str, PayloadManifestEntry] = {}
            descriptor: bytes | None = None
            for member in members:
                relative = (
                    member.path[len(top_level) + 1 :]
                    if member.path != top_level
                    else ""
                )
                if not relative:
                    if not member.is_directory:
                        raise AddonZipValidationError(
                            "addon_zip_layout",
                            "Top-level add-on entry must be a directory.",
                        )
                    continue
                components = relative.split("/")
                for depth in range(1, len(components)):
                    implicit = "/".join(components[:depth])
                    existing = entries_by_path.get(implicit)
                    directory_entry = PayloadManifestEntry(
                        implicit, "directory", 0o755, 0, None
                    )
                    if existing is not None and existing.entry_type != "directory":
                        raise AddonZipValidationError(
                            "addon_zip_type_collision",
                            "ZIP file and directory paths collide.",
                        )
                    entries_by_path.setdefault(implicit, directory_entry)
                    _safe_payload_parent(payload_root, implicit).mkdir(
                        mode=0o700, parents=True, exist_ok=True
                    )
                target = _safe_payload_parent(payload_root, relative)
                if member.is_directory:
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    entry = PayloadManifestEntry(relative, "directory", 0o755, 0, None)
                    existing = entries_by_path.get(relative)
                    if existing is not None and existing.entry_type != "directory":
                        raise AddonZipValidationError(
                            "addon_zip_type_collision",
                            "ZIP file and directory paths collide.",
                        )
                    entries_by_path[relative] = entry
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    digest, captured_descriptor = _copy_member(stream, member, fd)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.chmod(target, 0o400, follow_symlinks=False)
                entries_by_path[relative] = PayloadManifestEntry(
                    relative, "file", member.mode, member.uncompressed_size, digest
                )
                if captured_descriptor is not None:
                    descriptor = captured_descriptor
            if descriptor is None:
                raise AddonZipValidationError(
                    "addon_zip_addon_xml_missing", "ZIP addon.xml is missing."
                )
            try:
                observation = parse_captured_addon_xml(top_level, descriptor)
            except DevelopmentSourceValidationError as error:
                raise AddonZipValidationError(
                    "addon_zip_descriptor_invalid", "ZIP addon.xml is invalid."
                ) from error
            if observation.addon_id != top_level:
                raise AddonZipValidationError(
                    "addon_zip_id_directory_mismatch",
                    "Top-level directory must exactly equal the add-on ID.",
                )
            library = copied_video_plugin_library(observation)
            library_entry = None if library is None else entries_by_path.get(library)
            if (
                library is None
                or library_entry is None
                or library_entry.entry_type != "file"
            ):
                raise AddonZipValidationError(
                    "addon_zip_unsupported_addon_type",
                    "Only a non-service Python video plugin is supported.",
                )
            ordered = tuple(
                sorted(
                    entries_by_path.values(), key=lambda item: item.path.encode("utf-8")
                )
            )
            manifest_bytes = _canonical_manifest(ordered)
            manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            manifest_path = publishing / "manifest.json"
            manifest_fd = os.open(
                manifest_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                _write_all(manifest_fd, manifest_bytes)
                os.fsync(manifest_fd)
            finally:
                os.close(manifest_fd)
            os.chmod(manifest_path, 0o400, follow_symlinks=False)
            stream.seek(0)
            archive_digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                archive_digest.update(chunk)
        original = publishing / "original.zip"
        os.replace(upload, original)
        os.chmod(original, 0o400, follow_symlinks=False)
        for directory, subdirectories, _files in os.walk(
            payload_root, topdown=False, followlinks=False
        ):
            for name in subdirectories:
                os.chmod(Path(directory) / name, 0o500, follow_symlinks=False)
            os.chmod(directory, 0o500, follow_symlinks=False)
        directory_fd = os.open(publishing, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(publishing, final)
        os.chmod(final, 0o500, follow_symlinks=False)
        artifacts_fd = os.open(addon_zip_artifacts_root(data_root), _DIRECTORY_FLAGS)
        try:
            os.fsync(artifacts_fd)
        finally:
            os.close(artifacts_fd)
        return ValidatedAddonZip(
            sha256_digest=f"sha256:{archive_digest.hexdigest()}",
            compressed_size=file_size,
            total_uncompressed_bytes=sum(
                member.uncompressed_size
                for member in members
                if not member.is_directory
            ),
            member_count=len(members),
            addon_id=observation.addon_id,
            addon_name=observation.observed_name,
            addon_version=observation.observed_version,
            extension_classification=AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN,
            dependencies=tuple(
                AddonZipDependency(
                    item.dependency_id, item.version, item.minversion, item.optional
                )
                for item in observation.dependencies
            ),
            payload_manifest_digest=manifest_digest,
            entries=ordered,
            manifest_bytes=manifest_bytes,
        )
    except AddonZipValidationError:
        remove_upload_stage(data_root, artifact_id)
        raise
    except (OSError, ValueError, zlib.error) as error:
        remove_upload_stage(data_root, artifact_id)
        raise AddonZipValidationError(
            "addon_zip_validation_failed", "Uploaded ZIP could not be validated safely."
        ) from error


def revalidate_addon_zip_artifact(
    data_root: Path, artifact: AddonZipArtifact
) -> ValidatedAddonZip:
    """Reprove immutable original, manifest, and normalized payload before install."""

    directory = addon_zip_artifact_directory(data_root, artifact.id)
    original = directory / "original.zip"
    manifest_path = directory / "manifest.json"
    payload_root = directory / "payload"
    try:
        for path, expected_mode, expected_directory in (
            (directory, 0o500, True),
            (payload_root, 0o500, True),
            (original, 0o400, False),
            (manifest_path, 0o400, False),
        ):
            metadata = path.stat(follow_symlinks=False)
            if (
                stat.S_ISDIR(metadata.st_mode) != expected_directory
                or stat.S_IMODE(metadata.st_mode) != expected_mode
                or (
                    not expected_directory
                    and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1)
                )
            ):
                raise OSError("artifact storage identity changed")
        manifest_bytes = manifest_path.read_bytes()
        if (
            len(manifest_bytes) > MAX_MANIFEST_BYTES
            or f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            != artifact.payload_manifest_digest
        ):
            raise OSError("manifest identity changed")
        document = json.loads(manifest_bytes)
        if not isinstance(document, dict) or set(document) != {
            "format",
            "mode_policy",
            "members",
        }:
            raise OSError("manifest form changed")
        raw_members = document.get("members")
        if (
            document.get("format") != "kodi-lab-addon-zip-payload-v1"
            or document.get("mode_policy")
            != "directories-0755-files-0644-plus-source-execute"
            or not isinstance(raw_members, list)
            or len(raw_members) > MAX_ENTRIES
        ):
            raise OSError("manifest policy changed")
        entries: list[PayloadManifestEntry] = []
        expected_paths: set[str] = set()
        for raw in raw_members:
            if not isinstance(raw, dict) or not set(raw) <= {
                "path",
                "type",
                "mode",
                "size",
                "sha256",
            }:
                raise OSError("manifest member changed")
            member_path_text = raw.get("path")
            entry_type = raw.get("type")
            mode = raw.get("mode")
            size = raw.get("size")
            digest_value = raw.get("sha256")
            if (
                not isinstance(member_path_text, str)
                or entry_type not in {"file", "directory"}
                or not isinstance(mode, int)
                or not isinstance(size, int)
                or isinstance(mode, bool)
                or isinstance(size, bool)
            ):
                raise OSError("manifest member invalid")
            normalized_member_path = _validate_member_path(
                member_path_text, directory=False
            )
            if (
                normalized_member_path != member_path_text
                or member_path_text in expected_paths
            ):
                raise OSError("manifest member path changed")
            target = _safe_payload_parent(payload_root, member_path_text)
            metadata = target.stat(follow_symlinks=False)
            if entry_type == "directory":
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o500
                    or size != 0
                    or digest_value is not None
                ):
                    raise OSError("payload directory changed")
                digest_text = None
            else:
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o400
                    or metadata.st_size != size
                    or not isinstance(digest_value, str)
                ):
                    raise OSError("payload file changed")
                file_digest = hashlib.sha256()
                fd = os.open(target, _FILE_READ_FLAGS)
                try:
                    while chunk := os.read(fd, 1024 * 1024):
                        file_digest.update(chunk)
                finally:
                    os.close(fd)
                digest_text = file_digest.hexdigest()
                if digest_text != digest_value:
                    raise OSError("payload digest changed")
            expected_paths.add(member_path_text)
            entries.append(
                PayloadManifestEntry(
                    member_path_text, entry_type, mode, size, digest_text
                )
            )
        observed_paths: set[str] = set()
        for root, directories, files in os.walk(payload_root, followlinks=False):
            relative_root = Path(root).relative_to(payload_root)
            for name in directories + files:
                observed_paths.add((relative_root / name).as_posix())
        if observed_paths != expected_paths:
            raise OSError("payload inventory changed")
        original_digest = hashlib.sha256()
        original_fd = os.open(original, _FILE_READ_FLAGS)
        try:
            original_metadata = os.fstat(original_fd)
            if (
                not stat.S_ISREG(original_metadata.st_mode)
                or original_metadata.st_nlink != 1
                or stat.S_IMODE(original_metadata.st_mode) != 0o400
                or original_metadata.st_size != artifact.compressed_size
            ):
                raise OSError("original artifact identity changed")
            while chunk := os.read(original_fd, 1024 * 1024):
                original_digest.update(chunk)
        finally:
            os.close(original_fd)
        if f"sha256:{original_digest.hexdigest()}" != artifact.sha256_digest:
            raise OSError("original artifact changed")
        return ValidatedAddonZip(
            sha256_digest=artifact.sha256_digest,
            compressed_size=artifact.compressed_size,
            total_uncompressed_bytes=artifact.total_uncompressed_bytes,
            member_count=artifact.member_count,
            addon_id=artifact.addon_id,
            addon_name=artifact.addon_name,
            addon_version=artifact.addon_version,
            extension_classification=artifact.extension_classification,
            dependencies=artifact.dependencies,
            payload_manifest_digest=artifact.payload_manifest_digest,
            entries=tuple(entries),
            manifest_bytes=manifest_bytes,
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        AddonZipValidationError,
    ) as error:
        raise AddonZipValidationError(
            "addon_zip_artifact_drift",
            "Accepted ZIP artifact storage no longer matches immutable metadata.",
        ) from error

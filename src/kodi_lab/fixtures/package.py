"""Strict loading and deterministic archiving of trusted fixture resources."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any
from uuid import UUID

from kodi_lab.domain import (
    FixtureContentCategory,
    FixtureDefinition,
    FixtureSizeCategory,
)
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_LOCAL_SCRAPER,
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
    KODI_VIDEO_DATABASE_FILENAME,
    KODI_VIDEO_DATABASE_SCHEMA_VERSION,
    FixtureSeedAudit,
    FixtureSeedAuditError,
    audit_small_mixed_v2_seed,
)

SMALL_MIXED_V1_FIXTURE_SLUG = "small-mixed-v1"
SMALL_MIXED_V2_FIXTURE_SLUG = "small-mixed-v2"
# Kept as the original public name so existing v1 callers remain unchanged.
SMALL_MIXED_FIXTURE_SLUG = SMALL_MIXED_V1_FIXTURE_SLUG
TRUSTED_FIXTURE_SLUGS = frozenset(
    {SMALL_MIXED_V1_FIXTURE_SLUG, SMALL_MIXED_V2_FIXTURE_SLUG}
)
FIXTURE_CONTAINER_DESTINATION = "/home/kodi/.kodi"
FIXTURE_MEDIA_CONTAINER_PATH = "/home/kodi/.kodi/fixture-media"
FIXTURE_SOURCES_CONTAINER_PATH = "/home/kodi/.kodi/userdata/sources.xml"
FIXTURE_VIDEO_DATABASE_CONTAINER_PATH = (
    f"/home/kodi/.kodi/userdata/Database/{KODI_VIDEO_DATABASE_FILENAME}"
)
KODI_UID = 1000
KODI_GID = 1000

_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "slug",
        "fixture_version",
        "size_category",
        "content_category",
        "content_digest",
        "compatible_kodi_releases",
        "description",
        "container_destination",
        "files",
    }
)
_V2_MANIFEST_KEYS = _MANIFEST_KEYS | {"video_database_seed"}
_SEED_MANIFEST_KEYS = frozenset(
    {
        "database_filename",
        "schema_version",
        "sha256",
        "container_relative_path",
        "movie_path",
        "movie_content",
        "tv_path",
        "tv_content",
        "scraper",
    }
)


class PackagedFixtureError(RuntimeError):
    """A trusted fixture resource is missing, malformed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class PackagedFixtureFile:
    resource_path: str
    archive_path: str
    size: int
    sha256: str
    data: bytes


@dataclass(frozen=True, slots=True)
class PackagedFixture:
    definition: FixtureDefinition
    destination: str
    members: tuple[PackagedFixtureFile, ...]
    seed_audit: FixtureSeedAudit | None = None

    def build_archive(self) -> bytes:
        """Return one deterministic tar with fixed safe names and Kodi ownership."""

        output = io.BytesIO()
        directories: set[str] = set()
        for member in self.members:
            parent = PurePosixPath(member.archive_path).parent
            while str(parent) != ".":
                directories.add(f"{parent.as_posix()}/")
                parent = parent.parent
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.GNU_FORMAT
        ) as archive:
            for directory in sorted(
                directories, key=lambda value: (value.count("/"), value)
            ):
                info = _tar_info(directory, mode=0o755, size=0)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            for member in self.members:
                info = _tar_info(member.archive_path, mode=0o644, size=member.size)
                archive.addfile(info, io.BytesIO(member.data))
        return output.getvalue()


def _tar_info(name: str, *, mode: int, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.mode = mode
    info.size = size
    info.uid = KODI_UID
    info.gid = KODI_GID
    info.uname = "kodi"
    info.gname = "kodi"
    info.mtime = 0
    return info


def load_packaged_fixture(slug: str) -> PackagedFixture:
    """Load an approved fixture; the slug never becomes a filesystem path."""

    if slug not in TRUSTED_FIXTURE_SLUGS:
        raise PackagedFixtureError("fixture has no trusted packaged resource")
    root = files("kodi_lab.fixtures").joinpath(slug)
    try:
        manifest_bytes = root.joinpath("manifest.json").read_bytes()
        raw = json.loads(manifest_bytes)
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise PackagedFixtureError(
            "fixture manifest is unavailable or invalid"
        ) from error
    expected_keys = (
        _V2_MANIFEST_KEYS if slug == SMALL_MIXED_V2_FIXTURE_SLUG else _MANIFEST_KEYS
    )
    if not isinstance(raw, dict) or frozenset(raw) != expected_keys:
        raise PackagedFixtureError("fixture manifest schema is invalid")
    document = MappingProxyType(raw)
    _require_exact_manifest_constants(document)
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise PackagedFixtureError("fixture manifest file list is invalid")
    members = tuple(_load_member(root, value, slug=slug) for value in raw_files)
    paths = [member.resource_path for member in members]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise PackagedFixtureError("fixture manifest paths must be unique and sorted")
    digest_document = dict(raw)
    claimed_digest = digest_document.pop("content_digest")
    computed_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(digest_document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    if claimed_digest != computed_digest:
        raise PackagedFixtureError("fixture manifest content digest is invalid")
    try:
        definition = FixtureDefinition(
            id=UUID(_require_str(document, "id")),
            slug=_require_str(document, "slug"),
            fixture_version=_require_int(document, "fixture_version"),
            size_category=FixtureSizeCategory(_require_str(document, "size_category")),
            content_category=FixtureContentCategory(
                _require_str(document, "content_category")
            ),
            content_digest=computed_digest,
            compatible_kodi_version=_compatible_release(document),
            description=_require_str(document, "description"),
        )
    except (TypeError, ValueError) as error:
        raise PackagedFixtureError("fixture definition metadata is invalid") from error
    seed_audit = (
        _audit_seed(document, members) if slug == SMALL_MIXED_V2_FIXTURE_SLUG else None
    )
    return PackagedFixture(
        definition=definition,
        destination=FIXTURE_CONTAINER_DESTINATION,
        members=members,
        seed_audit=seed_audit,
    )


def _require_exact_manifest_constants(document: MappingProxyType[str, Any]) -> None:
    slug = document.get("slug")
    if not isinstance(slug, str):
        raise PackagedFixtureError("fixture manifest identity is invalid")
    expected = {
        SMALL_MIXED_V1_FIXTURE_SLUG: (1, 1),
        SMALL_MIXED_V2_FIXTURE_SLUG: (2, 2),
    }.get(slug)
    if expected is None:
        raise PackagedFixtureError("fixture manifest identity is invalid")
    schema_version, fixture_version = expected
    if document.get("schema_version") != schema_version:
        raise PackagedFixtureError("fixture manifest version is unsupported")
    if document.get("fixture_version") != fixture_version:
        raise PackagedFixtureError("fixture manifest identity is invalid")
    if document.get("container_destination") != FIXTURE_CONTAINER_DESTINATION:
        raise PackagedFixtureError("fixture container destination is invalid")


def _load_member(root: Any, value: object, *, slug: str) -> PackagedFixtureFile:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise PackagedFixtureError("fixture file entry is invalid")
    path = value.get("path")
    size = value.get("size")
    digest = value.get("sha256")
    if (
        not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise PackagedFixtureError("fixture file metadata is invalid")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or pure.parts[:1] != ("payload",):
        raise PackagedFixtureError("fixture file path is unsafe")
    archive = PurePosixPath(*pure.parts[1:])
    if archive.is_absolute() or not archive.parts or ".." in archive.parts:
        raise PackagedFixtureError("fixture archive path is unsafe")
    database_member = archive.parts == (
        "userdata",
        "Database",
        KODI_VIDEO_DATABASE_FILENAME,
    )
    if not (
        archive.parts[0] == "fixture-media"
        or archive.parts == ("userdata", "sources.xml")
        or (slug == SMALL_MIXED_V2_FIXTURE_SLUG and database_member)
    ):
        raise PackagedFixtureError("fixture archive path is outside fixed layout")
    try:
        data = root.joinpath(*pure.parts).read_bytes()
    except (FileNotFoundError, OSError) as error:
        raise PackagedFixtureError("fixture content is unavailable") from error
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise PackagedFixtureError("fixture content differs from its manifest")
    return PackagedFixtureFile(path, archive.as_posix(), size, digest, data)


def _audit_seed(
    document: MappingProxyType[str, Any], members: tuple[PackagedFixtureFile, ...]
) -> FixtureSeedAudit:
    raw = document.get("video_database_seed")
    if not isinstance(raw, dict) or frozenset(raw) != _SEED_MANIFEST_KEYS:
        raise PackagedFixtureError("fixture database seed metadata is invalid")
    expected_path = f"userdata/Database/{KODI_VIDEO_DATABASE_FILENAME}"
    expected = {
        "database_filename": KODI_VIDEO_DATABASE_FILENAME,
        "schema_version": KODI_VIDEO_DATABASE_SCHEMA_VERSION,
        "container_relative_path": expected_path,
        "movie_path": FIXTURE_MOVIE_DIRECTORY,
        "movie_content": "movies",
        "tv_path": FIXTURE_TV_DIRECTORY,
        "tv_content": "tvshows",
        "scraper": FIXTURE_LOCAL_SCRAPER,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise PackagedFixtureError("fixture database seed metadata is invalid")
    matches = [member for member in members if member.archive_path == expected_path]
    if len(matches) != 1 or raw.get("sha256") != matches[0].sha256:
        raise PackagedFixtureError("fixture database seed digest is invalid")
    try:
        return audit_small_mixed_v2_seed(matches[0].data)
    except FixtureSeedAuditError as error:
        raise PackagedFixtureError("fixture database seed audit failed") from error


def _require_str(document: MappingProxyType[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise PackagedFixtureError(f"fixture manifest {key} is invalid")
    return value


def _require_int(document: MappingProxyType[str, Any], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PackagedFixtureError(f"fixture manifest {key} is invalid")
    return value


def _compatible_release(document: MappingProxyType[str, Any]) -> str:
    value = document.get("compatible_kodi_releases")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise PackagedFixtureError("fixture compatibility is invalid")
    return value[0]

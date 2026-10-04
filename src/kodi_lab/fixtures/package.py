"""Strict loading and deterministic archiving of trusted fixture resources."""

from __future__ import annotations

import hashlib
import io
import json
import math
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
    KODI_19_VIDEO_DATABASE_FILENAME,
    KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
    KODI_20_VIDEO_DATABASE_FILENAME,
    KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
    KODI_22_VIDEO_DATABASE_FILENAME,
    KODI_22_VIDEO_DATABASE_SCHEMA_VERSION,
    KODI_VIDEO_DATABASE_FILENAME,
    KODI_VIDEO_DATABASE_SCHEMA_VERSION,
    FixtureSeedAudit,
    FixtureSeedAuditError,
    audit_medium_mixed_v1_seed,
    audit_small_mixed_v2_seed,
    audit_small_mixed_v3_seed,
    audit_small_mixed_v4_seed,
    audit_small_mixed_v5_seed,
    audit_small_movies_v1_seed,
    audit_small_movies_v2_seed,
    audit_small_movies_v3_seed,
    audit_small_tv_v1_seed,
    audit_small_tv_v2_seed,
    audit_small_tv_v3_seed,
)
from kodi_lab.fixtures.semantics import (
    MIXED_FIXTURE_SEMANTICS,
    FixtureEpisodeExpectation,
    FixtureMovieExpectation,
    FixtureMoviePlaybackExpectation,
    FixtureSemanticSpecification,
    FixtureSeriesExpectation,
)

SMALL_MIXED_V1_FIXTURE_SLUG = "small-mixed-v1"
SMALL_MIXED_V2_FIXTURE_SLUG = "small-mixed-v2"
SMALL_MIXED_V3_FIXTURE_SLUG = "small-mixed-v3"
SMALL_MIXED_V4_FIXTURE_SLUG = "small-mixed-v4"
SMALL_MIXED_V5_FIXTURE_SLUG = "small-mixed-v5"
SMALL_MIXED_V6_FIXTURE_SLUG = "small-mixed-v6"
SMALL_MOVIES_V1_FIXTURE_SLUG = "small-movies-v1"
SMALL_MOVIES_V2_FIXTURE_SLUG = "small-movies-v2"
SMALL_MOVIES_V3_FIXTURE_SLUG = "small-movies-v3"
SMALL_MOVIES_V4_FIXTURE_SLUG = "small-movies-v4"
SMALL_MOVIES_V5_FIXTURE_SLUG = "small-movies-v5"
SMALL_TV_V1_FIXTURE_SLUG = "small-tv-v1"
SMALL_TV_V2_FIXTURE_SLUG = "small-tv-v2"
SMALL_TV_V3_FIXTURE_SLUG = "small-tv-v3"
MEDIUM_MIXED_V1_FIXTURE_SLUG = "medium-mixed-v1"
CROSS_MOVIES_MATRIX_V1_FIXTURE_SLUG = "cross-movies-matrix-v1"
CROSS_MOVIES_NEXUS_V1_FIXTURE_SLUG = "cross-movies-nexus-v1"
CROSS_MOVIES_OMEGA_V1_FIXTURE_SLUG = "cross-movies-omega-v1"
CROSS_MOVIES_FIXTURE_SLUGS = frozenset(
    {
        CROSS_MOVIES_MATRIX_V1_FIXTURE_SLUG,
        CROSS_MOVIES_NEXUS_V1_FIXTURE_SLUG,
        CROSS_MOVIES_OMEGA_V1_FIXTURE_SLUG,
    }
)
SMALL_MOVIES_FIXTURE_SLUGS = (
    frozenset(
        {
            SMALL_MOVIES_V1_FIXTURE_SLUG,
            SMALL_MOVIES_V2_FIXTURE_SLUG,
            SMALL_MOVIES_V3_FIXTURE_SLUG,
            SMALL_MOVIES_V4_FIXTURE_SLUG,
            SMALL_MOVIES_V5_FIXTURE_SLUG,
        }
    )
    | CROSS_MOVIES_FIXTURE_SLUGS
)
SMALL_TV_FIXTURE_SLUGS = frozenset(
    {SMALL_TV_V1_FIXTURE_SLUG, SMALL_TV_V2_FIXTURE_SLUG, SMALL_TV_V3_FIXTURE_SLUG}
)
MEDIUM_MIXED_FIXTURE_SLUGS = frozenset({MEDIUM_MIXED_V1_FIXTURE_SLUG})
SEMANTIC_FIXTURE_SLUGS = (
    SMALL_MOVIES_FIXTURE_SLUGS | SMALL_TV_FIXTURE_SLUGS | MEDIUM_MIXED_FIXTURE_SLUGS
)
# Kept as the original public name so existing v1 callers remain unchanged.
SMALL_MIXED_FIXTURE_SLUG = SMALL_MIXED_V1_FIXTURE_SLUG
TRUSTED_FIXTURE_SLUGS = frozenset(
    {
        SMALL_MIXED_V1_FIXTURE_SLUG,
        SMALL_MIXED_V2_FIXTURE_SLUG,
        SMALL_MIXED_V3_FIXTURE_SLUG,
        SMALL_MIXED_V4_FIXTURE_SLUG,
        SMALL_MIXED_V5_FIXTURE_SLUG,
        SMALL_MIXED_V6_FIXTURE_SLUG,
        SMALL_MOVIES_V1_FIXTURE_SLUG,
        SMALL_MOVIES_V2_FIXTURE_SLUG,
        SMALL_MOVIES_V3_FIXTURE_SLUG,
        SMALL_MOVIES_V4_FIXTURE_SLUG,
        SMALL_MOVIES_V5_FIXTURE_SLUG,
        SMALL_TV_V1_FIXTURE_SLUG,
        SMALL_TV_V2_FIXTURE_SLUG,
        SMALL_TV_V3_FIXTURE_SLUG,
        MEDIUM_MIXED_V1_FIXTURE_SLUG,
        *CROSS_MOVIES_FIXTURE_SLUGS,
    }
)
FIXTURE_CONTAINER_DESTINATION = "/home/kodi/.kodi"
FIXTURE_MEDIA_CONTAINER_PATH = "/home/kodi/.kodi/fixture-media"
FIXTURE_SOURCES_CONTAINER_PATH = "/home/kodi/.kodi/userdata/sources.xml"
FIXTURE_VIDEO_DATABASE_CONTAINER_PATH = (
    f"/home/kodi/.kodi/userdata/Database/{KODI_VIDEO_DATABASE_FILENAME}"
)
FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS = MappingProxyType(
    {
        SMALL_MIXED_V2_FIXTURE_SLUG: FIXTURE_VIDEO_DATABASE_CONTAINER_PATH,
        SMALL_MIXED_V3_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MIXED_V4_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_19_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MIXED_V5_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_22_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MIXED_V6_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_19_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MOVIES_V1_FIXTURE_SLUG: FIXTURE_VIDEO_DATABASE_CONTAINER_PATH,
        SMALL_MOVIES_V2_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MOVIES_V3_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_19_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MOVIES_V4_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_MOVIES_V5_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_TV_V1_FIXTURE_SLUG: FIXTURE_VIDEO_DATABASE_CONTAINER_PATH,
        SMALL_TV_V2_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        SMALL_TV_V3_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_19_VIDEO_DATABASE_FILENAME}"
        ),
        MEDIUM_MIXED_V1_FIXTURE_SLUG: FIXTURE_VIDEO_DATABASE_CONTAINER_PATH,
        CROSS_MOVIES_MATRIX_V1_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_19_VIDEO_DATABASE_FILENAME}"
        ),
        CROSS_MOVIES_NEXUS_V1_FIXTURE_SLUG: (
            f"/home/kodi/.kodi/userdata/Database/{KODI_20_VIDEO_DATABASE_FILENAME}"
        ),
        CROSS_MOVIES_OMEGA_V1_FIXTURE_SLUG: FIXTURE_VIDEO_DATABASE_CONTAINER_PATH,
    }
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
_SEEDED_MANIFEST_KEYS = _MANIFEST_KEYS | {"video_database_seed"}
_SEMANTIC_SEEDED_MANIFEST_KEYS = _SEEDED_MANIFEST_KEYS | {"semantic_expectations"}
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
_MOVIES_SEED_MANIFEST_KEYS = _SEED_MANIFEST_KEYS - {"tv_path", "tv_content"}
_TV_SEED_MANIFEST_KEYS = _SEED_MANIFEST_KEYS - {"movie_path", "movie_content"}
_SEMANTIC_EXPECTATION_KEYS = frozenset(
    {"movies", "tvshows", "episodes", "scan_plan", "drift_movie_unique_id"}
)
_POST_SCAN_MOVIE_STATE_KEYS = frozenset({"watched", "in_progress"})
_WATCHED_MOVIE_KEYS = frozenset({"unique_id", "lastplayed"})
_IN_PROGRESS_MOVIE_KEYS = frozenset({"unique_id", "position", "total"})
_TV_SEMANTIC_EXPECTATION_KEYS = frozenset(
    {"movies", "tvshows", "episodes", "scan_plan", "drift_episode_unique_id"}
)
_MIXED_SEMANTIC_EXPECTATION_KEYS = frozenset(
    {
        "movies",
        "tvshows",
        "episodes",
        "scan_plan",
        "drift_movie_unique_id",
        "drift_episode_unique_id",
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
    semantics: FixtureSemanticSpecification = MIXED_FIXTURE_SEMANTICS

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
    if slug in SEMANTIC_FIXTURE_SLUGS:
        expected_keys = _SEMANTIC_SEEDED_MANIFEST_KEYS
    elif slug in FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS:
        expected_keys = _SEEDED_MANIFEST_KEYS
    else:
        expected_keys = _MANIFEST_KEYS
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
        _audit_seed(document, members, slug=slug)
        if slug in FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS
        else None
    )
    semantics = (
        _parse_semantics(document, slug=slug)
        if slug in SEMANTIC_FIXTURE_SLUGS
        else MIXED_FIXTURE_SEMANTICS
    )
    return PackagedFixture(
        definition=definition,
        destination=FIXTURE_CONTAINER_DESTINATION,
        members=members,
        seed_audit=seed_audit,
        semantics=semantics,
    )


def _require_exact_manifest_constants(document: MappingProxyType[str, Any]) -> None:
    slug = document.get("slug")
    if not isinstance(slug, str):
        raise PackagedFixtureError("fixture manifest identity is invalid")
    expected = {
        SMALL_MIXED_V1_FIXTURE_SLUG: (1, 1),
        SMALL_MIXED_V2_FIXTURE_SLUG: (2, 2),
        SMALL_MIXED_V3_FIXTURE_SLUG: (3, 3),
        SMALL_MIXED_V4_FIXTURE_SLUG: (4, 4),
        SMALL_MIXED_V5_FIXTURE_SLUG: (12, 5),
        SMALL_MIXED_V6_FIXTURE_SLUG: (13, 6),
        SMALL_MOVIES_V1_FIXTURE_SLUG: (5, 1),
        SMALL_TV_V1_FIXTURE_SLUG: (6, 1),
        SMALL_MOVIES_V2_FIXTURE_SLUG: (7, 2),
        SMALL_TV_V2_FIXTURE_SLUG: (8, 2),
        SMALL_MOVIES_V3_FIXTURE_SLUG: (9, 3),
        SMALL_MOVIES_V4_FIXTURE_SLUG: (14, 4),
        SMALL_MOVIES_V5_FIXTURE_SLUG: (15, 5),
        SMALL_TV_V3_FIXTURE_SLUG: (10, 3),
        MEDIUM_MIXED_V1_FIXTURE_SLUG: (11, 1),
        CROSS_MOVIES_MATRIX_V1_FIXTURE_SLUG: (16, 1),
        CROSS_MOVIES_NEXUS_V1_FIXTURE_SLUG: (17, 1),
        CROSS_MOVIES_OMEGA_V1_FIXTURE_SLUG: (18, 1),
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
    database_path = FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS.get(slug)
    database_member = (
        database_path is not None
        and archive.as_posix()
        == database_path.removeprefix(FIXTURE_CONTAINER_DESTINATION + "/")
    )
    if not (
        archive.parts[0] == "fixture-media"
        or archive.parts == ("userdata", "sources.xml")
        or database_member
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
    document: MappingProxyType[str, Any],
    members: tuple[PackagedFixtureFile, ...],
    *,
    slug: str,
) -> FixtureSeedAudit:
    raw = document.get("video_database_seed")
    expected_seed_keys = (
        _MOVIES_SEED_MANIFEST_KEYS
        if slug in SMALL_MOVIES_FIXTURE_SLUGS
        else _TV_SEED_MANIFEST_KEYS
        if slug in SMALL_TV_FIXTURE_SLUGS
        else _SEED_MANIFEST_KEYS
    )
    if not isinstance(raw, dict) or frozenset(raw) != expected_seed_keys:
        raise PackagedFixtureError("fixture database seed metadata is invalid")
    seed_contract = {
        SMALL_MIXED_V2_FIXTURE_SLUG: (
            KODI_VIDEO_DATABASE_FILENAME,
            KODI_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_mixed_v2_seed,
        ),
        SMALL_MIXED_V3_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_mixed_v3_seed,
        ),
        SMALL_MIXED_V4_FIXTURE_SLUG: (
            KODI_19_VIDEO_DATABASE_FILENAME,
            KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_mixed_v4_seed,
        ),
        SMALL_MIXED_V5_FIXTURE_SLUG: (
            KODI_22_VIDEO_DATABASE_FILENAME,
            KODI_22_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_mixed_v5_seed,
        ),
        SMALL_MIXED_V6_FIXTURE_SLUG: (
            KODI_19_VIDEO_DATABASE_FILENAME,
            KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_mixed_v4_seed,
        ),
        SMALL_MOVIES_V1_FIXTURE_SLUG: (
            KODI_VIDEO_DATABASE_FILENAME,
            KODI_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v1_seed,
        ),
        SMALL_TV_V1_FIXTURE_SLUG: (
            KODI_VIDEO_DATABASE_FILENAME,
            KODI_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_tv_v1_seed,
        ),
        SMALL_MOVIES_V2_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v2_seed,
        ),
        SMALL_TV_V2_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_tv_v2_seed,
        ),
        SMALL_MOVIES_V3_FIXTURE_SLUG: (
            KODI_19_VIDEO_DATABASE_FILENAME,
            KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v3_seed,
        ),
        SMALL_MOVIES_V4_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v2_seed,
        ),
        SMALL_MOVIES_V5_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v2_seed,
        ),
        SMALL_TV_V3_FIXTURE_SLUG: (
            KODI_19_VIDEO_DATABASE_FILENAME,
            KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_tv_v3_seed,
        ),
        MEDIUM_MIXED_V1_FIXTURE_SLUG: (
            KODI_VIDEO_DATABASE_FILENAME,
            KODI_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_medium_mixed_v1_seed,
        ),
        CROSS_MOVIES_MATRIX_V1_FIXTURE_SLUG: (
            KODI_19_VIDEO_DATABASE_FILENAME,
            KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v3_seed,
        ),
        CROSS_MOVIES_NEXUS_V1_FIXTURE_SLUG: (
            KODI_20_VIDEO_DATABASE_FILENAME,
            KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v2_seed,
        ),
        CROSS_MOVIES_OMEGA_V1_FIXTURE_SLUG: (
            KODI_VIDEO_DATABASE_FILENAME,
            KODI_VIDEO_DATABASE_SCHEMA_VERSION,
            audit_small_movies_v1_seed,
        ),
    }.get(slug)
    if seed_contract is None:
        raise PackagedFixtureError("fixture database seed metadata is invalid")
    database_filename, schema_version, audit = seed_contract
    expected_path = f"userdata/Database/{database_filename}"
    expected: dict[str, object] = {
        "database_filename": database_filename,
        "schema_version": schema_version,
        "container_relative_path": expected_path,
        "scraper": FIXTURE_LOCAL_SCRAPER,
    }
    if slug not in SMALL_TV_FIXTURE_SLUGS:
        expected.update(
            {"movie_path": FIXTURE_MOVIE_DIRECTORY, "movie_content": "movies"}
        )
    if slug not in SMALL_MOVIES_FIXTURE_SLUGS:
        expected.update({"tv_path": FIXTURE_TV_DIRECTORY, "tv_content": "tvshows"})
    if any(raw.get(key) != value for key, value in expected.items()):
        raise PackagedFixtureError("fixture database seed metadata is invalid")
    matches = [member for member in members if member.archive_path == expected_path]
    if len(matches) != 1 or raw.get("sha256") != matches[0].sha256:
        raise PackagedFixtureError("fixture database seed digest is invalid")
    try:
        return audit(matches[0].data)
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


def _parse_semantics(
    document: MappingProxyType[str, Any],
    *,
    slug: str,
) -> FixtureSemanticSpecification:
    raw = document.get("semantic_expectations")
    expected_keys = (
        _SEMANTIC_EXPECTATION_KEYS | {"post_scan_movie_state"}
        if slug == SMALL_MOVIES_V5_FIXTURE_SLUG
        else _SEMANTIC_EXPECTATION_KEYS
        if slug in SMALL_MOVIES_FIXTURE_SLUGS
        else _TV_SEMANTIC_EXPECTATION_KEYS
        if slug in SMALL_TV_FIXTURE_SLUGS
        else _MIXED_SEMANTIC_EXPECTATION_KEYS
    )
    if not isinstance(raw, dict) or frozenset(raw) != expected_keys:
        raise PackagedFixtureError("fixture semantic expectations are invalid")
    movies = raw.get("movies")
    shows = raw.get("tvshows")
    episodes = raw.get("episodes")
    scan_plan = raw.get("scan_plan")
    drift_movie_id = raw.get("drift_movie_unique_id")
    drift_episode_id = raw.get("drift_episode_unique_id")
    is_movies = slug in SMALL_MOVIES_FIXTURE_SLUGS
    is_tv = slug in SMALL_TV_FIXTURE_SLUGS
    is_medium_mixed = slug in MEDIUM_MIXED_FIXTURE_SLUGS
    is_rich_movies = slug in {
        SMALL_MOVIES_V4_FIXTURE_SLUG,
        SMALL_MOVIES_V5_FIXTURE_SLUG,
    }
    is_cross_movies = slug in CROSS_MOVIES_FIXTURE_SLUGS
    expected_scan_plan = (
        ["movies"] if is_movies else ["tv"] if is_tv else ["movies", "tv"]
    )
    if (
        not isinstance(movies, list)
        or not isinstance(shows, list)
        or not isinstance(episodes, list)
        or scan_plan != expected_scan_plan
        or ((is_movies or is_medium_mixed) and not isinstance(drift_movie_id, str))
        or ((is_tv or is_medium_mixed) and not isinstance(drift_episode_id, str))
    ):
        raise PackagedFixtureError("fixture semantic expectations are invalid")
    try:
        movie_specs = tuple(
            FixtureMovieExpectation(
                title=_semantic_str(
                    item,
                    "title",
                    (
                        {
                            "title",
                            "year",
                            "unique_id",
                            "file",
                            "genres",
                            "actors",
                            "director",
                            "studio",
                            "rating",
                            "set_name",
                            "tags",
                        }
                        if is_rich_movies
                        else {"title", "year", "unique_id", "file"}
                    ),
                ),
                year=_semantic_int(item, "year"),
                unique_id=_semantic_str(item, "unique_id"),
                file=_semantic_str(item, "file"),
                genres=_semantic_string_tuple(item, "genres")
                if is_rich_movies
                else None,
                actors=_semantic_string_tuple(item, "actors")
                if is_rich_movies
                else None,
                director=_semantic_str(item, "director") if is_rich_movies else None,
                studio=_semantic_str(item, "studio") if is_rich_movies else None,
                rating=_semantic_rating(item) if is_rich_movies else None,
                set_name=_semantic_optional_string(item, "set_name")
                if is_rich_movies
                else None,
                tags=_semantic_string_tuple(item, "tags") if is_rich_movies else None,
            )
            for item in movies
        )
        show_specs = tuple(
            FixtureSeriesExpectation(
                title=_semantic_str(
                    item,
                    "title",
                    {"title", "year", "unique_id", "directory"},
                ),
                year=_semantic_int(item, "year"),
                unique_id=_semantic_str(item, "unique_id"),
                directory=_semantic_str(item, "directory"),
            )
            for item in shows
        )
        episode_specs = tuple(
            FixtureEpisodeExpectation(
                title=_semantic_str(
                    item,
                    "title",
                    {
                        "title",
                        "show_title",
                        "season",
                        "episode",
                        "unique_id",
                        "file",
                    },
                ),
                show_title=_semantic_str(item, "show_title"),
                season=_semantic_int(item, "season"),
                episode=_semantic_int(item, "episode"),
                unique_id=_semantic_str(item, "unique_id"),
                file=_semantic_str(item, "file"),
            )
            for item in episodes
        )
    except (TypeError, ValueError, KeyError) as error:
        raise PackagedFixtureError(
            "fixture semantic expectations are invalid"
        ) from error
    movie_ids = {movie.unique_id for movie in movie_specs}
    episode_ids = {episode.unique_id for episode in episode_specs}
    movies_contract = (
        len(movie_specs) == (12 if is_rich_movies else 9 if is_cross_movies else 3)
        and not show_specs
        and not episode_specs
        and drift_movie_id in movie_ids
        and len(movie_ids) == len(movie_specs)
        and (
            tuple(
                (movie.title, movie.year, movie.unique_id, movie.file)
                for movie in movie_specs
            )
            == _expected_rich_movie_identities()
            if is_rich_movies
            else tuple(
                (movie.title, movie.year, movie.unique_id, movie.file)
                for movie in movie_specs
            )
            == _expected_cross_movie_identities()
            if is_cross_movies
            else True
        )
    )
    tv_contract = (
        not movie_specs
        and len(show_specs) == 1
        and len(episode_specs) == 3
        and drift_episode_id in episode_ids
        and len(episode_ids) == len(episode_specs)
        and all(
            episode.show_title == show_specs[0].title
            and episode.season == 1
            and episode.episode == index
            for index, episode in enumerate(episode_specs, 1)
        )
    )
    medium_contract = (
        is_medium_mixed
        and len(movie_specs) == 25
        and len(show_specs) == 5
        and len(episode_specs) == 50
        and drift_movie_id in movie_ids
        and drift_episode_id in episode_ids
        and len(movie_ids) == len(movie_specs)
        and len({show.unique_id for show in show_specs}) == len(show_specs)
        and len(episode_ids) == len(episode_specs)
        and tuple(movie_specs) == _expected_medium_movies()
        and tuple(show_specs) == _expected_medium_series()
        and tuple(episode_specs) == _expected_medium_episodes()
    )
    if not (
        movies_contract if is_movies else tv_contract if is_tv else medium_contract
    ):
        raise PackagedFixtureError("fixture semantic expectations are invalid")
    playback = (
        _parse_rich_movie_playback(raw.get("post_scan_movie_state"), movie_specs)
        if slug == SMALL_MOVIES_V5_FIXTURE_SLUG
        else ()
    )
    return FixtureSemanticSpecification(
        movies=movie_specs,
        series=show_specs,
        episodes=episode_specs,
        scan_movies=is_movies or is_medium_mixed,
        scan_tv=is_tv or is_medium_mixed,
        drift_movie_unique_id=(
            drift_movie_id if isinstance(drift_movie_id, str) else None
        ),
        drift_episode_unique_id=(
            drift_episode_id if isinstance(drift_episode_id, str) else None
        ),
        movie_playback=playback,
    )


def _parse_rich_movie_playback(
    raw: object, movies: tuple[FixtureMovieExpectation, ...]
) -> tuple[FixtureMoviePlaybackExpectation, ...]:
    if not isinstance(raw, dict) or frozenset(raw) != _POST_SCAN_MOVIE_STATE_KEYS:
        raise PackagedFixtureError("fixture post-scan state is invalid")
    watched = raw.get("watched")
    in_progress = raw.get("in_progress")
    if not isinstance(watched, list) or not isinstance(in_progress, list):
        raise PackagedFixtureError("fixture post-scan state is invalid")
    try:
        if any(
            not isinstance(item, dict) or frozenset(item) != _WATCHED_MOVIE_KEYS
            for item in watched
        ) or any(
            not isinstance(item, dict) or frozenset(item) != _IN_PROGRESS_MOVIE_KEYS
            for item in in_progress
        ):
            raise ValueError
        watched_by_id = {
            _semantic_str(item, "unique_id"): _semantic_str(item, "lastplayed")
            for item in watched
        }
        progress_by_id = {
            _semantic_str(item, "unique_id"): (
                _semantic_nonnegative_number(item, "position"),
                _semantic_nonnegative_number(item, "total"),
            )
            for item in in_progress
        }
        if len(watched_by_id) != len(watched) or len(progress_by_id) != len(
            in_progress
        ):
            raise ValueError
        movie_ids = {movie.unique_id for movie in movies}
        if (
            len(watched) != 5
            or len(in_progress) != 2
            or not (set(watched_by_id) | set(progress_by_id)) <= movie_ids
        ):
            raise ValueError
        if set(watched_by_id) & set(progress_by_id):
            raise ValueError
        states = tuple(
            FixtureMoviePlaybackExpectation(
                unique_id=movie.unique_id,
                playcount=1 if movie.unique_id in watched_by_id else 0,
                lastplayed=watched_by_id.get(movie.unique_id),
                resume_position=progress_by_id.get(movie.unique_id, (0.0, 0.0))[0],
                resume_total=progress_by_id.get(movie.unique_id, (0.0, 0.0))[1],
            )
            for movie in movies
        )
        if states != _expected_rich_movie_playback():
            raise ValueError
        return states
    except (TypeError, ValueError) as error:
        raise PackagedFixtureError("fixture post-scan state is invalid") from error


def _semantic_nonnegative_number(item: dict[str, object], key: str) -> float:
    value = item.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("invalid post-scan value")
    return float(value)


def _expected_rich_movie_playback() -> tuple[FixtureMoviePlaybackExpectation, ...]:
    watched = {1, 5, 7, 10, 12}
    progress = {3: 1.0, 8: 1.0}
    return tuple(
        FixtureMoviePlaybackExpectation(
            unique_id=f"small-movies-v4-movie-{index:02d}",
            playcount=1 if index in watched else 0,
            lastplayed="2020-01-01 00:00:00" if index in watched else None,
            resume_position=progress.get(index, 0.0),
            resume_total=2.0 if index in progress else 0.0,
        )
        for index in range(1, 13)
    )


def _expected_medium_movies() -> tuple[FixtureMovieExpectation, ...]:
    return tuple(
        FixtureMovieExpectation(
            title=f"Kodi Lab Medium Movie {index:03d}",
            year=2000 + index,
            unique_id=f"medium-mixed-v1-movie-{index:03d}",
            file=(
                f"{FIXTURE_MOVIE_DIRECTORY}kodi-lab-medium-movie-{index:03d}/"
                f"Kodi Lab Medium Movie {index:03d} ({2000 + index}).mp4"
            ),
        )
        for index in range(1, 26)
    )


def _expected_rich_movie_identities() -> tuple[tuple[str, int, str, str], ...]:
    identities = (
        ("Jaws", 1975),
        ("Rocky", 1976),
        ("Alien", 1979),
        ("The Shining", 1980),
        ("Back to the Future", 1985),
        ("Die Hard", 1988),
        ("Home Alone", 1990),
        ("Jurassic Park", 1993),
        ("The Matrix", 1999),
        ("Gladiator", 2000),
        ("Finding Nemo", 2003),
        ("The Dark Knight", 2008),
    )
    return tuple(
        (
            title,
            year,
            f"small-movies-v4-movie-{index:02d}",
            f"{FIXTURE_MOVIE_DIRECTORY}{title} ({year})/{title} ({year}).mp4",
        )
        for index, (title, year) in enumerate(identities, 1)
    )


def _expected_cross_movie_identities() -> tuple[tuple[str, int, str, str], ...]:
    years = (2001, 2005, 2009, 2011, 2015, 2019, 2021, 2024, 2026)
    return tuple(
        (
            f"Kodi Lab Cross-Version Movie {index:03d}",
            year,
            f"cross-movies-v1-movie-{index:03d}",
            (
                f"{FIXTURE_MOVIE_DIRECTORY}kodi-lab-cross-movie-{index:03d}/"
                f"Kodi Lab Cross-Version Movie {index:03d} ({year}).mp4"
            ),
        )
        for index, year in enumerate(years, 1)
    )


def _expected_medium_series() -> tuple[FixtureSeriesExpectation, ...]:
    return tuple(
        FixtureSeriesExpectation(
            title=f"Kodi Lab Medium Series {show:02d}",
            year=2020 + show,
            unique_id=f"medium-mixed-v1-series-{show:02d}",
            directory=(f"{FIXTURE_TV_DIRECTORY}kodi-lab-medium-series-{show:02d}/"),
        )
        for show in range(1, 6)
    )


def _expected_medium_episodes() -> tuple[FixtureEpisodeExpectation, ...]:
    return tuple(
        FixtureEpisodeExpectation(
            title=(f"Kodi Lab Medium Series {show:02d} S{season:02d}E{episode:02d}"),
            show_title=f"Kodi Lab Medium Series {show:02d}",
            season=season,
            episode=episode,
            unique_id=(
                f"medium-mixed-v1-series-{show:02d}-s{season:02d}e{episode:02d}"
            ),
            file=(
                f"{FIXTURE_TV_DIRECTORY}kodi-lab-medium-series-{show:02d}/"
                f"Season {season:02d}/Kodi Lab Medium Series {show:02d} "
                f"S{season:02d}E{episode:02d}.mp4"
            ),
        )
        for show in range(1, 6)
        for season in (1, 2)
        for episode in range(1, 6)
    )


def _semantic_str(item: object, key: str, exact_keys: set[str] | None = None) -> str:
    if not isinstance(item, dict) or (
        exact_keys is not None and set(item) != exact_keys
    ):
        raise ValueError("invalid semantic entity")
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid semantic string")
    return value


def _semantic_int(item: object, key: str) -> int:
    if not isinstance(item, dict):
        raise ValueError("invalid semantic entity")
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid semantic integer")
    return value


def _semantic_string_tuple(item: object, key: str) -> tuple[str, ...]:
    if not isinstance(item, dict):
        raise ValueError("invalid semantic entity")
    value = item.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(member, str) or not member for member in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid semantic string collection")
    return tuple(value)


def _semantic_optional_string(item: object, key: str) -> str | None:
    if not isinstance(item, dict):
        raise ValueError("invalid semantic entity")
    value = item.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("invalid optional semantic string")
    return value


def _semantic_rating(item: object) -> float:
    if not isinstance(item, dict):
        raise ValueError("invalid semantic entity")
    value = item.get("rating")
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 <= value <= 10
    ):
        raise ValueError("invalid semantic rating")
    return float(value)


def _compatible_release(document: MappingProxyType[str, Any]) -> str:
    value = document.get("compatible_kodi_releases")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], str):
        raise PackagedFixtureError("fixture compatibility is invalid")
    return value[0]

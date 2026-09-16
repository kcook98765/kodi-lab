"""Read-only audits of exact-release fixture configuration seeds."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

KODI_VIDEO_DATABASE_FILENAME = "MyVideos131.db"
KODI_VIDEO_DATABASE_SCHEMA_VERSION = 131
KODI_20_VIDEO_DATABASE_FILENAME = "MyVideos121.db"
KODI_20_VIDEO_DATABASE_SCHEMA_VERSION = 121
KODI_19_VIDEO_DATABASE_FILENAME = "MyVideos119.db"
KODI_19_VIDEO_DATABASE_SCHEMA_VERSION = 119
FIXTURE_MOVIE_DIRECTORY = "/home/kodi/.kodi/fixture-media/movies/"
FIXTURE_TV_DIRECTORY = "/home/kodi/.kodi/fixture-media/tvshows/"
FIXTURE_LOCAL_SCRAPER = "metadata.local"

_EXPECTED_PATH_ROWS: tuple[tuple[object, ...], ...] = (
    (
        1,
        FIXTURE_MOVIE_DIRECTORY,
        "movies",
        FIXTURE_LOCAL_SCRAPER,
        None,
        1,
        1,
        "",
        0,
        0,
        0,
        None,
        None,
    ),
    (
        2,
        FIXTURE_TV_DIRECTORY,
        "tvshows",
        FIXTURE_LOCAL_SCRAPER,
        None,
        0,
        0,
        "",
        0,
        0,
        0,
        None,
        None,
    ),
)

_SMALL_MOVIES_EXPECTED_PATH_ROWS: tuple[tuple[object, ...], ...] = (
    _EXPECTED_PATH_ROWS[0],
)

_SMALL_TV_EXPECTED_PATH_ROWS: tuple[tuple[object, ...], ...] = (
    (1, *_EXPECTED_PATH_ROWS[1][1:]),
)

# A Kodi-created empty Omega database has only the version row and Kodi's
# built-in video-version type catalog. The fixture seed adds exactly two path
# configuration rows. Every library and user-state table must remain empty.
_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 2,
    "version": 1,
    "videoversiontype": 387,
}

_SMALL_MOVIES_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 1,
    "version": 1,
    "videoversiontype": 387,
}

_KODI_20_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 2,
    "version": 1,
}

_KODI_20_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 1,
    "version": 1,
}

_KODI_19_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 2,
    "version": 1,
}

_KODI_19_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS = {
    "path": 1,
    "version": 1,
}


class FixtureSeedAuditError(RuntimeError):
    """The immutable database seed violates its configuration-only contract."""


@dataclass(frozen=True, slots=True)
class FixtureSeedAudit:
    database_filename: str
    schema_version: int
    configured_paths: tuple[tuple[object, ...], ...]
    table_counts: tuple[tuple[str, int], ...]


def audit_small_mixed_v2_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 21.3 seed without accepting a path or mutable SQL."""

    return _audit_seed(
        database,
        database_filename=KODI_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_EXPECTED_PATH_ROWS,
    )


def audit_medium_mixed_v1_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 21.3 Medium/Mixed configuration-only seed."""

    return _audit_seed(
        database,
        database_filename=KODI_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_EXPECTED_PATH_ROWS,
    )


def audit_small_movies_v1_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 21.3 Movies-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_SMALL_MOVIES_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_SMALL_MOVIES_EXPECTED_PATH_ROWS,
    )


def audit_small_tv_v1_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 21.3 TV-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_SMALL_MOVIES_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_SMALL_TV_EXPECTED_PATH_ROWS,
    )


def audit_small_mixed_v3_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 20.5 seed without accepting a path or mutable SQL."""

    return _audit_seed(
        database,
        database_filename=KODI_20_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_KODI_20_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_EXPECTED_PATH_ROWS,
    )


def audit_small_mixed_v4_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 19.5 seed without accepting a path or mutable SQL."""

    return _audit_seed(
        database,
        database_filename=KODI_19_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=_KODI_19_EXPECTED_NONZERO_TABLE_COUNTS,
        expected_paths=_EXPECTED_PATH_ROWS,
    )


def audit_small_movies_v2_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 20.5 Movies-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_20_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=(
            _KODI_20_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS
        ),
        expected_paths=_SMALL_MOVIES_EXPECTED_PATH_ROWS,
    )


def audit_small_tv_v2_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 20.5 TV-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_20_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=(
            _KODI_20_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS
        ),
        expected_paths=_SMALL_TV_EXPECTED_PATH_ROWS,
    )


def audit_small_movies_v3_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 19.5 Movies-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_19_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=(
            _KODI_19_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS
        ),
        expected_paths=_SMALL_MOVIES_EXPECTED_PATH_ROWS,
    )


def audit_small_tv_v3_seed(database: bytes) -> FixtureSeedAudit:
    """Audit the fixed Kodi 19.5 TV-only configuration seed."""

    return _audit_seed(
        database,
        database_filename=KODI_19_VIDEO_DATABASE_FILENAME,
        schema_version=KODI_19_VIDEO_DATABASE_SCHEMA_VERSION,
        expected_nonzero_table_counts=(
            _KODI_19_SINGLE_SOURCE_EXPECTED_NONZERO_TABLE_COUNTS
        ),
        expected_paths=_SMALL_TV_EXPECTED_PATH_ROWS,
    )


def _audit_seed(
    database: bytes,
    *,
    database_filename: str,
    schema_version: int,
    expected_nonzero_table_counts: dict[str, int],
    expected_paths: tuple[tuple[object, ...], ...],
) -> FixtureSeedAudit:
    """Apply the shared strict purity contract to one approved exact schema."""

    if not database:
        raise FixtureSeedAuditError("fixture database seed is empty")
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database)
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise FixtureSeedAuditError("fixture database seed is not integral")
        version = connection.execute(
            "SELECT idVersion, iCompressCount FROM version"
        ).fetchall()
        if version != [(schema_version, 0)]:
            raise FixtureSeedAuditError("fixture database schema version is invalid")
        paths = tuple(
            connection.execute(
                """
                SELECT idPath, strPath, strContent, strScraper, strHash,
                       scanRecursive, useFolderNames, strSettings, noUpdate,
                       exclude, allAudio, dateAdded, idParentPath
                  FROM path
              ORDER BY idPath
                """
            ).fetchall()
        )
        if paths != expected_paths:
            raise FixtureSeedAuditError(
                "fixture database path configuration is invalid"
            )
        table_names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        )
        table_counts = tuple(
            (
                name,
                int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]),
            )
            for name in table_names
        )
        for name, count in table_counts:
            expected = expected_nonzero_table_counts.get(name, 0)
            if count != expected:
                raise FixtureSeedAuditError(
                    f"fixture database table {name} contains unexpected state"
                )
        if set(expected_nonzero_table_counts) - set(table_names):
            raise FixtureSeedAuditError("fixture database schema is incomplete")
        return FixtureSeedAudit(
            database_filename=database_filename,
            schema_version=schema_version,
            configured_paths=paths,
            table_counts=table_counts,
        )
    except sqlite3.Error as error:
        raise FixtureSeedAuditError("fixture database seed is invalid") from error
    finally:
        connection.close()

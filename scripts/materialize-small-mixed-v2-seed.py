#!/usr/bin/env python3
"""Materialize the fixed small-mixed-v2 seed from a Kodi-created empty DB.

This development-only helper accepts no arguments, paths, or SQL. The input is
an exact fresh Kodi 21.3 database captured under build/fixture-seed; the output
is the one package-owned immutable fixture asset.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from kodi_lab.fixtures.seed_audit import (
    FIXTURE_LOCAL_SCRAPER,
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
    KODI_VIDEO_DATABASE_SCHEMA_VERSION,
    audit_small_mixed_v2_seed,
)

REPOSITORY = Path(__file__).resolve().parents[1]
INPUT = REPOSITORY / "build/fixture-seed/small-mixed-v2/MyVideos131.fresh.db"
OUTPUT = (
    REPOSITORY
    / "src/kodi_lab/fixtures/small-mixed-v2/payload/userdata/Database/MyVideos131.db"
)

_INSERT_PATH = """
INSERT INTO path(
    strPath, strContent, strScraper, strHash, scanRecursive,
    useFolderNames, strSettings, noUpdate, exclude, allAudio,
    dateAdded, idParentPath
) VALUES (?, ?, ?, NULL, ?, ?, '', 0, 0, 0, NULL, NULL)
"""


def main() -> None:
    if not INPUT.is_file():
        raise SystemExit(f"missing fixed Kodi-generated input: {INPUT}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(INPUT, OUTPUT)
    connection = sqlite3.connect(OUTPUT)
    try:
        if connection.execute(
            "SELECT idVersion, iCompressCount FROM version"
        ).fetchall() != [(KODI_VIDEO_DATABASE_SCHEMA_VERSION, 0)]:
            raise RuntimeError("input is not a fresh Kodi 21.3 video database")
        if connection.execute("SELECT COUNT(*) FROM path").fetchone() != (0,):
            raise RuntimeError("input video database already contains paths")
        for table in ("movie", "tvshow", "episode", "files", "art", "bookmark"):
            if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() != (0,):
                raise RuntimeError(f"input video database contains {table} state")
        connection.execute(
            _INSERT_PATH,
            (
                FIXTURE_MOVIE_DIRECTORY,
                "movies",
                FIXTURE_LOCAL_SCRAPER,
                1,
                1,
            ),
        )
        connection.execute(
            _INSERT_PATH,
            (
                FIXTURE_TV_DIRECTORY,
                "tvshows",
                FIXTURE_LOCAL_SCRAPER,
                0,
                0,
            ),
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    audit_small_mixed_v2_seed(OUTPUT.read_bytes())


if __name__ == "__main__":
    main()

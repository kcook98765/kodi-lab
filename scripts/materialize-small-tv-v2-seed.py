#!/usr/bin/env python3
"""Materialize the fixed Small/TV seed from a Kodi-created Nexus DB.

This development-only, argument-free helper has one fixed input, output, SQL
statement, and TV path. It is not imported by product or broker code.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from kodi_lab.fixtures.seed_audit import (
    FIXTURE_LOCAL_SCRAPER,
    FIXTURE_TV_DIRECTORY,
    KODI_20_VIDEO_DATABASE_SCHEMA_VERSION,
    audit_small_tv_v2_seed,
)

REPOSITORY = Path(__file__).resolve().parents[1]
INPUT = REPOSITORY / "build/fixture-seed/small-tv-v2/MyVideos121.fresh.db"
OUTPUT = (
    REPOSITORY
    / "src/kodi_lab/fixtures/small-tv-v2/payload/userdata/Database/MyVideos121.db"
)

_INSERT_TV_PATH = """
INSERT INTO path(
    strPath, strContent, strScraper, strHash, scanRecursive,
    useFolderNames, strSettings, noUpdate, exclude, allAudio,
    dateAdded, idParentPath
) VALUES (?, 'tvshows', ?, NULL, 0, 0, '', 0, 0, 0, NULL, NULL)
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
        ).fetchall() != [(KODI_20_VIDEO_DATABASE_SCHEMA_VERSION, 0)]:
            raise RuntimeError("input is not a fresh Kodi 20.5 video database")
        for table in ("path", "movie", "tvshow", "episode", "files", "art", "bookmark"):
            if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() != (0,):
                raise RuntimeError(f"input video database contains {table} state")
        connection.execute(
            _INSERT_TV_PATH,
            (FIXTURE_TV_DIRECTORY, FIXTURE_LOCAL_SCRAPER),
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    audit_small_tv_v2_seed(OUTPUT.read_bytes())


if __name__ == "__main__":
    main()

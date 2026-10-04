#!/usr/bin/env python3
"""Materialize three single-release packages for one nine-movie test dataset.

This development-only helper accepts no arguments and refuses to overwrite
existing fixture packages. Media and NFO content are identical across releases;
each package carries its own Kodi-created, configuration-only video DB seed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    audit_small_movies_v1_seed,
    audit_small_movies_v2_seed,
    audit_small_movies_v3_seed,
)

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY / "src/kodi_lab/fixtures"
MEDIA_SOURCE = (
    FIXTURES / "small-movies-v1/payload/fixture-media/movies/kodi-lab-fixture-movie-1"
)
YEARS = (2001, 2005, 2009, 2011, 2015, 2019, 2021, 2024, 2026)
MOVIE_ROOT = FIXTURE_MOVIE_DIRECTORY
FAMILY = (
    (
        "cross-movies-matrix-v1",
        "8b25126e-f633-4778-bb24-41e232e19f26",
        "19.4",
        16,
        "MyVideos119.db",
        119,
        FIXTURES / "small-mixed-v6/payload/userdata/Database/MyVideos119.db",
        audit_small_movies_v3_seed,
    ),
    (
        "cross-movies-nexus-v1",
        "f153d0d8-d76b-42e7-984c-f7a9b2fd0aa1",
        "20.5",
        17,
        "MyVideos121.db",
        121,
        FIXTURES / "small-movies-v2/payload/userdata/Database/MyVideos121.db",
        audit_small_movies_v2_seed,
    ),
    (
        "cross-movies-omega-v1",
        "81765a3d-1fd8-4b16-8f2e-850858052093",
        "21.3",
        18,
        "MyVideos131.db",
        131,
        FIXTURES / "small-movies-v1/payload/userdata/Database/MyVideos131.db",
        audit_small_movies_v1_seed,
    ),
)


def _movie_nfo(index: int, year: int) -> str:
    title = f"Kodi Lab Cross-Version Movie {index:03d}"
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>{title}</title>
  <originaltitle>{title}</originaltitle>
  <sorttitle>{title}</sorttitle>
  <year>{year}</year>
  <premiered>{year}-01-01</premiered>
  <plot>Fictional movie {index:03d} for deterministic cross-version Kodi testing.</plot>
  <runtime>1</runtime>
  <genre>Synthetic</genre>
  <studio>Kodi Lab</studio>
  <uniqueid type="kodi-lab" default="true">cross-movies-v1-movie-{index:03d}</uniqueid>
  <art><poster>poster.png</poster><fanart>fanart.png</fanart></art>
</movie>
"""


def _sources_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sources>
  <programs><default pathversion="1"></default></programs>
  <video>
    <default pathversion="1"></default>
    <source>
      <name>Kodi Lab Cross-Version Movies</name>
      <path pathversion="1">/home/kodi/.kodi/fixture-media/movies/</path>
      <allowsharing>true</allowsharing>
    </source>
  </video>
  <music><default pathversion="1"></default></music>
  <pictures><default pathversion="1"></default></pictures>
  <files><default pathversion="1"></default></files>
  <games><default pathversion="1"></default></games>
</sources>
"""


def _seed(
    source: Path,
    destination: Path,
    *,
    matrix: bool,
    audit: Callable[[bytes], object],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if matrix:
        # The reviewed Kodi 19.4 seed has movies and TV paths. This family is
        # movies-only; remove only the empty TV source, retaining schema 119.
        connection = sqlite3.connect(destination)
        try:
            rows = connection.execute(
                "SELECT idPath, strPath FROM path WHERE strContent = 'tvshows'"
            ).fetchall()
            if rows != [(2, "/home/kodi/.kodi/fixture-media/tvshows/")]:
                raise RuntimeError("Matrix source seed has unexpected TV configuration")
            connection.execute("DELETE FROM path WHERE idPath = 2")
            connection.commit()
            connection.execute("VACUUM")
        finally:
            connection.close()
    audit(destination.read_bytes())


def _materialize(
    slug: str,
    fixture_id: str,
    release: str,
    schema_version: int,
    database: str,
    database_schema: int,
    seed_source: Path,
    audit: Callable[[bytes], object],
) -> None:
    output = FIXTURES / slug
    if output.exists():
        raise RuntimeError(f"refusing to overwrite immutable fixture: {output}")
    payload = output / "payload"
    movie_media = next(MEDIA_SOURCE.glob("*.mp4"))
    poster = MEDIA_SOURCE / "poster.png"
    fanart = MEDIA_SOURCE / "fanart.png"
    movies = []
    for index, year in enumerate(YEARS, 1):
        title = f"Kodi Lab Cross-Version Movie {index:03d}"
        directory_name = f"kodi-lab-cross-movie-{index:03d}"
        directory = payload / "fixture-media/movies" / directory_name
        directory.mkdir(parents=True)
        base = f"{title} ({year})"
        shutil.copyfile(movie_media, directory / f"{base}.mp4")
        (directory / f"{base}.nfo").write_text(
            _movie_nfo(index, year), encoding="utf-8"
        )
        shutil.copyfile(poster, directory / "poster.png")
        shutil.copyfile(fanart, directory / "fanart.png")
        movies.append(
            {
                "file": f"{MOVIE_ROOT}{directory_name}/{base}.mp4",
                "title": title,
                "unique_id": f"cross-movies-v1-movie-{index:03d}",
                "year": year,
            }
        )
    source_path = payload / "userdata/sources.xml"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(_sources_xml(), encoding="utf-8")
    seed_path = payload / "userdata/Database" / database
    _seed(seed_source, seed_path, matrix=release == "19.4", audit=audit)

    members = []
    for path in sorted(payload.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            members.append(
                {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
    manifest = {
        "compatible_kodi_releases": [release],
        "container_destination": "/home/kodi/.kodi",
        "content_category": "movies",
        "description": (
            f"Cross-Version Movies family: nine synthetic movies for Kodi {release}; "
            "three movies in each of the 2000s, 2010s, and 2020s."
        ),
        "files": members,
        "fixture_version": 1,
        "id": fixture_id,
        "schema_version": schema_version,
        "semantic_expectations": {
            "drift_movie_unique_id": "cross-movies-v1-movie-001",
            "episodes": [],
            "movies": movies,
            "scan_plan": ["movies"],
            "tvshows": [],
        },
        "size_category": "small",
        "slug": slug,
        "video_database_seed": {
            "container_relative_path": f"userdata/Database/{database}",
            "database_filename": database,
            "movie_content": "movies",
            "movie_path": MOVIE_ROOT,
            "schema_version": database_schema,
            "scraper": "metadata.local",
            "sha256": hashlib.sha256(seed_path.read_bytes()).hexdigest(),
        },
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["content_digest"] = f"sha256:{digest}"
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    if any((FIXTURES / member[0]).exists() for member in FAMILY):
        raise SystemExit("refusing to overwrite an immutable family member")
    for member in FAMILY:
        _materialize(*member)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Materialize the one fixed Medium/Mixed fixture package.

This development-only helper accepts no arguments. It reuses the finalized
Kodi-21.3-generated two-source configuration seed and repository-owned media
and artwork bytes, while materializing every immutable NFO and manifest entry.
It is not imported by application or broker code.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
OUTPUT = REPOSITORY / "src/kodi_lab/fixtures/medium-mixed-v1"
PAYLOAD = OUTPUT / "payload"
MOVIE_SOURCE = (
    REPOSITORY / "src/kodi_lab/fixtures/small-movies-v1/payload/fixture-media/movies/"
    "kodi-lab-fixture-movie-1"
)
TV_SOURCE = (
    REPOSITORY / "src/kodi_lab/fixtures/small-tv-v1/payload/fixture-media/tvshows/"
    "kodi-lab-fixture-series-tv"
)
SEED_SOURCE = (
    REPOSITORY / "src/kodi_lab/fixtures/small-mixed-v2/payload/userdata/Database/"
    "MyVideos131.db"
)
SOURCES_SOURCE = (
    REPOSITORY / "src/kodi_lab/fixtures/small-mixed-v2/payload/userdata/sources.xml"
)

FIXTURE_ID = "13cfd4d5-236a-480c-ae7d-eb54398ab184"
MOVIE_ROOT = "/home/kodi/.kodi/fixture-media/movies/"
TV_ROOT = "/home/kodi/.kodi/fixture-media/tvshows/"


def _movie_nfo(index: int, year: int) -> str:
    title = f"Kodi Lab Medium Movie {index:03d}"
    unique_id = f"medium-mixed-v1-movie-{index:03d}"
    plot = (
        f"Fictional Medium fixture movie {index:03d} generated solely for "
        "deterministic Kodi Lab testing."
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
  <title>{title}</title>
  <originaltitle>{title}</originaltitle>
  <sorttitle>{title}</sorttitle>
  <year>{year}</year>
  <premiered>{year}-01-01</premiered>
  <plot>{plot}</plot>
  <runtime>1</runtime>
  <genre>Synthetic</genre>
  <studio>Kodi Lab</studio>
  <uniqueid type="kodi-lab" default="true">{unique_id}</uniqueid>
  <art><poster>poster.png</poster><fanart>fanart.png</fanart></art>
</movie>
"""


def _show_nfo(show: int, year: int) -> str:
    title = f"Kodi Lab Medium Series {show:02d}"
    unique_id = f"medium-mixed-v1-series-{show:02d}"
    plot = (
        f"Fictional Medium fixture series {show:02d} generated solely for "
        "deterministic Kodi Lab testing."
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tvshow>
  <title>{title}</title>
  <originaltitle>{title}</originaltitle>
  <sorttitle>{title}</sorttitle>
  <year>{year}</year>
  <premiered>{year}-01-01</premiered>
  <plot>{plot}</plot>
  <genre>Synthetic</genre>
  <studio>Kodi Lab</studio>
  <uniqueid type="kodi-lab" default="true">{unique_id}</uniqueid>
  <art><poster>poster.png</poster><fanart>fanart.png</fanart></art>
</tvshow>
"""


def _episode_nfo(show: int, season: int, episode: int, year: int) -> str:
    show_title = f"Kodi Lab Medium Series {show:02d}"
    title = f"Kodi Lab Medium Series {show:02d} S{season:02d}E{episode:02d}"
    unique_id = f"medium-mixed-v1-series-{show:02d}-s{season:02d}e{episode:02d}"
    plot = (
        f"Fictional Medium fixture series {show:02d}, season {season}, "
        f"episode {episode}."
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<episodedetails>
  <title>{title}</title>
  <showtitle>{show_title}</showtitle>
  <season>{season}</season><episode>{episode}</episode>
  <aired>{year}-{season:02d}-{episode:02d}</aired>
  <plot>{plot}</plot>
  <runtime>1</runtime>
  <uniqueid type="kodi-lab" default="true">{unique_id}</uniqueid>
  <thumb>{title}-thumb.png</thumb>
</episodedetails>
"""


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _materialize_payload() -> tuple[list[dict[str, object]], dict[str, object]]:
    movies: list[dict[str, object]] = []
    shows: list[dict[str, object]] = []
    episodes: list[dict[str, object]] = []
    movie_media = next(MOVIE_SOURCE.glob("*.mp4"))
    poster = MOVIE_SOURCE / "poster.png"
    fanart = MOVIE_SOURCE / "fanart.png"
    episode_media = next((TV_SOURCE / "Season 01").glob("*.mp4"))
    episode_thumb = next((TV_SOURCE / "Season 01").glob("*-thumb.png"))

    for index in range(1, 26):
        year = 2000 + index
        title = f"Kodi Lab Medium Movie {index:03d}"
        directory_name = f"kodi-lab-medium-movie-{index:03d}"
        directory = PAYLOAD / "fixture-media/movies" / directory_name
        base = f"{title} ({year})"
        _copy(movie_media, directory / f"{base}.mp4")
        (directory / f"{base}.nfo").write_text(
            _movie_nfo(index, year), encoding="utf-8"
        )
        _copy(poster, directory / "poster.png")
        _copy(fanart, directory / "fanart.png")
        movies.append(
            {
                "file": f"{MOVIE_ROOT}{directory_name}/{base}.mp4",
                "title": title,
                "unique_id": f"medium-mixed-v1-movie-{index:03d}",
                "year": year,
            }
        )

    for show in range(1, 6):
        year = 2020 + show
        show_title = f"Kodi Lab Medium Series {show:02d}"
        directory_name = f"kodi-lab-medium-series-{show:02d}"
        directory = PAYLOAD / "fixture-media/tvshows" / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tvshow.nfo").write_text(_show_nfo(show, year), encoding="utf-8")
        _copy(TV_SOURCE / "poster.png", directory / "poster.png")
        _copy(TV_SOURCE / "fanart.png", directory / "fanart.png")
        shows.append(
            {
                "directory": f"{TV_ROOT}{directory_name}/",
                "title": show_title,
                "unique_id": f"medium-mixed-v1-series-{show:02d}",
                "year": year,
            }
        )
        for season in (1, 2):
            season_directory = directory / f"Season {season:02d}"
            for episode in range(1, 6):
                title = f"Kodi Lab Medium Series {show:02d} S{season:02d}E{episode:02d}"
                base = title
                _copy(episode_media, season_directory / f"{base}.mp4")
                (season_directory / f"{base}.nfo").write_text(
                    _episode_nfo(show, season, episode, year), encoding="utf-8"
                )
                _copy(episode_thumb, season_directory / f"{base}-thumb.png")
                episodes.append(
                    {
                        "episode": episode,
                        "file": (
                            f"{TV_ROOT}{directory_name}/Season {season:02d}/{base}.mp4"
                        ),
                        "season": season,
                        "show_title": show_title,
                        "title": title,
                        "unique_id": (
                            f"medium-mixed-v1-series-{show:02d}-"
                            f"s{season:02d}e{episode:02d}"
                        ),
                    }
                )

    _copy(SEED_SOURCE, PAYLOAD / "userdata/Database/MyVideos131.db")
    _copy(SOURCES_SOURCE, PAYLOAD / "userdata/sources.xml")
    semantics = {
        "drift_episode_unique_id": "medium-mixed-v1-series-01-s01e01",
        "drift_movie_unique_id": "medium-mixed-v1-movie-001",
        "episodes": episodes,
        "movies": movies,
        "scan_plan": ["movies", "tv"],
        "tvshows": shows,
    }
    return movies, semantics


def _manifest(semantics: dict[str, object]) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in sorted(PAYLOAD.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(OUTPUT).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    seed = PAYLOAD / "userdata/Database/MyVideos131.db"
    document: dict[str, object] = {
        "compatible_kodi_releases": ["21.3"],
        "container_destination": "/home/kodi/.kodi",
        "content_category": "mixed",
        "description": (
            "Twenty-five synthetic movies and five two-season synthetic TV "
            "series with an exact Kodi 21.3 configuration-only seed."
        ),
        "files": files,
        "fixture_version": 1,
        "id": FIXTURE_ID,
        "schema_version": 11,
        "semantic_expectations": semantics,
        "size_category": "medium",
        "slug": "medium-mixed-v1",
        "video_database_seed": {
            "container_relative_path": "userdata/Database/MyVideos131.db",
            "database_filename": "MyVideos131.db",
            "movie_content": "movies",
            "movie_path": MOVIE_ROOT,
            "schema_version": 131,
            "scraper": "metadata.local",
            "sha256": hashlib.sha256(seed.read_bytes()).hexdigest(),
            "tv_content": "tvshows",
            "tv_path": TV_ROOT,
        },
    }
    digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    document["content_digest"] = f"sha256:{digest}"
    return document


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite immutable fixture: {OUTPUT}")
    _movies, semantics = _materialize_payload()
    document = _manifest(semantics)
    (OUTPUT / "manifest.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

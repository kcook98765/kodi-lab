#!/usr/bin/env python3
"""Create the fixed Rich Small Movies package; refuse historical overwrites.

The movie title/year pairs are real identities. Every other metadata field,
poster, fanart, and playable MP4 byte is Kodi Lab synthetic test material.
This development-only materializer requires Pillow; runtime loading does not.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]

REPOSITORY = Path(__file__).resolve().parents[1]
OUTPUT = REPOSITORY / "src/kodi_lab/fixtures/small-movies-v4"
PAYLOAD = OUTPUT / "payload"
OLD = REPOSITORY / "src/kodi_lab/fixtures/small-movies-v2/payload"
MEDIA = (
    OLD
    / "fixture-media/movies/kodi-lab-fixture-movie-1"
    / "Kodi Lab Fixture Movie 1 (2024).mp4"
)
SEED = OLD / "userdata/Database/MyVideos121.db"
SOURCES = OLD / "userdata/sources.xml"
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MOVIE_ROOT = "/home/kodi/.kodi/fixture-media/movies/"
FIXTURE_ID = "b69b4aa6-9968-4d5f-8ac9-3d5b16556d94"


@dataclass(frozen=True)
class Movie:
    title: str
    year: int
    genres: tuple[str, ...]
    actors: tuple[str, ...]
    director: str
    studio: str
    rating: float
    set_name: str | None
    tags: tuple[str, ...]


MOVIES = (
    Movie(
        "Jaws",
        1975,
        ("Adventure", "Thriller"),
        ("Ada Vale", "Ivo Reed"),
        "Nora Flint",
        "Atlas Lab",
        8.1,
        "Kodi Lab Set A",
        ("Archive", "Night"),
    ),
    Movie(
        "Rocky",
        1976,
        ("Drama", "Sport"),
        ("Lila Stone", "Ivo Reed"),
        "Mira North",
        "Bay Lab",
        7.4,
        "Kodi Lab Set B",
        ("Journey", "Classic"),
    ),
    Movie(
        "Alien",
        1979,
        ("Science Fiction", "Horror"),
        ("Ada Vale", "Omar Finch"),
        "Nora Flint",
        "Orion Lab",
        8.7,
        "Kodi Lab Set A",
        ("Night", "Space"),
    ),
    Movie(
        "The Shining",
        1980,
        ("Horror", "Drama"),
        ("Lila Stone", "Omar Finch"),
        "Theo Wren",
        "Atlas Lab",
        6.5,
        None,
        ("Night", "Classic"),
    ),
    Movie(
        "Back to the Future",
        1985,
        ("Science Fiction", "Comedy"),
        ("Ada Vale", "Pia Hart"),
        "Mira North",
        "Bay Lab",
        9.0,
        "Kodi Lab Set B",
        ("Journey", "Family"),
    ),
    Movie(
        "Die Hard",
        1988,
        ("Action", "Thriller"),
        ("Omar Finch", "Pia Hart"),
        "Theo Wren",
        "Orion Lab",
        7.9,
        None,
        ("Night", "Action"),
    ),
    Movie(
        "Home Alone",
        1990,
        ("Comedy", "Family"),
        ("Lila Stone", "Pia Hart"),
        "Nora Flint",
        "Atlas Lab",
        5.8,
        None,
        ("Family", "Classic"),
    ),
    Movie(
        "Jurassic Park",
        1993,
        ("Adventure", "Science Fiction"),
        ("Ada Vale", "Lila Stone"),
        "Mira North",
        "Orion Lab",
        8.4,
        "Kodi Lab Set A",
        ("Journey", "Family"),
    ),
    Movie(
        "The Matrix",
        1999,
        ("Action", "Science Fiction"),
        ("Ada Vale", "Omar Finch"),
        "Theo Wren",
        "Bay Lab",
        9.2,
        None,
        ("Night", "Space"),
    ),
    Movie(
        "Gladiator",
        2000,
        ("Action", "Drama"),
        ("Ivo Reed", "Pia Hart"),
        "Nora Flint",
        "Atlas Lab",
        8.8,
        "Kodi Lab Set B",
        ("Archive", "Action"),
    ),
    Movie(
        "Finding Nemo",
        2003,
        ("Adventure", "Family"),
        ("Lila Stone", "Pia Hart"),
        "Mira North",
        "Bay Lab",
        6.9,
        None,
        ("Family", "Journey"),
    ),
    Movie(
        "The Dark Knight",
        2008,
        ("Action", "Thriller"),
        ("Omar Finch", "Ivo Reed"),
        "Theo Wren",
        "Orion Lab",
        8.5,
        None,
        ("Night", "Action"),
    ),
)


def nfo(movie: Movie, index: int) -> bytes:
    root = ET.Element("movie")
    for key, value in (
        ("title", movie.title),
        ("originaltitle", movie.title),
        ("sorttitle", movie.title),
        ("year", str(movie.year)),
        (
            "plot",
            f"Kodi Lab synthetic test clip {index:02d}; "
            "metadata apart from title and year is fixture-controlled.",
        ),
        ("runtime", "1"),
    ):
        ET.SubElement(root, key).text = value
    ratings = ET.SubElement(root, "ratings")
    rating = ET.SubElement(
        ratings, "rating", {"name": "kodi-lab", "max": "10", "default": "true"}
    )
    ET.SubElement(rating, "value").text = f"{movie.rating:.1f}"
    ET.SubElement(rating, "votes").text = "100"
    for genre in movie.genres:
        ET.SubElement(root, "genre").text = genre
    for tag in movie.tags:
        ET.SubElement(root, "tag").text = tag
    if movie.set_name is not None:
        ET.SubElement(ET.SubElement(root, "set"), "name").text = movie.set_name
    ET.SubElement(root, "director").text = movie.director
    ET.SubElement(root, "studio").text = movie.studio
    ET.SubElement(
        root, "uniqueid", {"type": "kodi-lab", "default": "true"}
    ).text = f"small-movies-v4-movie-{index:02d}"
    for order, name in enumerate(movie.actors):
        actor = ET.SubElement(root, "actor")
        ET.SubElement(actor, "name").text = name
        ET.SubElement(actor, "role").text = f"Fixture role {order + 1}"
        ET.SubElement(actor, "order").text = str(order)
    art = ET.SubElement(root, "art")
    ET.SubElement(art, "poster").text = "poster.png"
    ET.SubElement(art, "fanart").text = "fanart.png"
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="utf-8")
    if not isinstance(body, bytes):
        raise TypeError("NFO XML serialization returned text")
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body + b"\n"


def artwork(destination: Path, movie: Movie, index: int, *, poster: bool) -> None:
    width, height = (160, 240) if poster else (320, 180)
    color = ((index * 29) % 160 + 35, (index * 47) % 160 + 35, (index * 71) % 160 + 35)
    canvas = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((8, 8, width - 9, height - 9), outline="white", width=2)
    draw.polygon([(width - 38, 20), (width - 20, 20), (width - 20, 38)], fill="white")
    title = movie.title.upper()
    if len(title) > 14:
        title = title[:14]
    font = ImageFont.truetype(str(FONT), 13 if poster else 20)
    small = ImageFont.truetype(str(FONT), 10 if poster else 14)
    draw.text((16, 25), "KODI LAB", fill="white", font=small)
    draw.text((16, height // 2 - 10), title, fill="white", font=font)
    draw.text(
        (16, height - 58), f"TEST {index:02d}  {movie.year}", fill="white", font=small
    )
    draw.text((16, height - 32), "SYNTHETIC MEDIA", fill="white", font=small)
    canvas.save(destination, format="PNG", optimize=False)


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite immutable fixture: {OUTPUT}")
    if (
        not MEDIA.is_file()
        or not SEED.is_file()
        or not SOURCES.is_file()
        or not FONT.is_file()
    ):
        raise SystemExit("fixed materialization inputs are unavailable")
    expected_movies = []
    for index, movie in enumerate(MOVIES, 1):
        base = f"{movie.title} ({movie.year})"
        directory = PAYLOAD / "fixture-media/movies" / base
        directory.mkdir(parents=True)
        shutil.copyfile(MEDIA, directory / f"{base}.mp4")
        (directory / f"{base}.nfo").write_bytes(nfo(movie, index))
        artwork(directory / "poster.png", movie, index, poster=True)
        artwork(directory / "fanart.png", movie, index, poster=False)
        expected_movies.append(
            {
                "title": movie.title,
                "year": movie.year,
                "unique_id": f"small-movies-v4-movie-{index:02d}",
                "file": f"{MOVIE_ROOT}{base}/{base}.mp4",
                "genres": list(movie.genres),
                "actors": list(movie.actors),
                "director": movie.director,
                "studio": movie.studio,
                "rating": movie.rating,
                "set_name": movie.set_name,
                "tags": list(movie.tags),
            }
        )
    database = PAYLOAD / "userdata/Database/MyVideos121.db"
    database.parent.mkdir(parents=True)
    shutil.copyfile(SEED, database)
    shutil.copyfile(SOURCES, PAYLOAD / "userdata/sources.xml")
    members = []
    for path in sorted(PAYLOAD.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            members.append(
                {
                    "path": path.relative_to(OUTPUT).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
    manifest = {
        "compatible_kodi_releases": ["20.5"],
        "container_destination": "/home/kodi/.kodi",
        "content_category": "movies",
        "description": (
            "Twelve real movie title/year identities with synthetic Kodi Lab clips, "
            "artwork, and controlled offline NFO metadata for Nexus 20.5."
        ),
        "files": members,
        "fixture_version": 4,
        "id": FIXTURE_ID,
        "schema_version": 14,
        "semantic_expectations": {
            "drift_movie_unique_id": "small-movies-v4-movie-01",
            "episodes": [],
            "movies": expected_movies,
            "scan_plan": ["movies"],
            "tvshows": [],
        },
        "size_category": "small",
        "slug": "small-movies-v4",
        "video_database_seed": {
            "container_relative_path": "userdata/Database/MyVideos121.db",
            "database_filename": "MyVideos121.db",
            "movie_content": "movies",
            "movie_path": MOVIE_ROOT,
            "schema_version": 121,
            "scraper": "metadata.local",
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        },
    }
    manifest["content_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

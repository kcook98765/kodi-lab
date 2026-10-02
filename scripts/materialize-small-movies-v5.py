#!/usr/bin/env python3
"""Create the immutable Nexus post-scan-state revision from accepted v4 bytes."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/kodi_lab/fixtures"
SOURCE = ROOT / "small-movies-v4"
OUTPUT = ROOT / "small-movies-v5"
FIXTURE_ID = "1f5e518e-a643-4dcb-aead-2d1c01b68bcc"
LASTPLAYED = "2020-01-01 00:00:00"


def movie_id(index: int) -> str:
    return f"small-movies-v4-movie-{index:02d}"


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite immutable fixture: {OUTPUT}")
    source = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    shutil.copytree(SOURCE / "payload", OUTPUT / "payload")
    source["id"] = FIXTURE_ID
    source["slug"] = "small-movies-v5"
    source["fixture_version"] = 5
    source["schema_version"] = 15
    source["description"] = (
        "Rich Small Movies for Nexus 20.5 with fixed instance-specific "
        "post-scan watched and resume state."
    )
    source["files"] = [
        {
            **member,
            "path": member["path"].replace("small-movies-v4/", "small-movies-v5/", 1),
        }
        for member in source["files"]
    ]
    source["semantic_expectations"]["post_scan_movie_state"] = {
        "watched": [
            {"unique_id": movie_id(index), "lastplayed": LASTPLAYED}
            for index in (1, 5, 7, 10, 12)
        ],
        "in_progress": [
            {"unique_id": movie_id(3), "position": 1.0, "total": 2.0},
            {"unique_id": movie_id(8), "position": 1.0, "total": 2.0},
        ],
    }
    source.pop("content_digest")
    source["content_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    (OUTPUT / "manifest.json").write_text(
        json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

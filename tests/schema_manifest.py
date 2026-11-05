"""Reproduce the frozen SQLite contract from an independently initialized DB.

Run: uv run python -m tests.schema_manifest /absolute/database.db > manifest.json
The reference was captured from e94f087's complete 92-revision chain, not ORM
metadata or the replacement baseline. SQL digests are byte-exact, not tokenized.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, cast


def schema_manifest(database: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        objects = list(
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' AND name<>'alembic_version' "
                "ORDER BY type,name"
            )
        )
        result: dict[str, Any] = {
            "objects": {
                name: {
                    "type": kind,
                    "table": table,
                    "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                }
                for kind, name, table, sql in objects
            },
            "tables": {},
            "seed_data": {},
        }
        for kind, name, _table, _sql in objects:
            if kind != "table":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            indexes = {}
            for idx in db.execute(f"PRAGMA index_list({quoted})"):
                index_name = '"' + idx[1].replace('"', '""') + '"'
                indexes[idx[1]] = {
                    "unique": idx[2],
                    "origin": idx[3],
                    "partial": idx[4],
                    "columns": db.execute(
                        f"PRAGMA index_xinfo({index_name})"
                    ).fetchall(),
                }
            result["tables"][name] = {
                "columns": db.execute(f"PRAGMA table_xinfo({quoted})").fetchall(),
                "foreign_keys": db.execute(
                    f"PRAGMA foreign_key_list({quoted})"
                ).fetchall(),
                "indexes": indexes,
            }
            rows = db.execute(f"SELECT * FROM {quoted}").fetchall()
            if rows:
                result["seed_data"][name] = sorted(rows, key=repr)
        timestamp = result["seed_data"]["production_mutation_control"][0][-1]
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", timestamp)
        result["seed_data"]["production_mutation_control"][0] = (
            *result["seed_data"]["production_mutation_control"][0][:-1],
            "INITIALIZATION_TIME",
        )
        result["properties"] = {
            key: db.execute(f"PRAGMA {key}").fetchall()
            for key in (
                "encoding",
                "page_size",
                "auto_vacuum",
                "user_version",
                "application_id",
                "journal_mode",
                "recursive_triggers",
            )
        }
        result["properties"]["table_list"] = sorted(
            row
            for row in db.execute("PRAGMA table_list")
            if not row[1].startswith("sqlite_") and row[1] != "alembic_version"
        )
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        return cast(
            dict[str, Any], json.loads(json.dumps(result))
        )  # Stable JSON list representation.


def serialize_manifest(value: dict[str, Any]) -> str:
    """One reviewable line per schema object/table, instead of a binary fixture."""
    sections = []
    for key, entries in value.items():
        sections.append(
            json.dumps(key)
            + ": {\n"
            + ",\n".join(
                "  " + json.dumps(name) + ": " + json.dumps(item, sort_keys=True)
                for name, item in entries.items()
            )
            + "\n}"
        )
    return "{\n" + ",\n".join(sections) + "\n}\n"


if __name__ == "__main__":
    print(serialize_manifest(schema_manifest(Path(sys.argv[1]).resolve())), end="")

"""Read-only admission of empty databases and the public Alembic revision tree."""

import sqlite3
from collections.abc import Collection

INCOMPATIBLE_DATABASE = (
    "Incompatible Kodi Lab database: a fresh installation is required. "
    "Prerelease databases cannot be upgraded. The database was left unchanged."
)


def check_database(connection: sqlite3.Connection, supported: Collection[str]) -> bool:
    """Reject unversioned/unknown/ambiguous state before any migration writes.

    Return whether the database is genuinely empty, including non-table objects.
    Recognized future revisions remain eligible for normal forward migrations.
    """
    objects = list(
        connection.execute(
            "SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    if not objects:
        return True
    if ("table", "alembic_version") not in objects:
        raise RuntimeError(INCOMPATIBLE_DATABASE)
    try:
        revisions = list(connection.execute("SELECT version_num FROM alembic_version"))
    except sqlite3.DatabaseError as error:
        raise RuntimeError(INCOMPATIBLE_DATABASE) from error
    if len(revisions) != 1 or revisions[0][0] not in supported:
        raise RuntimeError(INCOMPATIBLE_DATABASE)
    if not any(kind == "table" and name != "alembic_version" for kind, name in objects):
        raise RuntimeError(INCOMPATIBLE_DATABASE)
    return False

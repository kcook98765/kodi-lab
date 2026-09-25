"""Run the single Alembic revision tree from installed package resources."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from kodi_lab.storage.engine import create_database_engine


@contextmanager
def packaged_config(database_url: str) -> Iterator[Config]:
    migration_tree = resources.files("kodi_lab").joinpath("migrations")
    with resources.as_file(migration_tree) as path:
        if (
            not path.joinpath("env.py").is_file()
            or not path.joinpath("versions").is_dir()
        ):
            raise RuntimeError("installed migration resources are missing")
        config = Config()
        config.set_main_option("script_location", str(path).replace("%", "%%"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        yield config


def migrate_database(database_path: Path, database_url: str) -> tuple[str, str]:
    """Upgrade a new or versioned database; return installed current/head IDs."""

    if database_path.exists() or database_path.is_symlink():
        metadata = database_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("database path is not a regular file")
        with sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True) as db:
            tables = {
                name
                for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables and "alembic_version" not in tables:
                raise RuntimeError("existing database has no Alembic revision")
            if (
                "alembic_version" in tables
                and db.execute("SELECT version_num FROM alembic_version").fetchone()
                is None
            ):
                raise RuntimeError("existing database has no Alembic revision")
    with packaged_config(database_url) as config:
        head = ScriptDirectory.from_config(config).get_current_head()
        if head is None:
            raise RuntimeError("installed migration tree has no head")
        command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
    if current != head:
        raise RuntimeError("migration did not reach the installed head")
    return current, head

"""Run the single Alembic revision tree from installed package resources."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib import resources
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.managed_activity import ManagedResourcePolicyValue
from kodi_lab.storage.schema_compatibility import check_database


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


def database_is_empty(database_path: Path, config: Config) -> bool:
    """Read-only compatibility preflight, also used before installed startup."""
    if not (database_path.exists() or database_path.is_symlink()):
        return True
    if not stat.S_ISREG(database_path.lstat().st_mode):
        raise RuntimeError("database path is not a regular file")
    supported = {
        revision.revision
        for revision in ScriptDirectory.from_config(config).walk_revisions()
    }
    with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as db:
        return check_database(db, supported)


def migrate_database(database_path: Path, database_url: str) -> tuple[str, str]:
    """Initialize/upgrade under the caller's exclusive host lifecycle lock.

    Only an empty database receives today's first-install policy. Unsupported
    prerelease state is refused before Alembic can write anything.
    """
    with packaged_config(database_url) as config:
        new_database = database_is_empty(database_path, config)
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
    if new_database:
        # No administrator policy existed at entry. Versioned databases,
        # including partial/failed initializations, never enter this branch.
        with sqlite3.connect(database_path) as db:
            db.execute(
                "UPDATE managed_resource_policy SET max_running=? WHERE id=1",
                (ManagedResourcePolicyValue().max_running,),
            )
    return current, head

"""Closed canonical Alembic templates; every case owns its database copy."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


class MigrationTemplateStore:
    """Closed, hashed SQLite templates with exact revision verification."""

    def __init__(
        self, root: Path, templates: dict[str, Path], *, head_revision: str
    ) -> None:
        self.root = root
        self._templates = templates
        self._head_revision = head_revision
        self._digests = {
            revision: hashlib.sha256(path.read_bytes()).digest()
            for revision, path in templates.items()
        }

    def clone(self, revision: str, destination: Path) -> None:
        exact = self._head_revision if revision == "head" else revision
        source = self._templates[exact]
        if source.is_symlink() or any(
            Path(f"{source}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise RuntimeError("migration template must be closed without sidecars")
        data = source.read_bytes()
        if hashlib.sha256(data).digest() != self._digests[exact]:
            raise RuntimeError("migration template changed")
        with destination.open("xb") as output:
            output.write(data)
        with closing(sqlite3.connect(destination)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall() == [(exact,)]


_active_migration_templates: MigrationTemplateStore | None = None
_SESSION_TEMPLATES: pytest.StashKey[MigrationTemplateStore] = pytest.StashKey()


def migration_config(database: Path) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "migrations")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


@pytest.fixture(scope="session", autouse=True)
def migration_templates(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> MigrationTemplateStore:
    """Build each current revision once, including the upgrade predecessor."""
    global _active_migration_templates
    cached = request.config.stash.get(_SESSION_TEMPLATES, None)
    if cached is not None:
        _active_migration_templates = cached
        return cached
    root = tmp_path_factory.mktemp("migration-template")
    root.mkdir(parents=True, exist_ok=True)
    database = root / "building.db"
    config = migration_config(database)
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()
    assert head is not None
    templates: dict[str, Path] = {}
    for revision in reversed(list(scripts.walk_revisions())):
        command.upgrade(config, revision.revision)
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall() == [(revision.revision,)]
        assert not any(
            Path(f"{database}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        )
        template = root / f"{revision.revision}.db"
        shutil.copyfile(database, template)
        template.chmod(0o400)
        templates[revision.revision] = template
    store = MigrationTemplateStore(root, templates, head_revision=head)
    request.config.stash[_SESSION_TEMPLATES] = store
    _active_migration_templates = store
    return store


def migration_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    start_revision: str | None = "head",
) -> tuple[Config, str]:
    """Clone current schema, or request a truly empty database with None."""
    data_root = tmp_path / "data"
    database = data_root / "database" / "kodi-lab.db"
    database.parent.mkdir(parents=True)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    if start_revision is not None:
        assert _active_migration_templates is not None
        _active_migration_templates.clone(start_revision, database)
    return migration_config(database), f"sqlite:///{database}"

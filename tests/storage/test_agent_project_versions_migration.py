"""Project lease admission is additive and restores its predecessor exactly."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text

from kodi_lab.storage.engine import create_database_engine
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_project_version_admission_upgrade_and_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0010_agent_projects"
    )
    engine = create_database_engine(url)
    query = text("SELECT sql FROM sqlite_master WHERE name='retained_mutation_insert'")
    with engine.connect() as db:
        before = db.scalar(query)
    command.upgrade(config, "0011_agent_project_versions")
    with engine.connect() as db:
        after = db.scalar(query)
        assert before != after and "JOIN agent_projects" in after
        assert "managed_start_authorizations" in after
        assert db.scalar(text("PRAGMA integrity_check")) == "ok"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, "0010_agent_projects")
    with engine.connect() as db:
        assert db.scalar(query) == before
    engine.dispose()

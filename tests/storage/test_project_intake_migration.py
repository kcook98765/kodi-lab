"""The initial schema protects admitted source provenance."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import AgentProjectRow
from tests.application.test_mcp_workspace_development import (
    NOW_TEXT,
    SOURCE_ID,
    _environment,
)
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_used_intake_provenance_is_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_foundation"
    )
    engine = create_database_engine(url)
    command.upgrade(config, "head")
    assert "intake_provenance_json" in {
        column["name"] for column in inspect(engine).get_columns("agent_projects")
    }
    env = _environment(
        tmp_path / "fixture", database_path=Path(url.removeprefix("sqlite:///"))
    )
    with env.sessions.begin() as db:
        db.add(
            AgentProjectRow(
                source_id=str(SOURCE_ID),
                client_id=str(env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="seeded",
                created_at=NOW_TEXT,
                intake_provenance_json='{"package_sha256":"retained-test-proof"}',
            )
        )
    with env.sessions.begin() as db:
        row = db.get(AgentProjectRow, str(SOURCE_ID))
        assert row is not None
        row.intake_provenance_json = "{}"
        with pytest.raises(IntegrityError, match="identity_immutable"):
            db.flush()
        db.rollback()
    with pytest.raises(
        RuntimeError, match="initial managed-v2 schema cannot be downgraded"
    ):
        command.downgrade(config, "base")
    engine.dispose()

"""Package owner/request history survives migration and rejects lossy downgrade."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import AgentPackageRow
from tests.application.test_mcp_workspace_development import NOW_TEXT, _environment
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_package_migration_and_immutable_owner_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0012_agent_project_intake"
    )
    engine = create_database_engine(url)
    command.upgrade(config, "0013_agent_packages")
    assert "agent_packages" in inspect(engine).get_table_names()
    command.downgrade(config, "0012_agent_project_intake")
    assert "agent_packages" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    e = _environment(
        tmp_path / "fixture", database_path=Path(url.removeprefix("sqlite:///"))
    )
    identity = str(uuid4())
    with e.sessions.begin() as db:
        db.add(
            AgentPackageRow(
                id=identity,
                client_id=str(e.client_id),
                request_id=str(uuid4()),
                archive_sha256="a" * 64,
                provenance_reference="reviewed fixture",
                created_at=NOW_TEXT,
                state="reserved",
            )
        )
    for mutation in ("owner", "request", "delete"):
        with e.sessions.begin() as db:
            row = db.get(AgentPackageRow, identity)
            assert row is not None
            if mutation == "owner":
                row.client_id = str(uuid4())
            elif mutation == "request":
                row.request_id = str(uuid4())
            else:
                db.delete(row)
            with pytest.raises(IntegrityError, match="agent_package_"):
                db.flush()
            db.rollback()
    with pytest.raises(RuntimeError, match="preserve owner history"):
        command.downgrade(config, "0012_agent_project_intake")
    engine.dispose()

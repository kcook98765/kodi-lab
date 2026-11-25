"""The initial schema defaults managed role authority off and fences changes."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_initial_role_is_opt_in_and_durably_revocable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0001_foundation"
    )
    engine = create_database_engine(url)
    client_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_clients "
                "(id,display_name,enabled,preset_id,created_at,authority_revision) "
                "VALUES (:id,'Existing agent',1,'11000000-0000-4000-8000-000000000002',"
                "'2026-11-11T00:00:00.000000Z',1)"
            ),
            {"id": client_id},
        )
        assert (
            connection.scalar(
                text("SELECT managed_lab_role_version FROM mcp_clients WHERE id=:id"),
                {"id": client_id},
            )
            is None
        )
        with pytest.raises(IntegrityError, match="authority_revision_invalid"):
            connection.execute(
                text("UPDATE mcp_clients SET managed_lab_role_version=1 WHERE id=:id"),
                {"id": client_id},
            )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "UPDATE mcp_clients SET managed_lab_role_version=2,"
                    "authority_revision=2 WHERE id=:id"
                ),
                {"id": client_id},
            )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    admin = McpAdministrationService(create_session_factory(engine))
    granted = admin.set_managed_lab_role(UUID(client_id), True)
    assert granted.authority_revision == 2
    assert admin.set_managed_lab_role(UUID(client_id), True).authority_revision == 2
    assert admin.set_managed_lab_role(UUID(client_id), False).authority_revision == 3
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT managed_lab_role_version, authority_revision "
                "FROM mcp_clients WHERE id=:id"
            ),
            {"id": client_id},
        ).one() == (None, 3)
    with pytest.raises(
        RuntimeError, match="initial managed-v2 schema cannot be downgraded"
    ):
        command.downgrade(config, "base")
    engine.dispose()

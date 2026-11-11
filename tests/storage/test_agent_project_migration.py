"""Project identity migration preserves predecessor rows and protects replay."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import AgentProjectRow
from kodi_lab.storage.repositories import DevelopmentSourceRepository
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates


def test_additive_project_intake_and_used_history_refuses_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0009_trusted_managed_lab"
    )
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    admin = McpAdministrationService(sessions)
    client = admin.create_client(
        display_name="Migration agent", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    now = datetime.now(UTC)
    source_id = uuid4()
    with sessions.begin() as session:
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=source_id,
                logical_name="Predecessor source",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="script.predecessor",
                addon_id="script.predecessor",
                observed_name="Predecessor",
                observed_version="0.1.0",
                extension_types=("xbmc.python.script",),
                registered_at=now,
                validated_at=now,
            )
        )
    with engine.connect() as connection:
        before = {
            name: list(connection.execute(text(f'SELECT * FROM "{name}"')))
            for name in inspect(connection).get_table_names()
            if name != "alembic_version"
        }
    command.upgrade(config, "0010_agent_projects")
    with engine.connect() as connection:
        for name, rows in before.items():
            assert list(connection.execute(text(f'SELECT * FROM "{name}"'))) == rows
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, "0009_trusted_managed_lab")
    command.upgrade(config, "head")
    with sessions.begin() as session:
        session.add(
            AgentProjectRow(
                source_id=str(source_id),
                client_id=str(client.id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="reserved",
                created_at=now.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
            )
        )
    with sessions.begin() as session:
        row = session.get(AgentProjectRow, str(source_id))
        assert row is not None
        row.parameter_fingerprint = "b" * 64
        with pytest.raises(IntegrityError, match="identity_immutable"):
            session.flush()
        session.rollback()
    with pytest.raises(RuntimeError, match="project history"):
        command.downgrade(config, "0009_trusted_managed_lab")
    engine.dispose()

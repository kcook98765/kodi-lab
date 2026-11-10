"""Additive predecessor upgrade preserves authority, profiles and remote history."""

from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.application.mcp_remote_actions import McpRemoteActionReceiptService
from kodi_lab.development.navigation import DevelopmentNavigationBinding
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import McpRemoteActionReceiptRow
from tests.application.test_mcp_remote_actions import NOW, FakeLimiter, FakeRemote
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates

OLD, NEW = "0007_production_navigation", "0008_development_navigation"


def binding() -> DevelopmentNavigationBinding:
    return DevelopmentNavigationBinding(
        session_generation=1,
        workspace_id=str(uuid4()),
        source_id=str(uuid4()),
        workspace_generation=4,
        deployment_id=str(uuid4()),
        addon_id="script.fixture",
        addon_version="1.0",
        tree_digest="a" * 64,
        destination_digest="b" * 64,
        runtime_revision_id=str(uuid4()),
        runtime_state_revision=4,
        container_id="c" * 64,
        bridge_generation=1,
        window_id=10001,
        control_id=50,
    )


def test_upgrade_preserves_all_existing_rows_schema_triggers_and_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    old_id = str(uuid4())
    with sessions.begin() as db:
        db.add(
            McpRemoteActionReceiptRow(
                id=old_id,
                client_id=str(uuid4()),
                credential_id=str(uuid4()),
                target_id=str(uuid4()),
                request_id=str(uuid4()),
                action="select",
                created_at="2026-09-19T12:00:00.000000Z",
                state="pending",
            )
        )
    with engine.connect() as db:
        tables = set(inspect(db).get_table_names()) - {"alembic_version"}
        before = {
            table: db.execute(text(f'SELECT * FROM "{table}"')).all()
            for table in tables
        }
        schema = dict(
            db.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name!='alembic_version'"
                )
            )
            .tuples()
            .all()
        )
    command.upgrade(config, NEW)
    with engine.connect() as db:
        assert set(inspect(db).get_table_names()) - tables == {
            "alembic_version",
            "mcp_development_navigation_bindings",
        }
        for table, rows in before.items():
            assert db.execute(text(f'SELECT * FROM "{table}"')).all() == rows
        after = {
            name: sql
            for name, sql in db.execute(
                text("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
            )
        }
        assert all(after[name] == sql for name, sql in schema.items())
        assert db.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    receipts = McpRemoteActionReceiptService(
        sessions, FakeRemote(), FakeLimiter(), clock=lambda: NOW
    )
    context = binding()
    receipts.invoke(
        client_id=uuid4(),
        credential_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
        action=BrowserRemoteAction.DOWN,
        development_binding=context,
    )
    with engine.connect() as db:
        row = (
            db.execute(text("SELECT * FROM mcp_development_navigation_bindings"))
            .mappings()
            .one()
        )
        assert all(row[name] == value for name, value in asdict(context).items())
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(config, OLD)
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert (
            db.scalar(
                text("SELECT state FROM mcp_remote_action_receipts WHERE id=:id"),
                {"id": old_id},
            )
            == "pending"
        )
    engine.dispose()


def test_empty_upgrade_downgrade_is_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    command.downgrade(config, OLD)
    engine = create_database_engine(url)
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == OLD
        assert (
            "mcp_development_navigation_bindings" not in inspect(db).get_table_names()
        )
        assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()

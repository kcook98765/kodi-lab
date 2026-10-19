"""Existing-registration reconnect runs on the upgraded durable schema."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text

from kodi_lab.application.external_reconnect import ReconnectStartRequest
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.metadata import Base
from tests.application import test_external_onboarding as onboarding_fixtures
from tests.application import test_household_onboarding as household_fixtures
from tests.application.test_external_reconnect import _decision, _revoked, _service
from tests.application.test_household_onboarding import Environment
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

REVISION = "0075_external_reconnect"
PREDECESSOR = "0074_remote_observations"
env = household_fixtures.env
live_application = household_fixtures.live_application


def test_empty_reconnect_upgrade_preserves_tables_and_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    before = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        triggers = connection.execute(
            text(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        ).all()
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == before | {
        "external_reconnect_operations"
    }
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name"
                )
            ).all()
            == triggers
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()


@pytest.fixture
def upgraded_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, Engine]]:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    monkeypatch.setattr(onboarding_fixtures, "create_engine", lambda _url: engine)
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    yield config, engine
    engine.dispose()


@pytest.fixture
def migrated_environment(
    upgraded_schema: tuple[Any, Engine], env: Environment
) -> tuple[Environment, Any, Engine]:
    config, engine = upgraded_schema
    return env, config, engine


def test_real_reconnect_receipt_on_migrated_schema_refuses_lossy_downgrade(
    migrated_environment: tuple[Environment, Any, Engine],
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    environment, config, engine = migrated_environment
    target_id, code = _revoked(environment)
    service = _service(environment, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    result = service.resume(target_id, operation_id, _decision(first, code))
    assert result.state == "connected" and result.target_id == target_id
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM external_reconnect_operations")
            )
            == 1
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []

"""Session admission runs on real upgraded bytes and refuses evidence loss."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine
from tests.application.test_development_sessions import environment
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

PREDECESSOR = "0079_native_zip_installation"
REVISION = "0080_development_sessions"


def test_empty_session_migration_is_exact_and_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    prior = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == prior | {
        "development_sessions",
        "development_session_policy",
        "managed_provision_reservations",
    }
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == prior
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_real_session_reservation_and_broker_binding_on_migrated_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    path = Path(url.removeprefix("sqlite:///"))
    state = environment(tmp_path / "case", monkeypatch, database_path=path)
    reserved = state.service.reserve(state.request)
    assert reserved.generation == 1
    state.provision()
    assert state.service.status().phase == "bound"
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with state.env.sessions() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        )
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    assert state.service.reserve(state.request).phase == "bound"

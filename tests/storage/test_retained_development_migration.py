"""Exact 0088 upgrade preserves legacy selection; used policy cannot disappear."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_retained_development import environment
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

OLD = "0088_service_enable_start"
NEW = "0089_retained_development"


def test_legacy_selection_profiles_and_evidence_survive_reversible_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    path = Path(url.removeprefix("sqlite:///"))
    env = fixture._environment(tmp_path / "legacy", database_path=path)
    engine = create_database_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO development_sessions "
                "(generation,request_id,target_id,runtime_revision_id,workspace_id,"
                "source_id,container_id,phase,created_at,updated_at) "
                "VALUES (12,:request,:target,:revision,:workspace,:source,:container,"
                "'bound',:now,:now)"
            ),
            dict(
                request=str(uuid4()),
                target=str(fixture.TARGET_ID),
                revision=str(fixture.RUNTIME_REVISION_ID),
                workspace=str(env.workspace_id),
                source=str(fixture.SOURCE_ID),
                container=fixture.CONTAINER_ID,
                now=fixture.NOW_TEXT,
            ),
        )
        c.execute(
            text(
                "INSERT INTO development_session_policy "
                "(id,active_generation) VALUES (1,12)"
            )
        )
        tables = inspect(c).get_table_names()
        before = {
            t: c.execute(text(f'SELECT * FROM "{t}"')).all()
            for t in tables
            if t != "alembic_version"
        }
    command.upgrade(config, NEW)
    with engine.connect() as c:
        for t, rows in before.items():
            if t == "development_session_policy":
                assert (
                    c.execute(
                        text(
                            "SELECT id,active_generation "
                            "FROM development_session_policy"
                        )
                    ).all()
                    == rows
                )
            else:
                assert c.execute(text(f'SELECT * FROM "{t}"')).all() == rows
        assert c.execute(
            text(
                "SELECT retain_profiles,retained_omega_target_id "
                "FROM development_session_policy"
            )
        ).one() == (0, None)
        assert c.scalar(text("PRAGMA integrity_check")) == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    with engine.connect() as c:
        assert (
            c.scalar(text("SELECT active_generation FROM development_session_policy"))
            == 12
        )
        assert "development_selections" not in inspect(c).get_table_names()
    command.upgrade(config, NEW)
    engine.dispose()


def test_retained_admission_on_real_migration_and_history_blocks_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    # The current application requires the current schema. Exercise retained
    # admission there, then return to 0089 (no verification history exists)
    # before testing 0089's original used-history downgrade prohibition.
    command.upgrade(config, "head")
    s = environment(
        tmp_path / "retained",
        monkeypatch,
        database_path=Path(url.removeprefix("sqlite:///")),
    )
    for generation in ("omega", "nexus", "matrix", "omega"):
        s.choose(generation)
    command.downgrade(config, NEW)
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        command.downgrade(config, OLD)
    with s.env.sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert session.scalar(text("SELECT count(*) FROM development_selections")) == 4
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    with pytest.raises(IntegrityError):
        s.lease(s.ids["nexus"])
    # CHECK must reject SQL NULL, rather than treating it as an unknown truth.
    with s.env.sessions.begin() as session, pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE development_session_policy SET retained_omega_target_id=NULL")
        )

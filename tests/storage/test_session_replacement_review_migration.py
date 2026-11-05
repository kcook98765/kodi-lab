"""Real predecessor transition preserves captures and blocks review loss."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from tests.application.test_session_replacement_reviews import environment
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates
from tests.storage.session_review_fixtures import (
    cancel_historical_review,
    save_historical_review,
)

PREDECESSOR = "0001_initial_release"
REVISION = "0001_initial_release"


def test_empty_review_schema_adds_only_two_tables_and_reinitializations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    prior = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == prior | {
        "development_session_replacement_reviews",
        "development_session_evidence_holds",
    }
    assert set(inspect(engine).get_table_names()) == prior
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_populated_review_preserves_evidence_and_preserves_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, "head")
    env = environment(
        tmp_path / "case",
        monkeypatch,
        database_path=Path(url.removeprefix("sqlite:///")),
    )
    request = env.request()
    before = env.retained.read(env.capture.capture_id)
    command.upgrade(config, REVISION)
    review = save_historical_review(env, request=request)
    assert env.retained.read(env.capture.capture_id) == before
    with env.state.env.sessions() as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM development_session_evidence_holds h "
                    "JOIN development_session_replacement_reviews "
                    "r ON r.id=h.review_id "
                    "WHERE r.state='open' AND h.capture_id=:id"
                ),
                dict(id=str(env.capture.capture_id)),
            )
            == 1
        )
    for statement, params in (
        (
            "DELETE FROM managed_diagnostic_captures WHERE id=:id",
            {"id": str(env.capture.capture_id)},
        ),
        ("DELETE FROM development_sessions WHERE generation=1", {}),
        (
            "UPDATE development_session_replacement_reviews "
            "SET snapshot_json=:body WHERE id=:id",
            {"id": str(review.review_id), "body": "😀" * 5000},
        ),
    ):
        with pytest.raises(IntegrityError), env.state.env.sessions.begin() as session:
            session.execute(text(statement), params)
    for cancel in (False, True):
        if cancel:
            cancel_historical_review(env, review.review_id)
        with env.state.env.sessions() as session:
            assert (
                session.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION
            )
    command.upgrade(config, "head")
    assert env.state.service.status().phase == "bound"
    assert env.retained.read(env.capture.capture_id) == before

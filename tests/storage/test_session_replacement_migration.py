"""Real 0083-to-0084 handoff, preserving historical holds and global exclusion."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.session_replacements import SessionReplacementRequest
from kodi_lab.storage.engine import create_database_engine
from tests.application.test_runtime_retirement_receipts import (
    build as predecessor_build,
)
from tests.migration_support import migration_setup
from tests.migration_support import migration_templates as migration_templates
from tests.storage.session_review_fixtures import (
    load_historical_review,
    save_historical_review,
)

PREDECESSOR = "0001_initial_release"
REVISION = "0001_initial_release"


def test_empty_replacement_schema_adds_one_nullable_binding_and_one_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    prior = set(inspect(engine).get_table_names())
    columns = {
        item["name"]
        for item in inspect(engine).get_columns(
            "development_session_replacement_reviews"
        )
    }
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == prior | {
        "development_session_replacements"
    }
    assert {
        item["name"]
        for item in inspect(engine).get_columns(
            "development_session_replacement_reviews"
        )
    } == columns | {"successor_generation"}
    assert set(inspect(engine).get_table_names()) == prior
    assert {
        item["name"]
        for item in inspect(engine).get_columns(
            "development_session_replacement_reviews"
        )
    } == columns
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_existing_review_and_capture_holds_survive_reinitialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, "head")
    # Populate the predecessor using its actual columns, without teaching the
    # current application to accept an outdated database.
    case = predecessor_build(
        tmp_path / "case",
        monkeypatch,
        database_path=Path(url.removeprefix("sqlite:///")),
    )
    try:
        review = save_historical_review(case.env)
        evidence = case.env.retained.read(case.env.capture.capture_id)
        command.upgrade(config, REVISION)
        assert case.env.reviews.get(review.review_id) == review
        assert load_historical_review(case.env, review.review_id) == review
        command.upgrade(config, REVISION)
        assert case.env.reviews.get(review.review_id) == review
        assert case.env.retained.read(case.env.capture.capture_id) == evidence
        with case.sessions() as session:
            assert (
                session.scalar(
                    text("SELECT count(*) FROM development_session_evidence_holds")
                )
                == 1
            )
    finally:
        case.readiness.close()


def test_upgraded_predecessor_runs_real_scoped_lifecycle_and_atomic_successor_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, "head")
    # Build ordinary current-data fixtures on the predecessor before creating a
    # review; the review itself is the exact historical schema fixture.
    from typing import Any, cast

    from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
    from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
    from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
    from kodi_lab.application.session_replacements import SessionReplacementService
    from tests.application.test_session_replacement_reviews import environment
    from tests.application.test_session_replacements import Broker

    env = environment(
        tmp_path / "case",
        monkeypatch,
        database_path=Path(url.removeprefix("sqlite:///")),
    )
    review = save_historical_review(env)
    before = env.retained.read(env.capture.capture_id)
    command.upgrade(config, REVISION)
    assert load_historical_review(env, review.review_id) == review
    assert env.retained.read(env.capture.capture_id) == before
    command.upgrade(config, "head")
    broker = Broker(env.state)
    readiness = ReadinessTaskRegistry(max_workers=1)
    lifecycle = ManagedRuntimeLifecycleService(
        env.state.env.sessions,
        cast(Any, broker),
        readiness,
        ManagedRuntimeStatusService(env.state.env.sessions, broker, readiness),
    )
    service = SessionReplacementService(env.state.service, lifecycle)
    try:
        request = SessionReplacementRequest(
            request_id=uuid4(),
            expected_snapshot_sha256=review.snapshot_sha256,
            confirm_profile_disposal=True,
        )
        result = service.execute(review.review_id, request)
        assert result.successor is not None
        assert result.phase == "successor_bound" and result.successor.generation == 2
        assert env.retained.read(env.capture.capture_id) == before
        for statement in (
            "DELETE FROM development_sessions WHERE generation=2",
            "DELETE FROM managed_runtime_retirements",
            "UPDATE development_session_replacement_reviews SET successor_generation=1",
        ):
            with (
                pytest.raises(IntegrityError),
                env.state.env.sessions.begin() as session,
            ):
                session.execute(text(statement))
        with env.state.env.sessions() as session:
            assert (
                session.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        command.upgrade(config, "head")
        assert service.get(review.review_id) == result
    finally:
        readiness.close()

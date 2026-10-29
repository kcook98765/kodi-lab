"""Actual 0084-to-0085 transition and durable nonce-history constraints."""

from pathlib import Path
from typing import cast

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.session_source_copy import SessionSourceCopyService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    SessionSourceCopyAdmissionRow,
    TargetRow,
)
from tests.application import test_development_source_deployments as fixtures
from tests.application.test_development_source_deployments import factory as factory
from tests.application.test_session_source_copy import bind
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

PREDECESSOR = "0084_session_replacements"
REVISION = "0085_session_source_copy_admissions"


def test_empty_transition_is_additive_and_reversible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == before | {
        "session_source_copy_admissions"
    }
    foreign_keys = inspect(engine).get_foreign_keys("session_source_copy_admissions")
    assert [
        (fk["constrained_columns"], fk["referred_table"]) for fk in foreign_keys
    ] == [(["session_generation"], "development_sessions")]
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_populated_predecessor_produces_atomic_admission_and_retains_history(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision=PREDECESSOR
    )
    engine = create_database_engine(url)
    migrated = create_session_factory(engine)
    bind(factory)
    # Copy data only into the actual populated predecessor. Never create ORM
    # schema over it or call the new producer against the old revision.
    models = (
        RuntimeDefinitionRow,
        RuntimeRevisionRow,
        RuntimeExecutionProfileRow,
        TargetRow,
        ManagedInstanceRow,
        DevelopmentSourceRow,
        ManagedInstanceDevelopmentSourceRow,
        AgentWorkspaceRow,
        DevelopmentSessionRow,
        DevelopmentSessionPolicyRow,
    )
    with factory() as source, migrated.begin() as destination:
        for model in models:
            table = cast(Table, model.__table__)
            historical = Table(
                table.name, MetaData(), autoload_with=destination.connection()
            )
            for values in source.execute(select(table)).mappings():
                # Insert only columns that really exist on the predecessor.
                # Retained-policy fields must still be their unused defaults.
                if model is DevelopmentSessionPolicyRow:
                    assert values["retain_profiles"] is False
                    assert values["retained_omega_target_id"] is None
                destination.execute(
                    historical.insert().values(
                        **{k: v for k, v in values.items() if k in historical.c}
                    )
                )
    command.upgrade(config, REVISION)
    command.upgrade(config, "head")
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(migrated, tmp_path, broker, fixtures.Clock())
    service = SessionSourceCopyService(migrated, coordinator)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    assert receipt.operation is not None and receipt.operation.state.value == "queued"
    statements = (
        "DELETE FROM development_sessions WHERE generation=1",
        "UPDATE session_source_copy_admissions SET session_generation=99",
        "UPDATE session_source_copy_admissions SET container_id='invalid'",
        "UPDATE session_source_copy_admissions SET requested_at='invalid'",
        "UPDATE session_source_copy_admissions SET operation_id='invalid'",
    )
    for sql in statements:
        with pytest.raises(IntegrityError), migrated.begin() as session:
            session.execute(text(sql))
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with migrated() as session:
        row = session.get(SessionSourceCopyAdmissionRow, str(receipt.request_id))
        assert row is not None and row.container_id == fixtures.CONTAINER_ID
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        )
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(config, "head")
    assert (
        service.admit(request_id=receipt.request_id, expected_generation=1) == receipt
    )
    assert not broker.calls
    engine.dispose()

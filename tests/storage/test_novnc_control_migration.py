"""Predecessor transition, mutual exclusion and lossless downgrade refusal."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates
from tests.web.conftest import INSTANCE_ID, REVISION_ID, seed_instance


def test_novnc_predecessor_transition_has_real_arbitration_and_refuses_active_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0086_addon_settings"
    )
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    from sqlalchemy import event

    from kodi_lab.storage.models import ProductionMutationControlRow

    def omit_existing(session: object, context: object, instances: object) -> None:
        from sqlalchemy.orm import Session

        assert isinstance(session, Session)
        for row in list(session.new):
            if isinstance(row, ProductionMutationControlRow):
                session.expunge(row)

    event.listen(sessions, "before_flush", omit_existing)
    seed_instance(sessions)
    event.remove(sessions, "before_flush", omit_existing)
    prior = set(inspect(engine).get_table_names())
    command.upgrade(config, "0087_novnc_control")
    assert set(inspect(engine).get_table_names()) == prior | {
        "novnc_sessions",
        "managed_remote_guards",
    }
    ownership = HumanViewerOwnership(sessions)
    attempt = uuid4()
    owner = "c" * 64
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "start", (str(REVISION_ID), 1)
    )
    ownership.create(INSTANCE_ID, attempt, owner, binding, "interactive")
    with pytest.raises(IntegrityError), sessions.begin() as db:
        db.execute(
            text(
                "INSERT INTO managed_remote_guards "
                "(instance_id,invocation_id,process_id) "
                "VALUES (:instance,:invocation,:invocation)"
            ),
            dict(instance=str(INSTANCE_ID), invocation=str(uuid4())),
        )
    with pytest.raises(RuntimeError, match="ownership"):
        command.downgrade(config, "0086_addon_settings")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0087_novnc_control"
        )
        assert connection.scalar(text("SELECT count(*) FROM novnc_sessions")) == 1
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    ownership.revoke(INSTANCE_ID, attempt, owner)
    command.downgrade(config, "0086_addon_settings")
    assert set(inspect(engine).get_table_names()) == prior
    command.upgrade(config, "0087_novnc_control")
    engine.dispose()

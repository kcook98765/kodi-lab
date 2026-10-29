"""Direct predecessor, conservative backfill and lossless downgrade authority."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ProductionMutationControlRow
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates
from tests.web.conftest import INSTANCE_ID, REVISION_ID, seed_instance
from tests.web.test_development_sources import SOURCE_A, _seed_sources


def test_service_start_authority_backfill_and_downgrade_keep_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0087_novnc_control"
    )
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)

    def omit_seeded_control(
        session: Session, _context: object, _instances: object
    ) -> None:
        for row in list(session.new):
            if isinstance(row, ProductionMutationControlRow):
                session.expunge(row)

    event.listen(sessions, "before_flush", omit_seeded_control)
    seed_instance(sessions)
    event.remove(sessions, "before_flush", omit_seeded_control)
    _seed_sources(sessions)
    operation = str(uuid4())
    insert = text(
        "INSERT INTO development_source_enablement_operations "
        "(id,original_managed_instance_id,managed_instance_id,development_source_id,"
        "source_logical_name,addon_id,addon_version,runtime_revision_id,requested_at,started_at,"
        "state,phase,enable_performed,reconciliation_required,"
        "result_classification,completed_at,failure_reason) "
        "VALUES (:id,:target,:target,:source,'Fixture','plugin.video.alpha',"
        "'1.2.3',:revision,"
        "'2026-10-28T00:00:00.000000Z','2026-10-28T00:00:00.000000Z',"
        "'reconciliation_required','finalize',1,1,'reconciliation_required',"
        "'2026-10-28T00:00:00.000000Z','fixture_ambiguous')"
    )
    with engine.begin() as connection:
        connection.execute(
            insert,
            dict(
                id=operation,
                target=str(INSTANCE_ID),
                source=str(SOURCE_A),
                revision=str(REVISION_ID),
            ),
        )
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService

    legacy_status = ManagedInstanceReconciliationService(sessions).status(INSTANCE_ID)
    assert legacy_status.blocking
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "0088_service_enable_start")
    assert set(inspect(engine).get_table_names()) == before
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT service_start_authorized FROM "
                    "development_source_enablement_operations WHERE id=:id"
                ),
                dict(id=operation),
            )
            == 0
        )
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE development_source_enablement_operations "
                "SET service_start_authorized=1 WHERE id=:id"
            ),
            dict(id=operation),
        )
    with pytest.raises(RuntimeError, match="authority history"):
        command.downgrade(config, "0087_novnc_control")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0088_service_enable_start"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT service_start_authorized FROM "
                    "development_source_enablement_operations WHERE id=:id"
                ),
                dict(id=operation),
            )
            == 1
        )
    # Remove only this synthetic operation after verifying refusal/preservation.
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM development_source_enablement_operations WHERE id=:id"),
            dict(id=operation),
        )
    command.downgrade(config, "0087_novnc_control")
    assert "service_start_authorized" not in {
        column["name"]
        for column in inspect(engine).get_columns(
            "development_source_enablement_operations"
        )
    }
    command.upgrade(config, "0088_service_enable_start")
    engine.dispose()

"""Real scoped-retirement migration preserves unscoped history and review holds."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.runtime_lifecycle import LifecycleDispatchUnconfirmedError
from kodi_lab.application.runtime_retirement_receipts import (
    RuntimeRetirementReceiptError,
    read_retirement_receipt,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import ManagedRuntimeRetirementRow
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_runtime_retirement_receipts import build, scope
from tests.storage.session_review_fixtures import (
    load_historical_review,
    save_historical_review,
)
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

PREDECESSOR = "0082_session_replacement_reviews"
REVISION = "0083_managed_runtime_retirements"


def test_empty_retirement_migration_adds_only_one_table_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    prior = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == prior | {
        "managed_runtime_retirements"
    }
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == prior
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


@pytest.mark.parametrize("confirmed", [False, True])
def test_populated_predecessor_preserves_unscoped_history_and_exact_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    # Use a populated predecessor, not ORM metadata, for the receipt and lease path.
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, "head")
    case = build(
        tmp_path / "case",
        monkeypatch,
        database_path=Path(url.removeprefix("sqlite:///")),
    )
    try:
        request = case.env.request()
        command.downgrade(config, PREDECESSOR)
        review = save_historical_review(case.env, request=request)
        capture = case.env.retained.read(case.env.capture.capture_id)
        old_operation = ManagedInstanceLifecycleOperation(
            id=uuid4(),
            original_managed_instance_id=fixture.TARGET_ID,
            managed_instance_id=fixture.TARGET_ID,
            instance_name="Historical unscoped removal",
            kind=ManagedInstanceLifecycleKind.DEPROVISION,
            requested_at=fixture.NOW,
            started_at=fixture.NOW,
            completed_at=fixture.NOW + timedelta(seconds=1),
            prior_state=ManagedInstanceSemanticState.STOPPED,
            resulting_state=ManagedInstanceSemanticState.UNPROVISIONED,
            state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
        )
        with case.sessions.begin() as session:
            ManagedInstanceLifecycleOperationRepository(session).create(old_operation)
        command.upgrade(config, REVISION)
        with case.sessions() as session:
            assert session.scalar(select(ManagedRuntimeRetirementRow)) is None
            assert (
                ManagedInstanceLifecycleOperationRepository(session).get(
                    old_operation.id
                )
                == old_operation
            )
        with pytest.raises(RuntimeRetirementReceiptError, match="receipt_not_found"):
            read_retirement_receipt(case.sessions, old_operation.id)
        # The populated 0082->0083 proof above remains historical. Current
        # lifecycle code runs only after the complete schema upgrade.
        command.upgrade(config, "head")
        operation_id = uuid4()
        if confirmed:
            case.lifecycle.deprovision(
                fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
            )
        else:
            case.broker.mode = "lost_reply"
            with pytest.raises(LifecycleDispatchUnconfirmedError):
                case.lifecycle.deprovision(
                    fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
                )
        receipt = read_retirement_receipt(case.sessions, operation_id)
        assert (receipt.confirmation == "confirmed") is confirmed
        assert load_historical_review(case.env, review.review_id) == review
        assert case.env.retained.read(case.env.capture.capture_id) == capture
        for statement, params in (
            (
                "DELETE FROM managed_instance_lifecycle_operations WHERE id=:id",
                {"id": str(operation_id)},
            ),
            (
                "UPDATE managed_runtime_retirements SET container_id='unsafe' "
                "WHERE operation_id=:id",
                {"id": str(operation_id)},
            ),
        ):
            with pytest.raises(IntegrityError), case.sessions.begin() as session:
                session.execute(text(statement), params)
        with pytest.raises(
            RuntimeError, match="retirement history cannot be safely downgraded"
        ):
            command.downgrade(config, PREDECESSOR)
        with case.sessions() as session:
            assert (
                session.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION
            )
        assert read_retirement_receipt(case.sessions, operation_id) == receipt
    finally:
        case.readiness.close()

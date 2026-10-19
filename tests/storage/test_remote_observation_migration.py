"""Optional action observations preserve old receipts on the migrated schema."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.application.browser_remote import ScreenshotRefreshResult
from kodi_lab.application.mcp_remote_actions import McpRemoteActionResult
from kodi_lab.application.remote_observations import (
    RemoteObservationService,
    RuntimeObservationBinding,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.ui_state import UiObservation, WindowState
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

REVISION = "0074_remote_observations"
PREDECESSOR = "0073_workspace_imports"
NOW = datetime(2026, 10, 19, tzinfo=UTC)
STAMP = "2026-10-19T00:00:00.000000Z"
RECEIPT = "73000000-0000-4000-8000-000000000001"
TARGET = "73000000-0000-4000-8000-000000000002"
REQUEST = "73000000-0000-4000-8000-000000000003"


def test_empty_observation_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == before | {
        "mcp_remote_observations"
    }
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(config, REVISION)
    engine.dispose()


def test_observation_service_on_upgraded_schema_preserves_receipt_and_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mcp_remote_action_receipts "
                "(id,client_id,credential_id,target_id,request_id,action,created_at,state,"
                "final_result,completed_at,screenshot_refresh_result) VALUES "
                "(:id,:id,:id,:target,:request,'left',:stamp,'completed','succeeded',:stamp,'not_requested')"
            ),
            {"id": RECEIPT, "target": TARGET, "request": REQUEST, "stamp": STAMP},
        )
    command.upgrade(config, REVISION)
    service = RemoteObservationService(
        create_session_factory(engine), cast(Any, None), clock=lambda: NOW
    )
    binding = RuntimeObservationBinding(RECEIPT, 4, "a" * 64)
    monkeypatch.setattr(service, "identity", lambda _target: binding)
    result = McpRemoteActionResult(
        invocation_id=UUID(RECEIPT),
        request_id=UUID(REQUEST),
        target_id=UUID(TARGET),
        action=BrowserRemoteAction.LEFT,
        outcome="succeeded",
        created_at=NOW,
        completed_at=NOW,
        screenshot_refresh=ScreenshotRefreshResult(result="not_requested"),
    )
    calls: list[int] = []

    def collect() -> UiObservation:
        calls.append(1)
        return UiObservation(window=WindowState(id=10025), summary="Videos")

    first = service.complete(
        result,
        new_action=True,
        before=binding,
        inspect=collect,
        authorize_inspect=lambda: None,
    )
    assert first.status == "succeeded"
    assert first.ui_state is not None and first.ui_state.window.id == 10025
    assert (
        service.complete(
            result,
            new_action=False,
            before=None,
            inspect=collect,
            authorize_inspect=lambda: None,
        )
        == first
    )
    assert calls == [1]
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with engine.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT final_result FROM mcp_remote_action_receipts")
            )
            == "succeeded"
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        # Observation rows follow the existing receipt-retention boundary.
        connection.execute(
            text("DELETE FROM mcp_remote_action_receipts WHERE id=:id"), {"id": RECEIPT}
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM mcp_remote_observations")) == 0
        )
    command.downgrade(config, PREDECESSOR)
    engine.dispose()

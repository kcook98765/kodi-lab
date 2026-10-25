"""Real 0080-to-0081 transition preserves uncertainty and retained evidence."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.retained_diagnostics import (
    DiagnosticCaptureInput,
    RetainedDiagnosticsService,
)
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import RuntimeContainerArtifact
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.models import ManagedProvisionReservationRow
from kodi_lab.storage.repositories import RuntimeContainerArtifactRepository
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_retained_diagnostics import NOW, snapshot
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

PREDECESSOR = "0080_development_sessions"
REVISION = "0081_managed_diagnostic_captures"


def test_empty_transition_adds_only_capture_table_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    prior = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == prior | {
        "managed_diagnostic_captures"
    }
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == prior
    command.upgrade(config, REVISION)
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_upgraded_capture_preserves_pending_provision_and_refuses_lossy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    case = tmp_path / "case"
    env = fixtures._environment(
        case, database_path=Path(url.removeprefix("sqlite:///"))
    )
    pending = str(uuid4())
    with env.sessions.begin() as session:
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(
                runtime_revision_id=fixtures.RUNTIME_REVISION_ID,
                image_id=fixtures.IMAGE_ID,
            )
        )
        session.add(
            ManagedProvisionReservationRow(
                managed_instance_id=str(fixtures.TARGET_ID),
                runtime_revision_id=str(fixtures.RUNTIME_REVISION_ID),
                operation_id=pending,
                phase="uncertain",
                session_generation=None,
                created_at="2026-10-24T00:00:00.000000Z",
            )
        )
    command.upgrade(config, REVISION)
    logs = snapshot()
    logs.exit_metadata = logs.exit_metadata.model_copy(
        update={
            "managed_instance_id": str(fixtures.TARGET_ID),
            "runtime_revision_id": str(fixtures.RUNTIME_REVISION_ID),
            "container_id": fixtures.CONTAINER_ID,
            "image_id": fixtures.IMAGE_ID,
        }
    )

    def inventory(**_kwargs: object) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(fixtures.TARGET_ID),
                    runtime_revision_id=str(fixtures.RUNTIME_REVISION_ID),
                    container_id=fixtures.CONTAINER_ID,
                    image_id=fixtures.IMAGE_ID,
                    runtime_state=ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            ]
        )

    broker = SimpleNamespace(
        managed_container_inventory=inventory,
        read_stopped_managed_logs=lambda **_: logs,
    )
    service = RetainedDiagnosticsService(
        env.sessions, broker, data_root=case, clock=lambda: NOW
    )
    result = service.capture(
        fixtures.TARGET_ID,
        DiagnosticCaptureInput(
            request_id=uuid4(),
            expected_runtime_revision_id=fixtures.RUNTIME_REVISION_ID,
            expected_container_id=fixtures.CONTAINER_ID,
            retain_redacted_logs=True,
        ),
    )
    assert (
        result.state == "retained"
        and service.read(result.capture_id).logs.artifacts[0].content
    )
    with env.sessions() as session:
        reservation = session.get(
            ManagedProvisionReservationRow, str(fixtures.TARGET_ID)
        )
        assert (
            reservation is not None
            and reservation.operation_id == pending
            and reservation.phase == "uncertain"
        )
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with env.sessions() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        )
    assert (
        service.read(result.capture_id).capture.payload_sha256 == result.payload_sha256
    )
    with (
        pytest.raises(IntegrityError, match="diagnostic_capture_payload_bound"),
        env.sessions.begin() as session,
    ):
        session.execute(
            text(
                "UPDATE managed_diagnostic_captures SET payload_json=:body WHERE id=:id"
            ),
            {"body": "😀" * 13000, "id": str(result.capture_id)},
        )
    assert service.read(result.capture_id).capture.state == "retained"

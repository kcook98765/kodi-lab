"""Reviewed runtime admission uses broker identity and existing durable rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    REQUIRED_OPERATIONS,
    RuntimeInstallationBusy,
    RuntimeInstallationService,
)
from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository


class FakeBroker:
    def __init__(self) -> None:
        self.operation_id: str | None = None
        self.generation: str | None = None
        self.worker_state = "running"
        self.lock_held = False
        self.image_present = True
        self.launches: list[tuple[str, str]] = []

    def capabilities(self) -> SimpleNamespace:
        return SimpleNamespace(operations=list(REQUIRED_OPERATIONS))

    def active_reviewed_runtime_installation(self) -> SimpleNamespace:
        return SimpleNamespace(
            operation_id=self.operation_id,
            generation=self.generation,
            lock_held=self.lock_held,
        )

    def launch_reviewed_runtime_installation(
        self, *, generation: str, operation_id: str
    ) -> SimpleNamespace:
        self.operation_id = operation_id
        self.generation = generation
        self.launches.append((generation, operation_id))
        return SimpleNamespace(operation_id=operation_id, newly_accepted=True)

    def observe_reviewed_runtime_installation(
        self, _operation_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(state=self.worker_state, lock_held=self.lock_held)

    def reviewed_runtime_image_present(self, _operation_id: str) -> SimpleNamespace:
        return SimpleNamespace(present=self.image_present)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'installations.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def create_row(
    factory: sessionmaker[Session],
    generation: str,
    operation_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    reviewed = load_reviewed_recipe(generation)
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).create(
            operation_id=operation_id,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation=generation,
            now=now or datetime.now(UTC),
        )


def test_three_choices_admission_duplicate_busy_and_durable_phase(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    service = RuntimeInstallationService(factory, broker)
    initial = service.list()
    assert [(item.display_name, item.version, item.action) for item in initial] == [
        ("Kodi 19 — Matrix", "19.4", "install"),
        ("Kodi 20 — Nexus", "20.5", "install"),
        ("Kodi 21 — Omega", "21.3", "install"),
    ]
    accepted = service.start("matrix")
    assert accepted.newly_accepted and accepted.status == "pending"
    assert service.start("matrix").operation_id == accepted.operation_id
    assert len(broker.launches) == 1
    with pytest.raises(RuntimeInstallationBusy):
        service.start("nexus")

    operation_id = UUID(accepted.operation_id or "")
    create_row(factory, "matrix", operation_id)
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).advance(
            operation_id,
            expected=RuntimeRecipeInstallationState.PENDING,
            now=datetime.now(UTC),
        )
    assert service.get(operation_id).phase == "Acquiring runtime"  # type: ignore[union-attr]
    assert service.list()[0].operation_id == str(operation_id)


def test_missing_worker_reconciles_without_elapsed_time(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    service = RuntimeInstallationService(factory, broker)
    operation_id = uuid4()
    create_row(
        factory,
        "omega",
        operation_id,
        now=datetime.now(UTC) - timedelta(days=100),
    )
    broker.worker_state = "missing"
    broker.lock_held = True
    assert service.get(operation_id).status == "pending"  # type: ignore[union-attr]
    broker.lock_held = False
    failed = service.get(operation_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.action == "retry"
    assert "Check network access" in (failed.failure_message or "")
    assert service.start("omega", action="retry").newly_accepted


@pytest.mark.parametrize(
    "code,expected",
    [
        ("pinned_support_unavailable", "pinned availability"),
        ("support_artifact_mismatch", "exact verification"),
        ("support_download_bound", "exact verification"),
        ("acquisition_network_creation_failed", "acquisition network"),
        ("validation_network_creation_failed", "validation network"),
        ("docker_address_pool_exhausted", "acquisition network"),
        ("validation_failed", "did not pass validation"),
        ("installation_failed", "Check network access"),
    ],
)
def test_safe_phase_specific_failure_message(
    factory: sessionmaker[Session], code: str, expected: str
) -> None:
    operation_id = uuid4()
    create_row(factory, "nexus", operation_id)
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).fail(
            operation_id, code=code, now=datetime.now(UTC)
        )
    failed = RuntimeInstallationService(factory, FakeBroker()).get(operation_id)
    assert failed is not None and failed.status == "failed"
    assert expected in (failed.failure_message or "")
    assert failed.technical_details is not None
    assert failed.technical_details["failure_code"] == code


def test_independent_cli_lock_and_row_are_respected(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.lock_held = True
    service = RuntimeInstallationService(factory, broker)
    with pytest.raises(RuntimeInstallationBusy):
        service.start("matrix")
    operation_id = uuid4()
    create_row(factory, "matrix", operation_id)
    same = service.start("matrix")
    assert same.operation_id == str(operation_id)
    with pytest.raises(RuntimeInstallationBusy):
        service.start("nexus")
    assert broker.launches == []


def test_ready_noop_missing_image_and_no_silent_repin(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = FakeBroker()
    service = RuntimeInstallationService(factory, broker)
    operation_id = uuid4()
    revision_id = uuid4()
    row = SimpleNamespace(
        id=str(operation_id),
        generation="nexus",
        state=RuntimeRecipeInstallationState.READY,
        created_at="2026-09-27T23:50:00.000000Z",
        updated_at="2026-09-28T00:00:00.000000Z",
        completed_at="2026-09-28T00:00:00.000000Z",
        runtime_revision_id=str(revision_id),
        image_id="sha256:" + "a" * 64,
    )
    monkeypatch.setattr(service, "_latest", lambda _generation: row)
    monkeypatch.setattr(service, "_eligible_ready", lambda _row: True)
    monkeypatch.setattr(
        "kodi_lab.application.runtime_installations.ManagedInstanceRepository.get",
        lambda _self, _target: SimpleNamespace(runtime_revision_id=revision_id),
    )
    ready = service.start("nexus")
    assert ready.status == "ready" and ready.action == "open"
    assert ready.created_at == row.created_at
    assert ready.phase_started_at == row.updated_at
    assert ready.completed_at == row.completed_at
    assert ready.managed_instance_url.startswith("/instances/")  # type: ignore[union-attr]
    assert not broker.launches
    broker.image_present = False
    missing = service.list()[1]
    assert missing.status == "missing" and missing.action == "reacquire"
    reacquired = service.start("nexus", action="reacquire")
    assert reacquired.newly_accepted
    assert service.list()[1].operation_id == reacquired.operation_id
    assert service.list()[1].phase == "Preparing"
    assert row.runtime_revision_id == str(revision_id)
    assert broker.launches == [("nexus", reacquired.operation_id)]


@pytest.mark.parametrize(
    "kind,code",
    [
        ("missing_operations", "backend_capability_missing"),
        ("unknown_operation", "backend_capability_missing"),
        ("connection", "broker_unreachable"),
        ("worker", "worker_unavailable"),
    ],
)
def test_unavailable_reasons_do_not_launch_or_discard_records(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    code: str,
) -> None:
    broker = FakeBroker()
    operation_id = uuid4()
    create_row(factory, "omega", operation_id)
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).fail(
            operation_id, code="validation_failed", now=datetime.now(UTC)
        )

    def unavailable() -> SimpleNamespace:
        if kind == "unknown_operation":
            raise RemoteBrokerError(
                "unknown_operation", "Not supported", request_id=None
            )
        raise ConnectionError("private details must never be shown")

    if kind == "missing_operations":
        monkeypatch.setattr(
            broker, "capabilities", lambda: SimpleNamespace(operations=[])
        )
    elif kind == "worker":
        monkeypatch.setattr(broker, "active_reviewed_runtime_installation", unavailable)
    else:
        monkeypatch.setattr(broker, "capabilities", unavailable)
    items = RuntimeInstallationService(factory, broker).list()
    assert len(items) == 3
    assert all(
        item.unavailable_code == code and item.action == "none" for item in items
    )
    assert items[2].operation_id == str(operation_id)
    assert items[2].technical_details is not None
    assert items[2].technical_details["failure_code"] == "validation_failed"
    assert "private details" not in str(items)
    assert broker.launches == []
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None and row.failure_code == "validation_failed"


def test_unverified_record_and_image_probe_failure_are_distinct(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = FakeBroker()
    service = RuntimeInstallationService(factory, broker)
    operation_id = uuid4()
    create_row(factory, "omega", operation_id)
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None
        session.expunge(row)
        row.state = RuntimeRecipeInstallationState.READY
    invalid = service._view("omega", row, capable=True)
    assert invalid is not None and invalid.unavailable_code == "runtime_ineligible"
    assert invalid.action == "none"
    monkeypatch.setattr(service, "_eligible_ready", lambda _row: True)

    def unavailable(_id: str) -> SimpleNamespace:
        raise ConnectionError("private artifact details")

    monkeypatch.setattr(broker, "reviewed_runtime_image_present", unavailable)
    blocked = service._view("omega", row, capable=True)
    assert blocked is not None and blocked.unavailable_code == "image_check_unavailable"
    assert blocked.action == "none"
    assert broker.launches == []

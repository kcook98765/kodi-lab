"""Prerequisite snapshots observe existing authorities without admitting work."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_kodi_prerequisites import (
    DataStorageObservation,
    ManagedKodiPrerequisiteService,
    PrerequisiteCheck,
    StableGeneration,
    read_data_storage,
)
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.install import bootstrap_recipe_instance, recipe_target_id
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    ManagedProvisionReservationRow,
    ManagedResourcePolicyRow,
    ManagedStartReservationRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.application.test_runtime_installations import FakeBroker, create_row
from tests.runtime.test_recipe_installation import _ready
from tests.runtime.test_recipe_installation import factory as factory

NOW = datetime(2026, 11, 3, tzinfo=UTC)
STORAGE = DataStorageObservation(1024**3, False)


class Broker(FakeBroker):
    def __init__(self) -> None:
        super().__init__()
        self.containers: list[ManagedContainerInventoryEntry] = []
        self.inventory_error = False

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        if self.inventory_error:
            raise OSError("private broker address")
        return ManagedContainerInventoryResult(containers=self.containers)

    def observe_managed_kodi_runtime(self, **_kwargs: object) -> Any:
        pytest.fail("prerequisites must not use stateful runtime status")


class ForbiddenLifecycle:
    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"prerequisites called lifecycle: {name}")


def service(
    factory: sessionmaker[Session],
    tmp_path: Path,
    broker: Broker | None = None,
    *,
    storage: DataStorageObservation = STORAGE,
) -> ManagedKodiPrerequisiteService:
    broker = broker or Broker()
    return ManagedKodiPrerequisiteService(
        RuntimeInstallationService(factory, cast(Any, broker)),
        ManagedResourceService(factory, broker, cast(Any, ForbiddenLifecycle())),
        data_root=tmp_path,
        storage=lambda: storage,
        clock=lambda: NOW,
    )


def checks(
    value: ManagedKodiPrerequisiteService, generation: StableGeneration = "omega"
) -> dict[str, PrerequisiteCheck]:
    return {item.key: item for item in value.snapshot(generation).checks}


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_empty_recipe_is_information_not_install_authorization(
    factory: sessionmaker[Session], tmp_path: Path, generation: StableGeneration
) -> None:
    value = service(factory, tmp_path)
    snapshot = value.snapshot(generation)
    observed = checks(value, generation)
    assert snapshot.checked_at == NOW
    assert observed["recipe"].status == observed["installer"].status == "passed"
    assert observed["runtime"].status == "info"
    assert observed["instance"].status == "info"
    assert observed["capacity"].status == "passed"
    assert "0 of 4" in observed["capacity"].message
    assert {key for key, item in observed.items() if item.status == "unknown"} == {
        "docker_storage",
        "host_storage",
        "memory",
    }
    assert "authorization" not in snapshot.model_dump()
    assert "can_install" not in snapshot.model_dump()
    with factory() as session:
        assert session.scalars(select(ManagedResourcePolicyRow)).all() == []
        assert session.scalars(select(RuntimeRecipeInstallationRow)).all() == []


@pytest.mark.parametrize("present", [True, False])
def test_verified_existing_runtime_uses_eligibility_and_exact_image(
    factory: sessionmaker[Session], tmp_path: Path, present: bool
) -> None:
    operation = _ready(factory, generation="omega")
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
    reviewed = load_reviewed_recipe("omega")
    bootstrap_recipe_instance(factory, reviewed, revision)
    broker = Broker()
    broker.image_present = present
    observed = checks(service(factory, tmp_path, broker))
    assert observed["runtime"].status == ("passed" if present else "attention")
    assert observed["instance"].status == "existing"
    assert observed["instance"].detail_url == f"/instances/{recipe_target_id(reviewed)}"
    with factory() as session:
        saved = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert saved is not None and saved.state is RuntimeRecipeInstallationState.READY
    assert broker.launches == []


def test_untrusted_ready_record_does_not_claim_verified_runtime(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    operation = _ready(factory, generation="omega")
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None
        row.attestation_json = "{}"
    assert checks(service(factory, tmp_path))["runtime"].status == "attention"


@pytest.mark.parametrize("activity", ["same", "other", "none", "lock"])
def test_saved_pending_record_is_never_reconciled_or_retried(
    factory: sessionmaker[Session], tmp_path: Path, activity: str
) -> None:
    operation = uuid4()
    create_row(factory, "omega", operation)
    broker = Broker()
    if activity in {"same", "other"}:
        broker.operation_id = str(operation if activity == "same" else uuid4())
        broker.generation = "omega" if activity == "same" else "matrix"
    broker.lock_held = activity == "lock"
    value = service(factory, tmp_path, broker)
    for _ in range(3):
        observed = checks(value)
        assert observed["runtime"].status == (
            "existing" if activity == "same" else "attention"
        )
        assert observed["activity"].status == (
            "existing"
            if activity in {"same", "other"}
            else "attention"
            if activity == "lock"
            else "passed"
        )
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.state is RuntimeRecipeInstallationState.PENDING
        assert row.failure_code is None
    assert broker.launches == []


@pytest.mark.parametrize("failure", ["capability", "transport", "worker", "capacity"])
def test_missing_capabilities_are_distinct_from_unverified_observations(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    broker = Broker()

    def unavailable() -> Any:
        raise OSError("private broker address")

    if failure == "capability":
        monkeypatch.setattr(
            broker, "capabilities", lambda: SimpleNamespace(operations=[])
        )
    elif failure == "transport":
        monkeypatch.setattr(broker, "capabilities", unavailable)
    elif failure == "worker":
        monkeypatch.setattr(broker, "active_reviewed_runtime_installation", unavailable)
    else:
        broker.inventory_error = True
    observed = checks(service(factory, tmp_path, broker))
    if failure != "capacity":
        assert observed["installer"].status == (
            "attention" if failure == "capability" else "unknown"
        )
        assert observed["activity"].status == "unknown"
    else:
        assert observed["capacity"].status == "unknown"
    assert "private broker address" not in str(observed)


def test_saved_record_remains_visible_when_broker_cannot_be_verified(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_row(factory, "omega", uuid4())
    broker = Broker()
    monkeypatch.setattr(broker, "capabilities", lambda: SimpleNamespace(operations=[]))
    observed = checks(service(factory, tmp_path, broker))
    assert observed["runtime"].status == "existing"
    assert "could not be verified" in observed["runtime"].message
    assert observed["activity"].status == "unknown"
    assert observed["installer"].status == "attention"


@pytest.mark.parametrize("retained", [False, True])
def test_capacity_includes_running_containers_and_pending_start_reservations(
    factory: sessionmaker[Session], tmp_path: Path, retained: bool
) -> None:
    operation = _ready(factory, generation="omega")
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = row.runtime_revision_id
    reviewed = load_reviewed_recipe("omega")
    bootstrap_recipe_instance(factory, reviewed, UUID(revision))
    target = recipe_target_id(reviewed)
    with factory.begin() as session:
        session.add(
            ManagedResourcePolicyRow(
                id=1, max_running=4 if retained else 1, idle_minutes=60
            )
        )
        if retained:
            session.add(
                DevelopmentSessionPolicyRow(
                    id=1, retain_profiles=True, retained_omega_target_id=str(target)
                )
            )
            session.add(
                ManagedProvisionReservationRow(
                    managed_instance_id=str(target),
                    runtime_revision_id=revision,
                    operation_id=str(uuid4()),
                    phase="uncertain",
                    created_at="2026-11-03T00:00:00.000000Z",
                )
            )
        session.add(
            ManagedStartReservationRow(
                managed_instance_id=str(target),
                container_id="a" * 64,
                runtime_revision_id=revision,
                operation_id=str(uuid4()),
                phase="uncertain",
                created_at="2026-11-03T00:00:00.000000Z",
            )
        )
    broker = Broker()
    value = service(factory, tmp_path, broker)
    assert checks(value)["capacity"].status == "attention"
    broker.containers = [
        ManagedContainerInventoryEntry(
            managed_instance_id=str(target),
            container_id="a" * 64,
            image_id="sha256:" + "b" * 64,
            runtime_state=ContainerRuntimeState.RUNNING,
            health_state=ContainerHealthState.HEALTHY,
        )
    ]
    # The same pending start and running target consume one slot, not two.
    assert "1 of 1" in checks(value)["capacity"].message
    with factory() as session:
        assert len(session.scalars(select(ManagedStartReservationRow)).all()) == 1


@pytest.mark.parametrize(
    ("available", "read_only", "status"),
    [
        (1, False, "info"),
        (0, False, "attention"),
        (100, True, "attention"),
        (-1, False, "unknown"),
    ],
)
def test_storage_observations_do_not_invent_an_installation_minimum(
    factory: sessionmaker[Session],
    tmp_path: Path,
    available: int,
    read_only: bool,
    status: str,
) -> None:
    observed = checks(
        service(factory, tmp_path, storage=DataStorageObservation(available, read_only))
    )
    assert observed["data_storage"].status == status
    for key in ("docker_storage", "host_storage", "memory"):
        assert observed[key].status == "unknown"
    if available == 1:
        assert observed["data_storage"].available_bytes == 1
        assert "guarantee" in observed["data_storage"].message


@pytest.mark.parametrize("error", [OSError, AttributeError, NotImplementedError])
def test_storage_unavailable_hides_internal_paths(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    def unavailable(_root: Path) -> DataStorageObservation:
        raise error("/private/host/path")

    monkeypatch.setattr(
        "kodi_lab.application.managed_kodi_prerequisites.read_data_storage", unavailable
    )
    value = ManagedKodiPrerequisiteService(
        RuntimeInstallationService(factory, cast(Any, Broker())),
        cast(Any, SimpleNamespace(capacity=lambda: (_ for _ in ()).throw(OSError()))),
        data_root=tmp_path,
    ).snapshot()
    assert next(c for c in value.checks if c.key == "data_storage").status == "unknown"
    assert "/private/host/path" not in value.model_dump_json()


def test_fixed_data_filesystem_is_read_without_creating_files(tmp_path: Path) -> None:
    before = list(tmp_path.iterdir())
    assert read_data_storage(tmp_path).available_bytes >= 0
    assert list(tmp_path.iterdir()) == before
    link = tmp_path / "alias"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        read_data_storage(link)


def test_repeated_snapshots_issue_no_database_writes_or_stateful_calls(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_row(factory, "omega", uuid4())
    for name in ("_reconcile", "start", "get", "list"):
        monkeypatch.setattr(
            RuntimeInstallationService,
            name,
            lambda *_args, **_kwargs: pytest.fail("stateful installation helper"),
        )
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        broker = Broker()
        value = service(factory, tmp_path, broker)
        for _ in range(3):
            value.snapshot()
        assert broker.launches == []
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not [
        sql
        for sql in statements
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql, re.I)
    ]

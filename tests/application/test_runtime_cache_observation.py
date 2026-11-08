"""Committed runtime caches outlive the application worker that produced them."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_versions import ManagedVersionError
from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.broker.reviewed_installations import DockerReviewedInstallationWorker
from kodi_lab.domain import RevisionApprovalState, RuntimeRecipeInstallationState
from kodi_lab.storage.models import RuntimeRecipeInstallationRow, RuntimeRevisionRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from kodi_lab.web.managed_slots import ManagedSlotsWeb
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_create_kodi_installation import setup
from tests.application.test_managed_clean_reset import env as env
from tests.application.test_managed_clean_reset import reset
from tests.application.test_runtime_installations import create_row
from tests.broker.test_reviewed_runtime_installation_operation import FakeContainers
from tests.broker.test_reviewed_runtime_installation_operation import backend as backend
from tests.managed_versions_support import Environment
from tests.runtime.test_recipe_installation import _ready


def test_ready_cache_survives_a_broker_image_upgrade(
    factory: sessionmaker[Session],
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, broker, installer = setup(factory)
    operation = _ready(factory, generation="omega")
    worker, containers = backend
    worker.launch("omega", operation)
    historical = containers.items[0]
    historical.attrs["State"] = {"Running": False, "ExitCode": 0}
    previous_image = historical.attrs["Image"]

    # Replace only the application's broker-image context, retaining the old
    # successful worker and all of its exact ownership/security metadata.
    client = worker._client()
    own = client.api.inspect_container("broker-id")
    own["Image"] = "sha256:" + "b" * 64
    client.api.inspect_image = lambda image: {
        "Id": image,
        "Os": "linux",
        "Architecture": "amd64",
    }
    assert own["Image"] != previous_image
    assert worker.observe(operation).state == "mismatch"
    assert worker.active() is None and not worker.lock_held()

    def active() -> SimpleNamespace:
        current = worker.active()
        return SimpleNamespace(
            operation_id=str(current[0]) if current else None,
            generation=current[1] if current else None,
            lock_held=worker.lock_held(),
        )

    monkeypatch.setattr(broker, "active_reviewed_runtime_installation", active)
    monkeypatch.setattr(
        broker,
        "observe_reviewed_runtime_installation",
        lambda identity: worker.observe(UUID(identity)),
    )
    observed = installer.observe_create("omega", operation)
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None
        assert observed.runtime_revision_id == UUID(row.runtime_revision_id or "")
        assert observed.image_id == row.image_id
    assert observed.status == "ready" and not observed.busy
    assert historical.attrs["State"] == {"Running": False, "ExitCode": 0}
    assert historical.attrs["Image"] == previous_image
    assert historical.starts == 1 and broker.launches == []


@pytest.mark.parametrize("historical", ["missing", "exited", "mismatch", "unavailable"])
def test_completed_cache_observation_is_select_only_and_never_adopts_a_worker(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    historical: str,
) -> None:
    _, _, broker, installer = setup(factory)
    operation = _ready(factory, generation="omega")
    worker_reads: list[str] = []

    def observe(identity: str) -> SimpleNamespace:
        worker_reads.append(identity)
        if historical == "unavailable":
            raise RemoteBrokerError(
                "runtime_installation_unavailable", "", request_id=None
            )
        return SimpleNamespace(state=historical, lock_held=False)

    monkeypatch.setattr(broker, "observe_reviewed_runtime_installation", observe)
    with factory() as session:
        engine = session.get_bind()
    statements: list[str] = []

    def record(
        _c: object, _cu: object, statement: str, _p: object, _ctx: object, _many: object
    ) -> None:
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(2):
            observed = installer.observe_create("omega", operation)
            assert observed.status == "ready" and not observed.busy
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) == {"SELECT"}
    assert worker_reads == [] and broker.launches == []


@pytest.mark.parametrize(
    "blocker", ["same", "other", "lock", "unknown_lock", "unknown_worker"]
)
def test_completed_cache_does_not_override_active_or_uncertain_installation(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    _, _, broker, installer = setup(factory)
    operation = _ready(factory, generation="omega")
    broker.workers[str(operation)] = "mismatch"
    if blocker in {"same", "other"}:
        broker.operation_id = str(uuid4())
        broker.generation = "omega" if blocker == "same" else "nexus"
    elif blocker == "lock":
        broker.lock_held = True
    elif blocker == "unknown_lock":
        monkeypatch.setattr(
            broker,
            "active_reviewed_runtime_installation",
            lambda: SimpleNamespace(operation_id=None, generation=None, lock_held=None),
        )
    else:

        def unavailable() -> SimpleNamespace:
            raise RemoteBrokerError(
                "runtime_installation_unavailable", "", request_id=None
            )

        monkeypatch.setattr(broker, "active_reviewed_runtime_installation", unavailable)
    observed = installer.observe_create("omega", operation)
    assert observed.busy and observed.status != "ready"
    assert broker.launches == []


@pytest.mark.parametrize("worker", ["missing", "mismatch"])
@pytest.mark.parametrize(
    "state", ["pending", "acquiring", "assembling", "validating", "failed"]
)
def test_incomplete_or_failed_receipt_never_acquires_completed_cache_authority(
    factory: sessionmaker[Session], worker: str, state: str
) -> None:
    _, _, broker, installer = setup(factory)
    operation = uuid4()
    create_row(factory, "omega", operation)
    with factory.begin() as session:
        repository = RuntimeRecipeInstallationRepository(session)
        row = repository.get(operation)
        assert row is not None
        if state == "failed":
            repository.fail(operation, code="validation_failed", now=datetime.now(UTC))
        else:
            while row.state is not RuntimeRecipeInstallationState(state):
                repository.advance(operation, expected=row.state, now=datetime.now(UTC))
    broker.workers[str(operation)] = worker
    observed = installer.observe_create("omega", operation)
    assert observed.status == (
        "uncertain"
        if worker == "mismatch"
        else "failed"
        if state == "failed"
        else "interrupted"
    )
    assert observed.busy == (worker == "mismatch")
    assert observed.runtime_revision_id is None and broker.launches == []


@pytest.mark.parametrize(
    "defect",
    [
        "missing_image",
        "wrong_image",
        "approval",
        "attestation",
        "validation",
        "recipe",
        "generation",
        "revision",
    ],
)
def test_historical_worker_does_not_hide_invalid_cache_evidence(
    factory: sessionmaker[Session], defect: str
) -> None:
    _, _, broker, installer = setup(factory)
    operation = _ready(factory, generation="omega")
    other = _ready(factory, generation="nexus") if defect == "revision" else None
    broker.workers[str(operation)] = "mismatch"
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None
        revision = session.get(RuntimeRevisionRow, row.runtime_revision_id)
        assert revision is not None
        if defect == "missing_image":
            broker.image_present = False
        elif defect == "wrong_image":
            row.image_id = "sha256:" + "e" * 64
        elif defect == "approval":
            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
        elif defect == "attestation":
            row.attestation_json = "{}"
        elif defect == "validation":
            row.validation_json = "{}"
        elif defect == "recipe":
            row.recipe_digest = "sha256:" + "e" * 64
        elif defect == "generation":
            row.generation = "nexus"
        else:
            alternate = session.get(RuntimeRecipeInstallationRow, str(other))
            assert alternate is not None
            row.runtime_revision_id = alternate.runtime_revision_id
    observed = installer.observe_create("omega", operation)
    assert observed.status == "unavailable"
    assert observed.runtime_revision_id is None and observed.image_id is None
    assert broker.launches == []


def test_stopped_profile_clean_reset_uses_verified_cache_after_broker_upgrade(
    env: Environment,
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    original = env.broker.records[0]
    env.broker.worker_state = "mismatch"
    slots = ManagedSlotsWeb(env.service, lambda _target: False)
    omega = next(
        s
        for s in slots.status(lambda _session: None)["slots"]
        if s["version"] == "omega"
    )
    assert omega["state"] == "stopped" and omega["reset"]
    result = reset(env)
    assert result.phase == "complete"
    assert result.retirement == result.replacement == "confirmed"
    assert env.broker.records[0].runtime_revision_id == original.runtime_revision_id
    assert env.broker.records[0].image_id == original.image_id
    assert env.broker.records[0].runtime_state == "stopped"
    assert env.broker.removals == 1 and env.broker.launches == []


@pytest.mark.parametrize("blocker", ["same", "other", "lock"])
def test_clean_reset_keeps_stopped_profile_when_installation_is_busy(
    env: Environment, blocker: str
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    original = env.broker.records[0]
    if blocker == "lock":
        env.broker.lock_held = True
    else:
        latest = env.installer._latest("omega")
        assert latest is not None
        env.broker.operation_id = latest.id if blocker == "same" else str(uuid4())
        env.broker.generation = "omega" if blocker == "same" else "nexus"
    with pytest.raises(ManagedVersionError, match="approved_cached_runtime_required"):
        reset(env)
    assert env.broker.records == [original]
    assert env.broker.removals == 0 and env.broker.launches == []

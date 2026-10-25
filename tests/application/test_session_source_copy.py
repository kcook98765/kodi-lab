"""Durable copy admission replays only original scope and existing typed work."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentConflictError,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.application.session_source_copy import (
    SOURCE_COPY_NAMESPACE,
    SessionSourceCopyService,
)
from kodi_lab.domain import (
    AgentWorkspaceState,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    SessionSourceCopyAdmissionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceDeploymentOperationRepository
from tests.application import test_development_source_deployments as fixtures
from tests.application.test_development_source_deployments import factory as factory


def bind(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        workspace_id = str(uuid4())
        session.add(
            AgentWorkspaceRow(
                id=workspace_id,
                development_source_id=str(fixtures.SOURCE_ID),
                source_addon_id="plugin.video.example",
                source_logical_name="Example",
                source_observed_name="Example",
                source_observed_version="1.0.0",
                state=AgentWorkspaceState.READY,
                baseline_artifact_id=str(uuid4()),
                baseline_manifest_digest="sha256:" + "1" * 64,
                current_tree_digest="sha256:" + "1" * 64,
                capture_started_at=fixtures.NOW.isoformat(),
                captured_at=fixtures.NOW.isoformat(),
                created_at=fixtures.NOW.isoformat(),
                updated_at=fixtures.NOW.isoformat(),
            )
        )
        session.flush()
        session.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(fixtures.INSTANCE_ID),
                runtime_revision_id=str(fixtures.REVISION_ID),
                workspace_id=workspace_id,
                source_id=str(fixtures.SOURCE_ID),
                container_id=fixtures.CONTAINER_ID,
                phase="bound",
                created_at=fixtures.NOW.isoformat(),
                updated_at=fixtures.NOW.isoformat(),
            )
        )
        session.flush()
        session.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))


def setup(
    factory: sessionmaker[Session], tmp_path: Path
) -> tuple[SessionSourceCopyService, Any, fixtures.FakeBroker]:
    bind(factory)
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    return SessionSourceCopyService(factory, coordinator), coordinator, broker


def assert_counts(factory: sessionmaker[Session], count: int) -> None:
    with factory() as session:
        for model in (
            SessionSourceCopyAdmissionRow,
            DevelopmentSourceDeploymentOperationRow,
            ManagedInstanceMutationLeaseRow,
            GlobalHeavyOperationLeaseRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == count


def test_admission_commits_receipt_operation_and_two_authorities_then_exact_replay(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()
    receipt = service.admit(request_id=request, expected_generation=1)
    assert receipt.operation_id == uuid5(SOURCE_COPY_NAMESPACE, str(request))
    assert (
        receipt.container_id == fixtures.CONTAINER_ID and receipt.detail == "retained"
    )
    assert receipt.operation is not None and receipt.operation.state.value == "queued"
    assert_counts(factory, 1)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("replay must not inspect or queue runtime work")

    monkeypatch.setattr(coordinator, "queue", forbidden)
    monkeypatch.setattr(broker, "managed_container_inventory", forbidden)
    assert service.admit(request_id=request, expected_generation=1) == receipt
    assert service.get(request) == receipt and not broker.calls
    with pytest.raises(DevelopmentSessionConflict, match="request_conflict"):
        service.admit(request_id=request, expected_generation=2)
    assert_counts(factory, 1)


def test_real_existing_worker_produces_baseline_but_does_not_enable_or_start(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    completed = service.get(receipt.request_id)
    assert completed is not None and completed.operation is not None
    assert completed.operation.state.value == "succeeded" and len(broker.calls) == 1
    assert (
        service.admit(request_id=receipt.request_id, expected_generation=1) == completed
    )
    assert len(broker.calls) == 1


def test_pruned_terminal_detail_never_readmits_the_nonce(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    # Positive profile drift permits the existing exact unclaimed refusal path.
    monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    with pytest.raises(DevelopmentSourceDeploymentConflictError):
        coordinator.acquire(receipt.operation_id)
    with factory.begin() as session:
        assert (
            DevelopmentSourceDeploymentOperationRepository(session).prune_terminal(
                fixtures.INSTANCE_ID, retain=0
            )
            == 1
        )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pruned history cannot cause runtime dispatch")

    monkeypatch.setattr(coordinator, "queue", forbidden)
    result = service.admit(request_id=receipt.request_id, expected_generation=1)
    assert result.operation is None and result.detail == "not_retained"
    assert result.operation_id == receipt.operation_id and not broker.calls
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(SessionSourceCopyAdmissionRow)
            )
            == 1
        )


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_interruption_keeps_atomic_receipt_and_explicit_replay(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    service, _coordinator, broker = setup(factory, tmp_path)
    request = uuid4()
    original = Session.commit
    armed = True

    def commit(session: Session) -> None:
        nonlocal armed
        if (
            armed
            and session.get(SessionSourceCopyAdmissionRow, str(request)) is not None
        ):
            armed = False
            if after_commit:
                original(session)
            raise KeyboardInterrupt("lost queue acknowledgement")
        original(session)

    monkeypatch.setattr(Session, "commit", commit)
    with pytest.raises(KeyboardInterrupt):
        service.admit(request_id=request, expected_generation=1)
    assert_counts(factory, int(after_commit))
    receipt = service.admit(request_id=request, expected_generation=1)
    assert receipt.operation is not None and receipt.operation.state.value == "queued"
    assert_counts(factory, 1)
    assert not broker.calls


@pytest.mark.parametrize(
    "defect", ["stale", "uncertain", "pending", "profile", "writer_switch"]
)
def test_wrong_selected_scope_never_admits_work(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    from kodi_lab.storage.models import ManagedStartReservationRow

    service, coordinator, broker = setup(factory, tmp_path)
    if defect == "profile":
        monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    elif defect == "writer_switch":
        original = coordinator.queue

        def queue(*args: Any, **kwargs: Any) -> Any:
            with factory.begin() as session:
                row = session.get(DevelopmentSessionRow, 1)
                assert row is not None
                row.phase = "uncertain"
            return original(*args, **kwargs)

        monkeypatch.setattr(coordinator, "queue", queue)
    else:
        with factory.begin() as session:
            row = session.get(DevelopmentSessionRow, 1)
            assert row is not None
            if defect == "uncertain":
                row.phase = "uncertain"
            if defect == "pending":
                session.add(
                    ManagedStartReservationRow(
                        managed_instance_id=str(fixtures.INSTANCE_ID),
                        runtime_revision_id=str(fixtures.REVISION_ID),
                        operation_id=str(uuid4()),
                        container_id=fixtures.CONTAINER_ID,
                        phase="uncertain",
                        created_at=fixtures.NOW.isoformat(),
                    )
                )
    with pytest.raises(DevelopmentSessionConflict):
        service.admit(
            request_id=uuid4(), expected_generation=2 if defect == "stale" else 1
        )
    assert_counts(factory, 0)
    assert not broker.calls


@pytest.mark.parametrize("scope", ["container_id", "source_id", "operation_id"])
def test_partial_receipt_restore_is_not_reattributed(
    factory: sessionmaker[Session],
    tmp_path: Path,
    scope: str,
) -> None:
    service, _coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    with factory.begin() as session:
        row = session.get(SessionSourceCopyAdmissionRow, str(receipt.request_id))
        assert row is not None
        setattr(row, scope, "f" * 64 if scope == "container_id" else str(uuid4()))
    with pytest.raises(DevelopmentSessionConflict, match="receipt_scope_changed"):
        service.admit(request_id=receipt.request_id, expected_generation=1)
    assert_counts(factory, 1)
    assert not broker.calls


def test_capacity_refuses_without_pruning_any_request_or_creating_work(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kodi_lab.application.session_source_copy as module

    service, _coordinator, broker = setup(factory, tmp_path)
    monkeypatch.setattr(module, "MAX_ADMISSIONS_PER_SESSION", 0)
    with pytest.raises(DevelopmentSessionConflict, match="history_full"):
        service.admit(request_id=uuid4(), expected_generation=1)
    assert_counts(factory, 0)
    assert not broker.calls


def test_get_is_stored_observation_only(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    service, _coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    engine = factory.kw["bind"]
    commands: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        commands.append(sql.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert service.get(receipt.request_id) == receipt
        assert service.get(uuid4()) is None
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert set(commands) <= {"BEGIN", "SELECT"} and not broker.calls


def test_concurrent_same_request_has_one_atomic_queue(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()
    barrier = Barrier(2)
    original = coordinator.queue

    def queue(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "queue", queue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [
            pool.submit(service.admit, request_id=request, expected_generation=1)
            for _ in range(2)
        ]
        results = [task.result(timeout=15) for task in tasks]
    assert results[0] == results[1]
    assert_counts(factory, 1)
    assert not broker.calls


def test_historical_replay_survives_session_close_without_new_authority(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert current is not None and policy is not None
        current.phase = "closed"
        policy.active_generation = None

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("historical replay must not claim current authority")

    monkeypatch.setattr(coordinator, "queue", forbidden)
    assert (
        service.admit(request_id=receipt.request_id, expected_generation=1) == receipt
    )
    assert not broker.calls


def test_peer_receipt_wins_when_legacy_queue_checks_observe_its_active_work(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()
    original = coordinator.queue

    def queue(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise DevelopmentSourceDeploymentConflictError(
            "peer already queued this exact request"
        )

    monkeypatch.setattr(coordinator, "queue", queue)
    receipt = service.admit(request_id=request, expected_generation=1)
    assert receipt.operation is not None and receipt.operation.state.value == "queued"
    assert_counts(factory, 1)
    assert not broker.calls

"""Selected-source boundaries and explicit original-queued-child submission."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentLeaseLostError,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
)
from tests.application import test_development_source_deployments as fixtures
from tests.application.test_development_source_deployments import factory as factory
from tests.application.test_session_source_copy import assert_counts, setup


def test_explicit_duplicate_submit_uses_one_original_operation_and_one_legal_copy(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    submitted: list[UUID] = []
    request = uuid4()
    first = service.request_copy(
        request_id=request, expected_generation=1, submit=submitted.append
    )
    second = service.request_copy(
        request_id=request, expected_generation=1, submit=submitted.append
    )
    assert first.submission == second.submission == "requested"
    assert submitted == [first.receipt.operation_id] * 2
    lease = coordinator.acquire(submitted[0])
    assert lease is not None
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    with pytest.raises(DevelopmentSourceDeploymentLeaseLostError):
        coordinator.acquire(submitted[1])
    replay = service.request_copy(
        request_id=request, expected_generation=1, submit=submitted.append
    )
    assert replay.submission == "not_requested" and len(submitted) == 2
    assert (
        replay.receipt.operation is not None
        and replay.receipt.operation.state.value == "succeeded"
    )
    assert len(broker.calls) == 1


def test_unacknowledged_submission_keeps_durable_queue_for_explicit_continuation(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()

    def interrupted(_operation_id: UUID) -> None:
        raise RuntimeError("lost submit acknowledgement")

    pending = service.request_copy(
        request_id=request, expected_generation=1, submit=interrupted
    )
    assert pending.submission == "not_acknowledged"
    assert_counts(factory, 1)
    assert not broker.calls

    def execute(operation_id: UUID) -> None:
        lease = coordinator.acquire(operation_id)
        assert lease is not None
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()

    resumed = service.request_copy(
        request_id=request, expected_generation=1, submit=execute
    )
    assert (
        resumed.receipt.operation_id == pending.receipt.operation_id
        and resumed.submission == "requested"
    )
    finished = service.get(request)
    assert finished is not None and finished.operation is not None
    assert finished.operation.state.value == "succeeded" and len(broker.calls) == 1


@pytest.mark.parametrize("outcome", ["running", "failed", "reconciliation_required"])
def test_nonqueued_work_is_never_submitted_again(
    factory: sessionmaker[Session],
    tmp_path: Path,
    outcome: str,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    if outcome != "running":
        coordinator.finish_failure(
            lease,
            reason="fixture outcome",
            reconciliation_required=outcome == "reconciliation_required",
        )
    submitted: list[UUID] = []
    replay = service.request_copy(
        request_id=receipt.request_id, expected_generation=1, submit=submitted.append
    )
    assert replay.submission == "not_requested" and not submitted and not broker.calls
    assert (
        replay.receipt.operation is not None
        and replay.receipt.operation.state.value == outcome
    )


@pytest.mark.parametrize("defect", ["foreign", "claimed"])
def test_unclaimed_lease_proof_is_required_before_explicit_submission(
    factory: sessionmaker[Session],
    tmp_path: Path,
    defect: str,
) -> None:
    service, _coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    with factory.begin() as session:
        row = session.scalar(select(GlobalHeavyOperationLeaseRow))
        assert row is not None
        if defect == "foreign":
            row.operation_id = str(uuid4())
        else:
            row.lease_token_digest = "a" * 64
            row.lease_expires_at = "2026-09-16T23:00:00.000000Z"
    submitted: list[UUID] = []
    result = service.request_copy(
        request_id=receipt.request_id, expected_generation=1, submit=submitted.append
    )
    assert result.submission == "review_required" and not submitted and not broker.calls
    assert_counts(factory, 1)


def test_legacy_queue_cannot_target_another_profile_under_active_policy(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    _service, coordinator, broker = setup(factory, tmp_path)
    with pytest.raises(DevelopmentSessionConflict, match="other_target"):
        coordinator.queue(fixtures.SECOND_INSTANCE_ID, fixtures.SOURCE_ID)
    assert_counts(factory, 0)
    assert not broker.calls


@pytest.mark.parametrize("boundary", ["worker", "broker", "explicit_submit"])
@pytest.mark.parametrize("change", ["uncertain", "closed", "same_scope_successor"])
def test_selected_scope_is_independently_rechecked_and_preserves_the_fence(
    factory: sessionmaker[Session],
    tmp_path: Path,
    boundary: str,
    change: str,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    if boundary == "broker":
        assert coordinator.acquire(receipt.operation_id) is not None
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert current is not None and policy is not None
        if change == "uncertain":
            current.phase = "uncertain"
        else:
            current.phase = "closed"
            policy.active_generation = None
            if change == "same_scope_successor":
                # Partial restore: even an identical CID/scope cannot reattribute
                # the old guided request to a different logical generation.
                values = {
                    column.name: getattr(current, column.name)
                    for column in current.__table__.columns
                }
                values.update(generation=2, request_id=str(uuid4()), phase="bound")
                session.add(DevelopmentSessionRow(**values))
                session.flush()
                policy.active_generation = 2
    if boundary == "broker":
        resolver = SqliteDevelopmentSourceDeploymentResolver(
            factory, development_root=tmp_path, data_root=tmp_path / "data"
        )
        with pytest.raises(
            DevelopmentSourceDeploymentStateError, match="Selected development session"
        ):
            resolver.resolve(
                str(fixtures.INSTANCE_ID),
                str(fixtures.SOURCE_ID),
                str(receipt.operation_id),
                fixtures.CONTAINER_ID,
            )
    elif boundary == "worker":
        with pytest.raises(DevelopmentSessionConflict):
            coordinator.acquire(receipt.operation_id)
    else:

        def forbidden(_operation_id: UUID) -> Any:
            raise AssertionError("changed selection cannot submit")

        with pytest.raises(DevelopmentSessionConflict):
            service.request_copy(
                request_id=receipt.request_id, expected_generation=1, submit=forbidden
            )
    operation = coordinator.get(receipt.operation_id)
    assert operation is not None and operation.state.value == (
        "running" if boundary == "broker" else "queued"
    )
    with factory() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
            is not None
        )
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is not None
    assert not broker.calls


def test_lost_submit_reply_after_completed_copy_does_not_repeat_it(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()

    def execute_then_lose_reply(operation_id: UUID) -> None:
        lease = coordinator.acquire(operation_id)
        assert lease is not None
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
        raise RuntimeError("lost acknowledgement after the original copy")

    result = service.request_copy(
        request_id=request, expected_generation=1, submit=execute_then_lose_reply
    )
    assert result.submission == "not_acknowledged" and len(broker.calls) == 1
    submitted: list[UUID] = []
    replay = service.request_copy(
        request_id=request, expected_generation=1, submit=submitted.append
    )
    assert replay.submission == "not_requested" and not submitted
    assert (
        replay.receipt.operation is not None
        and replay.receipt.operation.state.value == "succeeded"
    )
    assert len(broker.calls) == 1


def test_concurrent_explicit_continuations_have_one_legal_native_dispatch(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, coordinator, broker = setup(factory, tmp_path)
    request = uuid4()
    receipt = service.admit(request_id=request, expected_generation=1)
    barrier = Barrier(2)

    def execute(operation_id: UUID) -> None:
        assert operation_id == receipt.operation_id
        # Both callers reach submit before either claims. No read transaction
        # may remain open here and block the existing atomic writer claim.
        barrier.wait(timeout=5)
        lease = coordinator.acquire(operation_id)
        assert lease is not None
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [
            pool.submit(
                service.request_copy,
                request_id=request,
                expected_generation=1,
                submit=execute,
            )
            for _ in range(2)
        ]
        results = [task.result(timeout=15) for task in tasks]
    assert {result.submission for result in results} == {
        "requested",
        "not_acknowledged",
    }
    finished = service.get(request)
    assert finished is not None and finished.operation is not None
    assert finished.operation.state.value == "succeeded" and len(broker.calls) == 1

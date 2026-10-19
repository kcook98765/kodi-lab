"""A lifecycle mutation and its owned agent receipt are claimed in one transaction."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import (
    ManagedInstanceLifecycleOperationCoordinator,
    RunningLifecycleOperation,
)
from kodi_lab.domain import ManagedInstanceLifecycleKind, ManagedInstanceSemanticState
from kodi_lab.storage.models import (
    ManagedAgentLifecycleRequestRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
)
from tests.application import test_lifecycle_history as history

factory = history.factory


@pytest.mark.parametrize("fail_claim", [False, True])
def test_agent_receipt_and_exact_child_operation_commit_or_rollback_together(
    factory: sessionmaker[Session],
    fail_claim: bool,
) -> None:
    request_id, operation_id = uuid4(), uuid4()
    with factory.begin() as session:
        session.add(
            ManagedAgentLifecycleRequestRow(
                id=str(request_id),
                client_id=None,
                request_id=str(uuid4()),
                original_instance_id=str(history.INSTANCE_ID),
                managed_instance_id=str(history.INSTANCE_ID),
                action="ensure_ready",
                hold_minutes=15,
                state="pending",
                created_at="2026-10-19T12:00:00.000000Z",
                updated_at="2026-10-19T12:00:00.000000Z",
            )
        )

    def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
        row = session.get(ManagedAgentLifecycleRequestRow, str(request_id))
        assert row is not None
        lease = session.get(ManagedInstanceMutationLeaseRow, str(history.INSTANCE_ID))
        assert lease is not None and lease.operation_id == str(operation_id)
        row.lifecycle_operation_id, row.state = str(handle.operation_id), "dispatched"
        if fail_claim:
            raise RuntimeError("fixture claim persistence failure")

    coordinator = ManagedInstanceLifecycleOperationCoordinator(factory)

    def begin() -> RunningLifecycleOperation:
        return coordinator.begin(
            history.INSTANCE_ID,
            instance_name="Owned lifecycle fixture",
            kind=ManagedInstanceLifecycleKind.START,
            prior_state=ManagedInstanceSemanticState.AMBIGUOUS,
            operation_id=operation_id,
            on_claim=claimed,
        )

    if fail_claim:
        with pytest.raises(RuntimeError, match="claim persistence failure"):
            begin()
    else:
        assert begin().operation_id == operation_id
    with factory() as session:
        row = session.get(ManagedAgentLifecycleRequestRow, str(request_id))
        assert row is not None
        assert row.state == ("pending" if fail_claim else "dispatched")
        child = session.get(ManagedInstanceLifecycleOperationRow, str(operation_id))
        lease = session.get(ManagedInstanceMutationLeaseRow, str(history.INSTANCE_ID))
        assert (child is None) is fail_claim
        assert (lease is None) is fail_claim


def test_atomic_stop_precondition_refuses_before_lifecycle_or_lease_creation(
    factory: sessionmaker[Session],
) -> None:
    operation_id = uuid4()

    def protect(_session: Session) -> None:
        raise RuntimeError("fixture another client is using this Kodi")

    coordinator = ManagedInstanceLifecycleOperationCoordinator(factory)
    with pytest.raises(RuntimeError, match="another client"):
        coordinator.begin(
            history.INSTANCE_ID,
            instance_name="Protected fixture",
            kind=ManagedInstanceLifecycleKind.STOP,
            prior_state=ManagedInstanceSemanticState.AMBIGUOUS,
            operation_id=operation_id,
            precondition=protect,
        )
    with factory() as session:
        assert (
            session.get(ManagedInstanceLifecycleOperationRow, str(operation_id)) is None
        )
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(history.INSTANCE_ID))
            is None
        )

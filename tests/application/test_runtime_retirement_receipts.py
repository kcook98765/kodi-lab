"""Exact scoped removal, owned terminal publication and retained uncertainty."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from kodi_lab.application.lifecycle_operations import (
    LifecycleOperationLeaseLostError,
    RunningLifecycleOperation,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_lifecycle import (
    LifecycleConflictError,
    LifecycleDispatchUnconfirmedError,
    ManagedRuntimeLifecycleService,
)
from kodi_lab.application.runtime_retirement_receipts import (
    RuntimeRetirementReceiptError,
    RuntimeRetirementScope,
    confirm_retirement_receipt,
    prepare_retirement_receipt,
    read_retirement_receipt,
)
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
)
from kodi_lab.storage.models import (
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedRuntimeRetirementRow,
)
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_session_replacement_reviews import environment


class Broker:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.mode = "success"
        self.calls: list[dict[str, object]] = []

    def managed_container_inventory(self, **_kwargs: object) -> Any:
        return self.state.service._inventory()

    def observe_managed_kodi_runtime(
        self, **_kwargs: object
    ) -> ObserveManagedKodiRuntimeResult:
        raise AssertionError("stopped retirement must not probe native readiness")

    def deprovision_managed_container(
        self, **kwargs: object
    ) -> DeprovisionManagedContainerResult:
        self.calls.append(kwargs)
        if self.mode != "remaining":
            self.state.records.clear()
        if self.mode == "lost_reply":
            raise ClientTransportError("fixture removal reply was lost")
        if self.mode == "cancelled":
            raise KeyboardInterrupt
        return DeprovisionManagedContainerResult(
            managed_instance_id=str(fixture.TARGET_ID),
            runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
            removed_container_id="e" * 64
            if self.mode == "wrong_reply"
            else fixture.CONTAINER_ID,
            removed=True,
        )


def scope() -> RuntimeRetirementScope:
    return RuntimeRetirementScope(
        managed_instance_id=fixture.TARGET_ID,
        runtime_revision_id=fixture.RUNTIME_REVISION_ID,
        container_id=fixture.CONTAINER_ID,
        image_id=fixture.IMAGE_ID,
    )


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_path: Path | None = None,
) -> Any:
    env = environment(tmp_path, monkeypatch, database_path=database_path)
    broker = Broker(env.state)
    readiness = ReadinessTaskRegistry(max_workers=1)
    status = ManagedRuntimeStatusService(env.state.env.sessions, broker, readiness)
    lifecycle = ManagedRuntimeLifecycleService(
        env.state.env.sessions, cast(Any, broker), readiness, status
    )
    return SimpleNamespace(
        env=env,
        sessions=env.state.env.sessions,
        broker=broker,
        readiness=readiness,
        lifecycle=lifecycle,
    )


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    result = build(tmp_path, monkeypatch)
    yield result
    result.readiness.close()


def test_exact_removal_reply_and_owned_terminalization_publish_one_scoped_receipt(
    case: Any,
) -> None:
    operation_id = uuid4()
    before = case.env.state.service.status()

    def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
        row = session.get(ManagedRuntimeRetirementRow, str(operation_id))
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID))
        assert row is not None and row.confirmed_at is None
        assert lease is not None and lease.operation_id == row.operation_id == str(
            handle.operation_id
        )

    case.lifecycle.deprovision(
        fixture.TARGET_ID,
        operation_id=operation_id,
        expected_scope=scope(),
        on_claim=claimed,
    )
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.scope == scope() and receipt.confirmation == "confirmed"
    assert receipt.operation_state is ManagedInstanceLifecycleOperationState.SUCCEEDED
    assert (
        receipt.confirmed_at is not None and receipt.confirmed_at >= receipt.prepared_at
    )
    assert receipt.settles_uncertain_create is False
    assert (
        case.env.state.service.status() == before
    )  # No close, successor or policy gap.
    assert not case.env.state.records and len(case.broker.calls) == 1
    assert (
        case.env.retained.read(case.env.capture.capture_id).capture.state == "retained"
    )
    with case.sessions() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)) is None
        )


def test_existing_unscoped_deprovision_does_not_backfill_an_exact_witness(
    case: Any,
) -> None:
    operation_id = uuid4()
    case.lifecycle.deprovision(fixture.TARGET_ID, operation_id=operation_id)
    with pytest.raises(RuntimeRetirementReceiptError, match="receipt_not_found"):
        read_retirement_receipt(case.sessions, operation_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("managed_instance_id", UUID(int=123)),
        ("runtime_revision_id", UUID(int=456)),
        ("image_id", "sha256:" + "d" * 64),
    ],
)
def test_changed_stored_scope_refuses_before_lifecycle_or_receipt_claim(
    case: Any, field: str, value: object
) -> None:
    operation_id = uuid4()
    with pytest.raises(RuntimeRetirementReceiptError, match="scope_changed"):
        case.lifecycle.deprovision(
            fixture.TARGET_ID,
            operation_id=operation_id,
            expected_scope=scope().model_copy(update={field: value}),
        )
    with case.sessions() as session:
        assert (
            session.get(ManagedInstanceLifecycleOperationRow, str(operation_id)) is None
        )
        assert session.get(ManagedRuntimeRetirementRow, str(operation_id)) is None
    assert not case.broker.calls and len(case.env.state.records) == 1


@pytest.mark.parametrize("race", ["container", "image", "runtime"])
def test_identity_changed_after_claim_cannot_invalidate_or_remove_replacement(
    case: Any, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    from kodi_lab.application import runtime_lifecycle as module

    operation_id = uuid4()

    def claimed(_session: Session, _handle: RunningLifecycleOperation) -> None:
        record = case.env.state.records[0]
        updates = {
            "container": {"container_id": "f" * 64},
            "image": {"image_id": "sha256:" + "f" * 64},
            "runtime": {"runtime_revision_id": str(uuid4())},
        }[race]
        case.env.state.records[:] = [replace(record, **updates)]

    def forbidden(*_args: object) -> None:
        raise AssertionError("stale scope must not invalidate readiness or trust")

    monkeypatch.setattr(case.readiness, "clear", forbidden)
    for name in (
        "invalidate_development_source_deployments",
        "invalidate_addon_zip_installation_evidence",
        "invalidate_managed_bridge_trust",
    ):
        monkeypatch.setattr(module, name, forbidden)
    with pytest.raises(RuntimeRetirementReceiptError, match="scope_changed"):
        case.lifecycle.deprovision(
            fixture.TARGET_ID,
            expected_scope=scope(),
            operation_id=operation_id,
            on_claim=claimed,
        )
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.confirmation == "not_confirmed" and not case.broker.calls
    assert len(case.env.state.records) == 1


@pytest.mark.parametrize(
    "mode,error,state",
    [
        (
            "lost_reply",
            LifecycleDispatchUnconfirmedError,
            ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED,
        ),
        (
            "wrong_reply",
            LifecycleDispatchUnconfirmedError,
            ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED,
        ),
        (
            "remaining",
            LifecycleConflictError,
            ManagedInstanceLifecycleOperationState.FAILED,
        ),
        (
            "cancelled",
            KeyboardInterrupt,
            ManagedInstanceLifecycleOperationState.RUNNING,
        ),
    ],
)
def test_absence_bad_reply_failed_postcondition_and_cancellation_never_confirm(
    case: Any,
    mode: str,
    error: type[BaseException],
    state: ManagedInstanceLifecycleOperationState,
) -> None:
    operation_id = uuid4()
    case.broker.mode = mode
    with pytest.raises(error):
        case.lifecycle.deprovision(
            fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
        )
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.confirmation == "not_confirmed" and receipt.confirmed_at is None
    assert receipt.operation_state is state
    assert case.env.state.service.status().phase == "bound"


def test_receipt_and_exact_child_claim_roll_back_together_on_callback_failure(
    case: Any,
) -> None:
    operation_id = uuid4()

    def fail(_session: Session, _handle: RunningLifecycleOperation) -> None:
        raise RuntimeError("fixture claim failed")

    with pytest.raises(RuntimeError, match="claim failed"):
        case.lifecycle.deprovision(
            fixture.TARGET_ID,
            operation_id=operation_id,
            expected_scope=scope(),
            on_claim=fail,
        )
    with case.sessions() as session:
        assert session.get(ManagedRuntimeRetirementRow, str(operation_id)) is None
        assert (
            session.get(ManagedInstanceLifecycleOperationRow, str(operation_id)) is None
        )
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)) is None
        )
    assert not case.broker.calls


def test_receipt_publication_failure_rolls_back_terminal_success_and_lease_release(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import runtime_lifecycle as module

    operation_id = uuid4()

    def fail(*_args: object) -> None:
        raise RuntimeError("fixture receipt storage failed")

    monkeypatch.setattr(module, "confirm_retirement_receipt", fail)
    with pytest.raises(RuntimeError, match="receipt storage failed"):
        case.lifecycle.deprovision(
            fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
        )
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.confirmation == "not_confirmed"
    assert receipt.operation_state is ManagedInstanceLifecycleOperationState.RUNNING
    with case.sessions() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID))
            is not None
        )
    assert not case.env.state.records


def test_cancellation_after_committed_terminalization_preserves_confirmed_receipt(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import runtime_lifecycle as module

    confirm = confirm_retirement_receipt

    def mark(session: Session, *args: Any) -> None:
        confirm(session, *args)
        session.info["cancel_after_retirement_commit"] = True

    def cancel(session: Session) -> None:
        if session.info.get("cancel_after_retirement_commit"):
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "confirm_retirement_receipt", mark)
    event.listen(Session, "after_commit", cancel)
    operation_id = uuid4()
    try:
        with pytest.raises(KeyboardInterrupt):
            case.lifecycle.deprovision(
                fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
            )
    finally:
        event.remove(Session, "after_commit", cancel)
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.confirmation == "confirmed"
    assert receipt.operation_state is ManagedInstanceLifecycleOperationState.SUCCEEDED


def test_pruning_preserves_referenced_exact_operation_without_rewriting_history(
    case: Any,
) -> None:
    operation_id = uuid4()
    case.lifecycle.deprovision(
        fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
    )
    other_id = uuid4()
    with case.sessions.begin() as session:
        repository = ManagedInstanceLifecycleOperationRepository(session)
        original = repository.get(operation_id)
        assert original is not None
        repository.create(replace(original, id=other_id))
        assert repository.prune_terminal(fixture.TARGET_ID, retain=0) == 1
        assert (
            repository.get(other_id) is None
            and repository.get(operation_id) is not None
        )
    assert (
        read_retirement_receipt(case.sessions, operation_id).confirmation == "confirmed"
    )


def test_scope_changed_in_prepared_row_cannot_publish_a_different_retirement(
    case: Any,
) -> None:
    operation_id = uuid4()

    def changed(session: Session, _handle: RunningLifecycleOperation) -> None:
        row = session.get(ManagedRuntimeRetirementRow, str(operation_id))
        assert row is not None
        row.container_id = "f" * 64

    with pytest.raises(RuntimeRetirementReceiptError, match="confirmation_invalid"):
        case.lifecycle.deprovision(
            fixture.TARGET_ID,
            operation_id=operation_id,
            expected_scope=scope(),
            on_claim=changed,
        )
    receipt = read_retirement_receipt(case.sessions, operation_id)
    assert receipt.confirmation == "not_confirmed"
    assert receipt.operation_state is ManagedInstanceLifecycleOperationState.RUNNING


def test_bounded_receipt_history_refuses_before_removing_another_profile(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import runtime_retirement_receipts as module

    monkeypatch.setattr(module, "MAX_RETIREMENTS", 0)
    with pytest.raises(RuntimeRetirementReceiptError, match="history_capacity"):
        case.lifecycle.deprovision(fixture.TARGET_ID, expected_scope=scope())
    assert not case.broker.calls
    with case.sessions() as session:
        assert session.scalar(select(ManagedRuntimeRetirementRow)) is None


def test_terminal_hook_cannot_publish_after_lifecycle_lease_ownership_is_lost(
    case: Any,
) -> None:
    coordinator = case.lifecycle._operations
    called: list[object] = []

    def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
        prepare_retirement_receipt(session, handle.operation_id, scope())

    handle = coordinator.begin(
        fixture.TARGET_ID,
        instance_name="Owned retirement",
        kind=ManagedInstanceLifecycleKind.DEPROVISION,
        prior_state=ManagedInstanceSemanticState.STOPPED,
        on_claim=claimed,
    )
    with case.sessions.begin() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID))
        assert lease is not None
        lease.lease_token_digest = "f" * 64
    with pytest.raises(LifecycleOperationLeaseLostError):
        coordinator.terminalize(
            handle,
            state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
            resulting_state=ManagedInstanceSemanticState.UNPROVISIONED,
            on_terminal=lambda *_args: called.append("forbidden"),
        )
    assert not called
    receipt = read_retirement_receipt(case.sessions, handle.operation_id)
    assert receipt.confirmation == "not_confirmed"
    assert receipt.operation_state is ManagedInstanceLifecycleOperationState.RUNNING


def test_receipt_reader_rejects_inconsistent_historical_operation(case: Any) -> None:
    operation_id = uuid4()
    case.lifecycle.deprovision(
        fixture.TARGET_ID, operation_id=operation_id, expected_scope=scope()
    )
    with case.sessions.begin() as session:
        row = session.get(ManagedInstanceLifecycleOperationRow, str(operation_id))
        assert row is not None
        row.kind = ManagedInstanceLifecycleKind.STOP
    with pytest.raises(RuntimeRetirementReceiptError, match="receipt_invalid"):
        read_retirement_receipt(case.sessions, operation_id)

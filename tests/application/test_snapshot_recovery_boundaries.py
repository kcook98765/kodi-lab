from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    ManagedInstanceMutationKind,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotState,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    SnapshotDeleteOperationRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryRow,
)
from kodi_lab.storage.snapshot_recovery import stamp, values
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


@pytest.mark.parametrize("stage", ["preview", "claim"])
@pytest.mark.parametrize("kind", ["duplicate", "unknown"])
def test_operation_refuses_duplicate_or_unknown_inventory(
    recovery_case: Case, stage: str, kind: str
) -> None:
    case = recovery_case
    preview = (
        case.service.preview(case.epoch_id, case.actor) if stage == "claim" else None
    )
    evidence = case.review.normalized()
    record = ManagedContainerRecord(
        evidence["receipt"]["managed_instance_id"]
        if kind == "duplicate"
        else str(uuid4()),
        "9" * 64,
        evidence["native_identity"]["image_id"],
        ContainerRuntimeState.STOPPED,
        ContainerHealthState.NOT_CONFIGURED,
        evidence["selection"]["runtime_revision_id"],
    )
    case.provider.authoritative_records = (
        (record, record) if kind == "duplicate" else (record,)
    )
    with pytest.raises((RecoveryRefused, DevelopmentSessionConflict)):
        if preview is None:
            case.service.preview(case.epoch_id, case.actor)
        else:
            case.service.confirm(
                preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
            )
    with case.sessions() as session:
        assert session.get(DevelopmentSessionRow, 56) is None
        assert all(
            row.consumed_at is None
            for row in session.scalars(select(SnapshotRecoveryRow))
        )
    assert case.provider.calls == [] and case.wire_calls == []


@pytest.mark.parametrize("stage", ["preview", "claim"])
@pytest.mark.parametrize("kind", ["instance", "global"])
@pytest.mark.parametrize("expired", [False, True])
def test_conflicting_leases_are_not_cleared_to_gain_eligibility(
    recovery_case: Case, stage: str, kind: str, expired: bool
) -> None:
    case = recovery_case
    preview = (
        case.service.preview(case.epoch_id, case.actor) if stage == "claim" else None
    )
    target = case.review.normalized()["receipt"]["managed_instance_id"]
    acquired = datetime.now(UTC) - timedelta(minutes=20 if expired else 1)
    expires = stamp(acquired + timedelta(minutes=10))
    operation = str(uuid4())
    with case.sessions.begin() as session:
        lease: ManagedInstanceMutationLeaseRow | GlobalHeavyOperationLeaseRow
        if kind == "instance":
            lease = ManagedInstanceMutationLeaseRow(
                managed_instance_id=target,
                operation_kind=ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
                operation_id=operation,
            )
        else:
            lease = GlobalHeavyOperationLeaseRow(
                singleton_id=1,
                operation_kind=GlobalHeavyOperationKind.SNAPSHOT_DELETE,
                operation_id=operation,
            )
            # Valid global-only owner at the metadata-removed/finish boundary;
            # no snapshot bytes or protected records are deleted by this fixture.
            session.add(
                SnapshotDeleteOperationRow(
                    id=operation,
                    snapshot_id=str(uuid4()),
                    managed_instance_id=target,
                    prior_snapshot_state=SnapshotState.AVAILABLE,
                    requested_at=stamp(acquired),
                    started_at=stamp(acquired),
                    heartbeat_at=stamp(acquired),
                    state=SnapshotDeleteOperationState.RUNNING,
                    phase=SnapshotDeleteOperationPhase.DELETING_METADATA,
                    lease_token_digest="7" * 64,
                    lease_expires_at=expires,
                )
            )
        lease.lease_token_digest, lease.lease_expires_at = "7" * 64, expires
        lease.acquired_at = lease.heartbeat_at = stamp(acquired)
        session.add(lease)
        session.flush()
        before = values(lease)
    with pytest.raises(RecoveryRefused, match="reservation"):
        if preview is None:
            case.service.preview(case.epoch_id, case.actor)
        else:
            case.service.confirm(
                preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
            )
    with case.sessions() as session:
        after = (
            session.get(ManagedInstanceMutationLeaseRow, target)
            if kind == "instance"
            else session.get(GlobalHeavyOperationLeaseRow, 1)
        )
        assert values(after) == before
        assert session.get(DevelopmentSessionRow, 56) is None
        assert all(
            row.consumed_at is None
            for row in session.scalars(select(SnapshotRecoveryRow))
        )
    assert case.provider.calls == [] and case.wire_calls == []


@pytest.mark.parametrize("terminal", ["success", "failure"])
def test_terminal_confirm_replay_returns_status_without_dispatch(
    recovery_case: Case, terminal: str
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    request_id = str(uuid4())
    _, dispatch = case.service.confirm(
        preview["recovery_id"], case.actor, preview["fingerprint"], request_id
    )
    assert dispatch
    if terminal == "failure":
        case.provider.fault = "create_lost_reply"
    case.service.execute(preview["recovery_id"])
    native, wire = list(case.provider.calls), list(case.wire_calls)
    with case.sessions() as session:
        before = [
            values(row)
            for row in session.scalars(
                select(SnapshotRecoveryDispatchRow).order_by(
                    SnapshotRecoveryDispatchRow.id
                )
            )
        ]
    result, dispatch = case.service.confirm(
        preview["recovery_id"], case.actor, preview["fingerprint"], request_id
    )
    assert not dispatch and result["state"] == (
        "succeeded" if terminal == "success" else "reconciliation_required"
    )
    with pytest.raises(RecoveryRefused, match="request_conflict"):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    with case.sessions() as session:
        assert [
            values(row)
            for row in session.scalars(
                select(SnapshotRecoveryDispatchRow).order_by(
                    SnapshotRecoveryDispatchRow.id
                )
            )
        ] == before
    assert case.provider.calls == native and case.wire_calls == wire


def test_expiry_during_artifact_validation_is_rechecked_after_writer_acquisition(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    now = [datetime.now(UTC)]
    case.service.clock = lambda: now[0]
    actual = case.service._artifact

    def slow(snapshot: str, target: str) -> dict[str, object]:
        result = actual(snapshot, target)
        now[0] += timedelta(minutes=6)
        return result

    monkeypatch.setattr(case.service, "_artifact", slow)
    with pytest.raises(RecoveryRefused, match="expired"):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    with case.sessions() as session:
        row = session.get(SnapshotRecoveryRow, preview["recovery_id"])
        assert row is not None and row.consumed_at is None
        assert session.get(DevelopmentSessionRow, 56) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
    assert case.provider.calls == [] and case.wire_calls == []


def test_offline_reviewed_snapshot_substitution_refuses(recovery_case: Case) -> None:
    review = recovery_case.review.model_copy(deep=True)
    review.reviewed_snapshot.snapshot_id = str(uuid4())
    with pytest.raises(RecoveryRefused, match="primary_chain"):
        review.normalized()
    assert recovery_case.provider.calls == []

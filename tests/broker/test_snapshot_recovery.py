from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused, digest
from kodi_lab.broker.docker_inventory import ProvisionedManagedContainerRecord
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.broker.snapshot_recovery import RecoveryStepParams, require_mode_operation
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryEpochRow,
    SnapshotRecoveryRow,
)
from kodi_lab.storage.snapshot_recovery import stamp
from tests.snapshot_recovery_support import Case, profile
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


def test_recovery_create_preserves_failed_successor_and_checks_entry() -> None:
    from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
    from kodi_lab.broker.errors import PostCreateVerificationError
    from tests.broker.test_provision_managed_container import (
        FakeProvisioningDockerClient,
        container_data,
        spec,
    )

    client = FakeProvisioningDockerClient(inspections=[container_data(state="running")])
    entered: list[str] = []
    with pytest.raises(PostCreateVerificationError):
        DockerManagedContainerAdapter(lambda: client).provision_managed_container(
            spec(), recovery_guard=lambda: entered.append("checked")
        )
    assert entered == ["checked"] and len(client.create_calls) == 1
    assert client.remove_calls == []

    refused = FakeProvisioningDockerClient()

    def lost() -> None:
        raise RecoveryRefused("recovery_exact_lease_lost")

    with pytest.raises(RecoveryRefused):
        DockerManagedContainerAdapter(lambda: refused).provision_managed_container(
            spec(), recovery_guard=lost
        )
    assert refused.create_calls == [] and refused.remove_calls == []


def prepare(case: Case) -> tuple[RecoveryStepParams, str]:
    claimed = case.claim()
    attempt, wire = str(uuid4()), str(uuid4())
    with case.sessions.begin() as session:
        row = session.get(SnapshotRecoveryRow, claimed["recovery_id"])
        assert row is not None
        session.add(
            SnapshotRecoveryDispatchRow(
                id=attempt,
                recovery_id=row.id,
                step="provision",
                request_id=wire,
                intent_digest=digest(
                    {
                        "fingerprint": row.fingerprint,
                        "step": "provision",
                        "container": None,
                    }
                ),
                deadline=stamp(datetime.now(UTC) + timedelta(minutes=2)),
                state="prepared",
            )
        )
    return RecoveryStepParams.model_validate(
        {
            "recovery_id": claimed["recovery_id"],
            "attempt_id": attempt,
            "step": "provision",
        }
    ), wire


def test_broker_consumes_actual_wire_once_and_identical_replay_only_reports(
    recovery_case: Case,
) -> None:
    case = recovery_case
    params, wire = prepare(case)
    with pytest.raises(RecoveryRefused, match="dispatch_identity"):
        case.executor.execute(params, str(uuid4()))
    assert case.provider.calls == []
    assert case.executor.execute(params, wire).state == "completed"
    assert case.executor.execute(params, wire).state == "completed"
    assert case.provider.calls == ["provision"]


def test_restricted_broker_stays_restricted_after_epoch_is_closed(
    recovery_case: Case,
) -> None:
    case = recovery_case
    with case.sessions.begin() as session:
        boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
        assert boundary is not None
        boundary.state, boundary.active = "closed", None
    with pytest.raises(RecoveryRefused, match="maintenance_restricted"):
        require_mode_operation(
            case.sessions, case.epoch_id, "provision_managed_container"
        )


@pytest.mark.parametrize("when", ["native_entry", "completion"])
def test_lease_expiry_at_native_entry_or_after_create_never_binds_or_applies(
    recovery_case: Case,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    case = recovery_case
    claimed = case.claim()
    real = case.provider.provision_managed_container

    def expire() -> None:
        with case.sessions.begin() as session:
            heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
            assert heavy is not None
            heavy.lease_expires_at = stamp(datetime.now(UTC) - timedelta(seconds=1))

    def provision(
        spec: ManagedContainerProvisioningSpec,
        *,
        recovery_guard: Callable[[], None] | None = None,
    ) -> ProvisionedManagedContainerRecord:
        if when == "native_entry":
            expire()
        result = real(spec, recovery_guard=recovery_guard)
        if when == "completion":
            expire()
        return result

    monkeypatch.setattr(case.provider, "provision_managed_container", provision)
    case.service.execute(claimed["recovery_id"])
    status = case.service.status(claimed["recovery_id"])
    assert (
        status["state"] == "reconciliation_required" and status["final_state"] is None
    )
    assert "apply" not in case.provider.calls
    if when == "native_entry":
        assert case.provider.cid is None and case.provider.calls == []
    else:
        assert case.provider.cid is not None
        assert (
            status["dispatches"][0]["result"]["unaccepted_create_observation"][
                "container"
            ]
            == case.provider.cid
        )


def test_revoked_administrator_after_claim_refuses_dispatch(
    recovery_case: Case,
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.auth.logout(case.token)
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == []
    assert (
        case.service.status(claimed["recovery_id"])["state"]
        == "reconciliation_required"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "instance_token",
        "heavy_token",
        "instance_kind",
        "heavy_kind",
        "deadline",
        "unknown_successor",
        "consumed",
    ],
)
def test_exact_broker_preconditions_never_enter_native(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    params, wire = prepare(case)
    with case.sessions.begin() as session:
        row = session.get(SnapshotRecoveryRow, str(params.recovery_id))
        assert row is not None
        if fault.startswith("instance"):
            lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
            assert lease is not None
            if fault.endswith("token"):
                lease.lease_token_digest = "0" * 64
            else:
                lease.operation_kind = "snapshot_restore"  # type: ignore[assignment]
        elif fault.startswith("heavy"):
            heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
            assert heavy is not None
            if fault.endswith("token"):
                heavy.lease_token_digest = "0" * 64
            else:
                heavy.operation_kind = "snapshot_restore"  # type: ignore[assignment]
        elif fault in {"deadline", "consumed"}:
            dispatch = session.get(SnapshotRecoveryDispatchRow, str(params.attempt_id))
            assert dispatch is not None
            if fault == "deadline":
                dispatch.deadline = stamp(datetime.now(UTC) - timedelta(seconds=1))
            else:
                dispatch.state = "consumed"
        elif fault == "unknown_successor":
            case.provider.cid = "f" * 64
    if fault == "consumed":
        assert case.executor.execute(params, wire).state == "uncertain"
    else:
        with pytest.raises(RecoveryRefused):
            case.executor.execute(params, wire)
    assert case.provider.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        profile(uid=0),
        profile(mode=0o4600),
        profile(content=b"different"),
        profile(extra=True),
    ],
)
def test_success_reply_cannot_replace_independent_exact_readback(
    recovery_case: Case, payload: bytes
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.provider.payload = payload
    case.service.execute(claimed["recovery_id"])
    assert (
        case.service.status(claimed["recovery_id"])["state"]
        == "reconciliation_required"
    )
    assert "verify" not in case.provider.calls

from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.application.snapshot_recovery_maintenance import (
    ReviewedStateConsumer,
    consumer_metadata_digest,
)
from kodi_lab.snapshot_recovery_host import InstallationPlan, drain
from kodi_lab.storage.models import DevelopmentSessionRow, SnapshotRecoveryRow
from tests.snapshot_recovery_support import Case, ConsumerEngine
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


def test_other_named_bind_volume_cannot_alias_authority_state(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    sdk = case.consumer_engine
    actual = sdk.inspect_volume

    def volume(name: str) -> dict[str, object]:
        if name == "state-alias-volume":
            return {
                "Name": name,
                "Mountpoint": "/other-volume/_data",
                "Driver": "local",
                "Scope": "local",
                "CreatedAt": "2026-01-01T00:00:00Z",
                "Options": {
                    "type": "none",
                    "o": "bind",
                    "device": sdk.volume["Mountpoint"],
                },
            }
        return actual(name)

    monkeypatch.setattr(sdk, "inspect_volume", volume)
    extra = sdk.record("3" * 64, running=True, rw=False)
    extra["Mounts"][0]["Name"] = "state-alias-volume"
    sdk.records["3" * 64] = extra
    with pytest.raises(RecoveryRefused):
        case.service.preview(case.epoch_id, case.actor)
    assert case.provider.calls == [] and sdk.stops == []


@pytest.mark.parametrize(
    "fault",
    [None, "live", "unreviewed", "kernel_live", "parent", "metadata", "restart"],
)
def test_missing_bind_requires_exact_physical_inert_proof(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    case = recovery_case
    sdk = before_drain(case)
    extra = sdk.record("2" * 64, running=fault == "live", exit_code=143)
    missing = str(case.root / "absent-historical-bind")
    extra["Mounts"] = [
        {"Type": "bind", "Source": missing, "Destination": "/old-state", "RW": False}
    ]
    if fault == "parent":
        extra["HostConfig"]["CgroupParent"] = "other"
    if fault == "restart":
        extra["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
    sdk.records["2" * 64] = extra
    plan = host_plan(case, sdk, fault != "unreviewed")

    def kernel(path: str) -> bool:
        if path.endswith("2" * 64):
            if fault == "kernel_live":
                return False
            if fault == "metadata":
                sdk.records["2" * 64]["RestartCount"] += 1
        return True

    monkeypatch.setattr("kodi_lab.snapshot_recovery_host._empty_cgroup", kernel)
    if fault is None:
        seal = drain(plan, sdk)
        assert missing in seal.state_consumers.bind_aliases
        assert seal.state_consumers.bind_aliases[missing] is None
        assert seal.state_consumers.ancillary[0].exit_code == 143
        assert sdk.records["2" * 64]["State"]["ExitCode"] == 143
    else:
        with pytest.raises(RecoveryRefused):
            drain(plan, sdk)
        assert sdk.stops == []


@pytest.mark.parametrize("recovery_case", ["retained_missing_consumer"], indirect=True)
@pytest.mark.parametrize("change", ["none", "new_cid", "restarted_stopped", "metadata"])
def test_ambiguous_source_cannot_be_adopted_by_another_cid(
    recovery_case: Case, change: str
) -> None:
    case = recovery_case
    sdk = case.consumer_engine
    assert None in case.seal.state_consumers.bind_aliases.values()
    if change == "new_cid":
        sdk.records["3" * 64] = copy.deepcopy(sdk.records["2" * 64])
        sdk.records["3" * 64]["Id"] = "3" * 64
    if change == "restarted_stopped":
        sdk.records["2" * 64]["State"]["FinishedAt"] = "2026-02-01T00:00:00Z"
    if change == "metadata":
        sdk.records["2" * 64]["Config"]["Env"] = ["SYNTHETIC=changed"]
    if change == "none":
        receipt = case.claim()
        case.service.execute(receipt["recovery_id"])
        assert case.service.status(receipt["recovery_id"])["resolution_verified"]
    else:
        with pytest.raises(RecoveryRefused):
            case.service.preview(case.epoch_id, case.actor)
        assert case.provider.calls == []
    assert sdk.stops == []


def host_plan(
    case: Case, sdk: ConsumerEngine, reviewed: bool = False
) -> InstallationPlan:
    return InstallationPlan(
        installation="owned-qa",
        bundle_root=str(case.root),
        compose_file=str(case.root / "compose.yml"),
        engine_id="separate-synthetic-engine",
        state_volume="qa-state",
        web_container_id="c" * 64,
        broker_container_id="d" * 64,
        candidate_package_digest=case.seal.candidate_package_digest,
        review=case.review,
        ancillary_consumers=(
            ReviewedStateConsumer(
                container_id="2" * 64,
                metadata_digest=consumer_metadata_digest(sdk.records["2" * 64]),
            ),
        )
        if reviewed
        else (),
    )


def before_drain(case: Case) -> ConsumerEngine:
    sdk = ConsumerEngine(case.root)
    sdk.records = {
        "c" * 64: sdk.record("c" * 64, "web", running=True),
        "d" * 64: sdk.record("d" * 64, "broker", running=True),
    }
    return sdk


@pytest.mark.parametrize(
    "fault",
    [
        "live_rw",
        "live_ro",
        "wrong_project",
        "wrong_role",
        "unreviewed_stopped",
        "uncertain",
        "reviewed_live",
        "parent",
        "kernel_live",
        "metadata_drift",
        "postincident",
        "restart_policy",
        "bind_alias",
        "bind_ancestor",
    ],
)
def test_extra_consumer_refuses_before_either_service_stop(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    case = recovery_case
    sdk = before_drain(case)
    running = fault in {
        "live_rw",
        "live_ro",
        "wrong_project",
        "wrong_role",
        "reviewed_live",
        "bind_alias",
        "bind_ancestor",
    }
    extra = sdk.record("2" * 64, running=running, rw=fault != "live_ro")
    sdk.records["2" * 64] = extra
    reviewed = fault in {
        "reviewed_live",
        "parent",
        "kernel_live",
        "metadata_drift",
        "postincident",
        "restart_policy",
    }
    if fault == "wrong_project":
        extra["Config"]["Labels"]["com.docker.compose.project"] = "other"
    if fault == "wrong_role":
        extra["Config"]["Labels"]["com.docker.compose.service"] = "broker"
    if fault == "uncertain":
        extra["State"]["Status"] = "dead"
    if fault == "parent":
        extra["HostConfig"]["CgroupParent"] = "/different-parent"
    if fault == "postincident":
        extra["State"]["FinishedAt"] = "2099-01-01T00:00:00Z"
    if fault == "restart_policy":
        extra["HostConfig"]["RestartPolicy"]["Name"] = "always"
    if fault in {"bind_alias", "bind_ancestor"}:
        alias = case.root / "state-alias"
        alias.symlink_to(Path(sdk.volume["Mountpoint"]), target_is_directory=True)
        source = str(alias) if fault == "bind_alias" else str(case.root)
        extra["Mounts"] = [
            {
                "Type": "bind",
                "Source": source,
                "Destination": "/read-only-alias",
                "RW": False,
            }
        ]
    plan = host_plan(case, sdk, reviewed)

    def kernel(path: str) -> bool:
        if path.endswith("2" * 64):
            if fault == "kernel_live":
                return False
            if fault == "metadata_drift":
                sdk.records["2" * 64]["Config"]["Labels"]["changed"] = "yes"
        return True

    monkeypatch.setattr("kodi_lab.snapshot_recovery_host._empty_cgroup", kernel)
    with pytest.raises(RecoveryRefused):
        drain(plan, sdk)
    assert sdk.stops == [] and case.provider.calls == [] and case.wire_calls == []
    with case.sessions() as session:
        selected = session.get(DevelopmentSessionRow, 55)
        assert selected is not None and selected.phase == "bound"
        assert all(
            row.consumed_at is None
            for row in session.scalars(select(SnapshotRecoveryRow))
        )


@pytest.mark.parametrize("exit_code", [0, 137, 2])
def test_explicitly_reviewed_kernel_empty_preincident_consumer_is_retained(
    recovery_case: Case, exit_code: int
) -> None:
    case = recovery_case
    sdk = before_drain(case)
    sdk.records["2" * 64] = sdk.record("2" * 64, rw=False, exit_code=exit_code)
    original = copy.deepcopy(sdk.records["2" * 64])
    seal = drain(host_plan(case, sdk, True), sdk)
    assert sdk.stops == ["c" * 64, "d" * 64]
    assert sdk.records["2" * 64] == original
    proof = seal.state_consumers.ancillary[0]
    assert proof.container_id == "2" * 64 and proof.exit_code == exit_code
    assert proof.cgroup == "/docker/" + "2" * 64
    assert proof.metadata_digest == consumer_metadata_digest(original)


@pytest.mark.parametrize("stage", ["preview", "claim", "native_entry"])
@pytest.mark.parametrize("fault", ["live_rw", "live_ro", "unreviewed_stopped"])
def test_live_attestation_rechecks_all_volume_consumers_at_operation_boundaries(
    recovery_case: Case, stage: str, fault: str
) -> None:
    case = recovery_case
    preview = (
        case.service.preview(case.epoch_id, case.actor) if stage == "claim" else None
    )
    claimed = case.claim() if stage == "native_entry" else None
    case.consumer_engine.records["3" * 64] = case.consumer_engine.record(
        "3" * 64, running=fault != "unreviewed_stopped", rw=fault != "live_ro"
    )
    if claimed is not None:
        case.service.execute(claimed["recovery_id"])
        assert (
            case.service.status(claimed["recovery_id"])["state"]
            == "reconciliation_required"
        )
    else:
        with pytest.raises(RecoveryRefused):
            if preview is None:
                case.service.preview(case.epoch_id, case.actor)
            else:
                case.service.confirm(
                    preview["recovery_id"],
                    case.actor,
                    preview["fingerprint"],
                    str(uuid4()),
                )
        with case.sessions() as session:
            assert session.get(DevelopmentSessionRow, 56) is None
            assert all(
                row.consumed_at is None
                for row in session.scalars(select(SnapshotRecoveryRow))
            )
    assert (
        case.provider.calls == []
        and case.wire_calls == []
        and case.consumer_engine.stops == []
    )


@pytest.mark.parametrize("recovery_case", ["retained_consumer"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["restart", "restarted_stopped", "metadata", "removed", "new_consumer"]
)
def test_proven_retained_identity_cannot_be_restarted_changed_or_readopted(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    sdk = case.consumer_engine
    if fault == "restart":
        sdk.records["2" * 64]["State"].update(Running=True, Pid=999, Status="running")
    if fault == "restarted_stopped":
        sdk.records["2" * 64]["State"].update(
            StartedAt="2026-02-01T00:00:00Z", FinishedAt="2026-02-02T00:00:00Z"
        )
    if fault == "metadata":
        sdk.records["2" * 64]["Config"]["Env"] = ["SYNTHETIC=changed"]
    if fault == "removed":
        del sdk.records["2" * 64]
    if fault == "new_consumer":
        sdk.records["3" * 64] = sdk.record("3" * 64)
    with pytest.raises(RecoveryRefused):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    assert case.provider.calls == [] and case.wire_calls == []


@pytest.mark.parametrize("recovery_case", ["retained_consumer"], indirect=True)
@pytest.mark.parametrize("retired", ["retained", "recreated"])
def test_current_pair_and_exact_retired_pair_with_inert_ancillary_handover(
    recovery_case: Case, retired: str
) -> None:
    case = recovery_case
    if retired == "recreated":
        del case.consumer_engine.records["c" * 64]
        del case.consumer_engine.records["d" * 64]
    receipt = case.claim()
    case.service.execute(receipt["recovery_id"])
    assert case.service.status(receipt["recovery_id"])["resolution_verified"]
    assert case.consumer_engine.records["2" * 64]["State"]["ExitCode"] == 137
    assert case.consumer_engine.stops == []


def test_old_retired_service_cannot_return_as_unapproved_live_writer(
    recovery_case: Case,
) -> None:
    case = recovery_case
    case.consumer_engine.records["c" * 64]["State"].update(
        Running=True, Pid=777, Status="running"
    )
    with pytest.raises(RecoveryRefused):
        case.service.preview(case.epoch_id, case.actor)
    assert case.provider.calls == []


@pytest.mark.parametrize("recovery_case", ["retained_bind_consumer"], indirect=True)
def test_physically_resolved_retained_bind_alias_stays_private_and_inert(
    recovery_case: Case,
) -> None:
    case = recovery_case
    assert case.seal.state_consumers.bind_aliases
    reply = case.snapshot_recovery_attestation(case.epoch_id, str(uuid4()))
    serialized = reply.model_dump_json()
    assert str(case.root) not in serialized and "SYNTHETIC=1" not in serialized
    assert "Mounts" not in serialized and "Env" not in serialized
    receipt = case.claim()
    case.service.execute(receipt["recovery_id"])
    assert case.service.status(receipt["recovery_id"])["resolution_verified"]


def test_consumer_appearing_during_drain_refuses_seal_without_cleanup(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    sdk = before_drain(case)
    actual = sdk.stop

    def stop(cid: str, *, timeout: int) -> None:
        actual(cid, timeout=timeout)
        if cid == "d" * 64:
            sdk.records["3" * 64] = sdk.record("3" * 64, running=True)

    monkeypatch.setattr(sdk, "stop", stop)
    with pytest.raises(RecoveryRefused, match="unapproved_state_consumer"):
        drain(host_plan(case, sdk), sdk)
    assert sdk.stops == ["c" * 64, "d" * 64]
    assert sdk.records["3" * 64]["State"]["Running"]
    assert case.provider.calls == []

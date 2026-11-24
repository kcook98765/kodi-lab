from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedResourcePolicyRow,
    ManagedVersionOperationRow,
    RuntimeContainerArtifactRow,
    RuntimeRecipeInstallationRow,
    RuntimeRevisionRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryEpochRow,
    SnapshotRecoveryResolutionRow,
    SnapshotRecoveryRow,
    SnapshotRestoreOperationRow,
    SnapshotRow,
)
from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository
from kodi_lab.storage.snapshot_recovery import stamp, values
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case


@pytest.mark.parametrize("recovery_case", [True], indirect=True)
def test_preview_profile_is_from_original_bytes_not_current_workspace(
    recovery_case: Case,
) -> None:
    case = recovery_case
    with case.sessions.begin() as session:
        selected = session.get(DevelopmentSessionRow, 55)
        assert selected is not None
        workspace = session.get(AgentWorkspaceRow, selected.workspace_id)
        assert workspace is not None
        workspace.source_observed_version = "2.1.1"
    preview = case.service.preview(case.epoch_id, case.actor)
    assert preview["artifact"]["snapshot_profile"] == {
        "selected_skin": "skin.synthetic",
        "selected_skin_version": "2.1.0",
        "active_profile": "Synthetic master",
        "metadata": "read_from_exact_snapshot",
    }
    assert preview["reviewed_bindings"]["workspace_observed_version"] == "2.1.1"
    assert preview["artifact"]["manifest_length"] > 0
    assert preview["artifact"]["ownership"] == [1000, 1000]
    assert (
        preview["final_state"] is None and preview["intended_final_state"] == "stopped"
    )


def test_revoked_review_and_changed_fingerprint_cannot_claim(
    recovery_case: Case,
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    with pytest.raises(RecoveryRefused, match="mismatch"):
        case.service.confirm(preview["recovery_id"], case.actor, "0" * 64, str(uuid4()))
    case.service.revoke(preview["recovery_id"], case.actor)
    with pytest.raises(RecoveryRefused, match="revoked"):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    assert case.provider.calls == []


@pytest.mark.parametrize(
    "fault",
    [
        "workspace_generation",
        "workspace_baseline",
        "source_locator",
        "source_identity",
        "policy",
    ],
)
def test_retained_attribution_refuses_drift_before_first_preview(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    selected = case.review.normalized()["selection"]
    with case.sessions.begin() as session:
        if fault.startswith("workspace"):
            workspace = session.get(AgentWorkspaceRow, selected["workspace_id"])
            assert workspace is not None
            if fault == "workspace_generation":
                workspace.generation += 1
            else:
                workspace.baseline_artifact_id = str(uuid4())
        elif fault.startswith("source"):
            source = session.get(DevelopmentSourceRow, selected["source_id"])
            assert source is not None
            if fault == "source_locator":
                source.relative_locator += "-drift"
            else:
                source.logical_name += " changed"
        else:
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.retain_profiles = False
            policy.retained_omega_target_id = None
    with pytest.raises(RecoveryRefused, match="retained_attribution_changed"):
        case.service.preview(case.epoch_id, case.actor)
    assert case.provider.calls == []


def test_dispatcher_preserves_first_terminal_recovery_failure(
    recovery_case: Case,
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.service.fence(claimed["recovery_id"], "original_specific_failure")
    with case.sessions.begin() as session:
        for lease in (
            session.get(ManagedInstanceMutationLeaseRow, claimed["target_id"]),
            session.get(GlobalHeavyOperationLeaseRow, 1),
        ):
            assert lease is not None
            lease.lease_expires_at = stamp(datetime.now(UTC) - timedelta(seconds=1))
    dispatcher = StaleOperationReconciliationDispatcher(case.sessions)
    dispatcher.sweep_startup()
    dispatcher.sweep_startup()
    case.service.interrupted()
    assert (
        case.service.status(claimed["recovery_id"])["failure_code"]
        == "original_specific_failure"
    )


@pytest.mark.parametrize(
    "kind,state",
    [
        ("recipe", "pending"),
        ("recipe", "acquiring"),
        ("recipe", "assembling"),
        ("recipe", "validating"),
        ("capture", "staging"),
        ("capture", "switching"),
        ("version", "accepted"),
        ("version", "prepared"),
        ("version", "provisioning"),
        ("version", "starting"),
        ("version", "stopping"),
    ],
)
def test_actual_nonterminal_worker_vocabularies_refuse_without_any_lease(
    recovery_case: Case,
    kind: str,
    state: str,
) -> None:
    case = recovery_case
    now = stamp(datetime.now(UTC))
    with case.sessions.begin() as session:
        selected = session.get(DevelopmentSessionRow, 55)
        assert selected is not None
        if kind == "recipe":
            session.add(
                RuntimeRecipeInstallationRow(
                    id=str(uuid4()),
                    recipe_id="synthetic",
                    recipe_revision=1,
                    recipe_digest="sha256:" + "1" * 64,
                    generation="nexus",
                    state=state,
                    created_at=now,
                    updated_at=now,
                )
            )
        elif kind == "capture":
            session.add(
                AgentWorkspaceCaptureOperationRow(
                    id=str(uuid4()),
                    workspace_id=selected.workspace_id,
                    development_source_id=selected.source_id,
                    state=state,
                    prior_generation=1,
                    resulting_generation=2,
                    prior_baseline_id=str(uuid4()),
                    prior_baseline_digest="sha256:" + "1" * 64,
                    prior_tree_digest="sha256:" + "2" * 64,
                    captured_digest="sha256:" + "3" * 64,
                    requested_at=now,
                )
            )
        else:
            session.add(
                ManagedVersionOperationRow(
                    id=str(uuid4()),
                    client_id=str(uuid4()),
                    request_id=str(uuid4()),
                    generation="omega",
                    target_id=selected.target_id,
                    action="start",
                    phase=state,
                    code="pending",
                    created_at=now,
                    updated_at=now,
                )
            )
    with pytest.raises(RecoveryRefused, match="queued_or_live"):
        case.service.preview(case.epoch_id, case.actor)
    assert case.provider.calls == []
    with case.sessions() as session:
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_new_one_use_success_preserves_failure_history_and_resolves_only_it(
    recovery_case: Case,
) -> None:
    case = recovery_case
    original = case.review.normalized()["receipt"]
    claimed = case.claim()
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == ["provision", "apply", "verify"]
    with case.sessions() as session:
        assert (
            values(session.get(SnapshotRestoreOperationRow, original["id"])) == original
        )
        old, new = (
            session.get(DevelopmentSessionRow, 55),
            session.get(DevelopmentSessionRow, 56),
        )
        assert old is not None and new is not None
        assert (
            old.phase == "closed" and old.container_id == original["old_container_id"]
        )
        assert new.phase == "bound" and new.container_id == case.provider.cid
        assert (old.source_id, old.workspace_id) == (new.source_id, new.workspace_id)
        row = session.get(SnapshotRecoveryRow, claimed["recovery_id"])
        assert row is not None and row.state == "succeeded"
        assert session.get(SnapshotRecoveryResolutionRow, original["id"]) is not None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
        assert session.get(ManagedInstanceMutationLeaseRow, row.target_id) is None
        assert (
            not ManagedInstanceReconciliationService(case.sessions)
            .status(UUID(row.target_id))
            .blocking
        )
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == ["provision", "apply", "verify"]


@pytest.mark.parametrize(
    "fault", ["workspace", "pin", "snapshot", "epoch", "consent", "actor", "inventory"]
)
def test_stale_preview_refuses_before_handoff(recovery_case: Case, fault: str) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    with case.sessions.begin() as session:
        saved = session.get(SnapshotRecoveryRow, preview["recovery_id"])
        assert saved is not None
        proof = json.loads(saved.proof_json)
        if fault == "workspace":
            row = session.get(AgentWorkspaceRow, proof["selection"]["workspace_id"])
            assert row is not None
            row.generation += 1
        elif fault == "pin":
            row2 = session.get(ManagedInstanceRow, preview["target_id"])
            assert row2 is not None
            row2.fixture_definition_id = str(uuid4())
        elif fault == "snapshot":
            snapshot = session.get(SnapshotRow, preview["snapshot_id"])
            assert snapshot is not None
            snapshot.manifest_digest = "sha256:" + "0" * 64
        elif fault == "epoch":
            boundary = session.get(SnapshotRecoveryEpochRow, case.epoch_id)
            assert boundary is not None
            boundary.broker_owner = "replacement"
    if fault == "consent":
        case.service.clock = lambda: datetime.now(UTC) + timedelta(minutes=6)
    if fault == "actor":
        case.auth.logout(case.token)
    if fault == "inventory":
        case.provider.cid = "f" * 64
    with pytest.raises((RecoveryRefused, DevelopmentSessionConflict)):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    with case.sessions() as session:
        old = session.get(DevelopmentSessionRow, 55)
        assert old is not None and old.phase == "bound"
    assert case.provider.calls == []


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "workspace_bytes",
        "revision",
        "image",
        "policy",
        "resources",
        "session",
        "approval",
    ],
)
def test_reviewed_source_runtime_policy_and_session_drift(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    with case.sessions.begin() as session:
        saved = session.get(SnapshotRecoveryRow, preview["recovery_id"])
        assert saved is not None
        proof = json.loads(saved.proof_json)
        if fault == "source":
            source = session.get(DevelopmentSourceRow, proof["selection"]["source_id"])
            assert source is not None
            source.relative_locator += "-changed"
        elif fault == "workspace_bytes":
            workspace = session.get(
                AgentWorkspaceRow, proof["selection"]["workspace_id"]
            )
            assert workspace is not None
            workspace.current_tree_digest = "sha256:" + "0" * 64
        elif fault == "revision":
            instance = session.get(ManagedInstanceRow, preview["target_id"])
            assert instance is not None
            instance.runtime_revision_id = str(uuid4())
        elif fault == "image":
            artifact = session.get(
                RuntimeContainerArtifactRow, proof["selection"]["runtime_revision_id"]
            )
            assert artifact is not None
            artifact.image_id = "sha256:" + "0" * 64
        elif fault == "policy":
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.retain_profiles = False
            policy.retained_omega_target_id = None
        elif fault == "resources":
            resources = session.get(ManagedResourcePolicyRow, 1)
            assert resources is not None
            resources.max_running += 1
        elif fault == "session":
            selected = session.get(DevelopmentSessionRow, 55)
            assert selected is not None
            selected.request_id = str(uuid4())
        elif fault == "approval":
            revision = session.get(
                RuntimeRevisionRow, proof["selection"]["runtime_revision_id"]
            )
            assert revision is not None
            from kodi_lab.domain import RevisionApprovalState

            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
    with pytest.raises((RecoveryRefused, DevelopmentSessionConflict)):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    assert case.provider.calls == []


@pytest.mark.parametrize("stage", ["preview", "claim"])
@pytest.mark.parametrize(
    "fault",
    ["dead", "old", "owner", "build", "engine", "volume", "challenge", "installation"],
)
def test_live_specific_broker_proof_is_required_before_handoff(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch, stage: str, fault: str
) -> None:
    case = recovery_case
    reviewed = (
        case.service.preview(case.epoch_id, case.actor) if stage == "claim" else None
    )
    real = case.snapshot_recovery_attestation

    def wrong(epoch_id: str, challenge: str) -> object:
        if fault in {"dead", "old"}:
            raise ConnectionError("old or dead broker cannot attest")
        result = real(epoch_id, challenge)
        fields = {
            "owner": "broker_owner",
            "build": "package_digest",
            "engine": "engine_id",
            "volume": "state_volume_digest",
            "challenge": "challenge",
            "installation": "installation",
        }
        return result.model_copy(update={fields[fault]: "wrong"})

    monkeypatch.setattr(case, "snapshot_recovery_attestation", wrong)
    with pytest.raises(RecoveryRefused, match="live_broker"):
        if reviewed is None:
            case.service.preview(case.epoch_id, case.actor)
        else:
            case.service.confirm(
                reviewed["recovery_id"],
                case.actor,
                reviewed["fingerprint"],
                str(uuid4()),
            )
    with case.sessions() as session:
        selected = session.get(DevelopmentSessionRow, 55)
        assert selected is not None and selected.phase == "bound"
        assert session.get(DevelopmentSessionRow, 56) is None
    assert case.provider.calls == []


def test_expiry_after_waiting_for_sqlite_writer_refuses_before_claim(
    recovery_case: Case,
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    now = [datetime.now(UTC)]
    case.service.clock = lambda: now[0]
    entering = Event()

    def claim() -> None:
        entering.set()
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )

    with ThreadPoolExecutor(max_workers=1) as worker:
        with case.sessions.begin() as held:
            held.execute(text("BEGIN IMMEDIATE"))
            future = worker.submit(claim)
            assert entering.wait(2)
            now[0] += timedelta(minutes=6)
        with pytest.raises(RecoveryRefused, match="expired"):
            future.result(timeout=10)
    assert case.provider.calls == []


@pytest.mark.parametrize(
    "fault", ["digest", "dispatch", "session", "target", "verification"]
)
def test_wrong_or_partial_typed_relation_never_unlocks_original(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.service.execute(claimed["recovery_id"])
    with case.sessions.begin() as session:
        if fault in {"digest", "target"}:
            relation = session.get(
                SnapshotRecoveryResolutionRow, claimed["incident_id"]
            )
            assert relation is not None
            if fault == "digest":
                relation.proof_digest = "0" * 64
            else:
                relation.recovery_id = str(uuid4())
        elif fault == "session":
            successor = session.get(DevelopmentSessionRow, 56)
            assert successor is not None
            successor.container_id = "0" * 64
        else:
            dispatch = session.scalar(
                select(SnapshotRecoveryDispatchRow).where(
                    SnapshotRecoveryDispatchRow.step == "verify"
                )
            )
            assert dispatch is not None
            if fault == "dispatch":
                dispatch.state = "consumed"
            else:
                dispatch.result_json = "{}"
    assert (
        ManagedInstanceReconciliationService(case.sessions)
        .status(UUID(claimed["target_id"]))
        .blocking
    )
    with case.sessions() as session:
        assert ManagedRuntimeProjectionRepository(
            session
        ).has_blocking_runtime_reconciliation(UUID(claimed["target_id"]))


def test_resolved_newer_rows_cannot_hide_older_unresolved_restore(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = recovery_case
    original = case.review.normalized()["receipt"]
    newer_ids = {str(uuid4()), str(uuid4())}
    with case.sessions.begin() as session:
        for index, new_id in enumerate(newer_ids):
            session.add(
                SnapshotRestoreOperationRow(
                    **{
                        **original,
                        "id": new_id,
                        "requested_at": stamp(
                            datetime.now(UTC) + timedelta(seconds=index + 1)
                        ),
                    }
                )
            )
    # Closure itself has independent positive/negative tests. This classifier
    # double isolates the LIMIT-before-filter bug against real repository SQL.
    monkeypatch.setattr(
        "kodi_lab.application.reconciliation._snapshot_recovery_resolved",
        lambda _session, incident: incident in newer_ids,
    )
    status = ManagedInstanceReconciliationService(case.sessions).status(
        UUID(original["managed_instance_id"])
    )
    assert status.blocking and str(status.operation_uuid) == original["id"]


def test_concurrent_identical_and_conflicting_claims_dispatch_once(
    recovery_case: Case,
) -> None:
    case = recovery_case
    preview = case.service.preview(case.epoch_id, case.actor)
    request_id = str(uuid4())

    def claim() -> bool:
        return case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], request_id
        )[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
    with pytest.raises(RecoveryRefused, match="request_conflict"):
        case.service.confirm(
            preview["recovery_id"], case.actor, preview["fingerprint"], str(uuid4())
        )
    case.service.execute(preview["recovery_id"])
    assert case.provider.calls == ["provision", "apply", "verify"]


@pytest.mark.parametrize("fault", ["create_lost_reply", "apply", "verify"])
def test_uncertain_dispatch_is_fenced_and_restart_never_resends(
    recovery_case: Case, fault: str
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.provider.fault = fault
    case.service.execute(claimed["recovery_id"])
    calls = list(case.provider.calls)
    case.service.interrupted()
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == calls
    with case.sessions() as session:
        row = session.get(SnapshotRecoveryRow, claimed["recovery_id"])
        assert row is not None and row.state == "reconciliation_required"
        assert session.get(SnapshotRecoveryResolutionRow, row.incident_id) is None
        assert session.get(ManagedInstanceMutationLeaseRow, row.target_id) is not None
        assert any(
            d.state == "uncertain"
            for d in session.scalars(select(SnapshotRecoveryDispatchRow))
        )


@pytest.mark.parametrize("which", ["instance", "heavy"])
def test_exact_lease_loss_and_stale_sweep_keep_fences(
    recovery_case: Case, which: str
) -> None:
    case = recovery_case
    claimed = case.claim()
    with case.sessions.begin() as session:
        row = (
            session.get(ManagedInstanceMutationLeaseRow, claimed["target_id"])
            if which == "instance"
            else session.get(GlobalHeavyOperationLeaseRow, 1)
        )
        assert row is not None
        row.lease_expires_at = stamp(datetime.now(UTC) - timedelta(seconds=1))
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == []
    StaleOperationReconciliationDispatcher(case.sessions).sweep_startup()
    with case.sessions() as session:
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is not None
        assert (
            session.get(ManagedInstanceMutationLeaseRow, claimed["target_id"])
            is not None
        )


def test_interrupted_before_dispatch_requires_review_without_native_work(
    recovery_case: Case,
) -> None:
    case = recovery_case
    claimed = case.claim()
    case.service.interrupted()
    case.service.execute(claimed["recovery_id"])
    assert case.provider.calls == []
    assert (
        case.service.status(claimed["recovery_id"])["state"]
        == "reconciliation_required"
    )

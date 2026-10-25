"""Durable evidence selection does not dispatch or grant lifecycle authority."""

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.application.retained_diagnostics import (
    DiagnosticCaptureInput,
    DiagnosticDiscardInput,
    RetainedDiagnosticsError,
    RetainedDiagnosticsService,
)
from kodi_lab.application.session_replacement_reviews import (
    ReplacementEvidenceSelection,
    ReplacementReviewCancel,
    ReplacementReviewRequest,
    SessionReplacementReviewError,
    SessionReplacementReviewService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import RuntimeContainerArtifact
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionEvidenceHoldRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    ManagedDiagnosticCaptureRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    TargetRepository,
)
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_development_sessions import (
    environment as session_environment,
)
from tests.application.test_retained_diagnostics import NOW, snapshot
from tests.storage.test_repositories import make_target


def environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_path: Path | None = None,
) -> Any:
    state = session_environment(tmp_path, monkeypatch, database_path=database_path)
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    with state.env.sessions.begin() as session:
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(
                runtime_revision_id=fixture.RUNTIME_REVISION_ID,
                image_id=fixture.IMAGE_ID,
            )
        )
    state.service.reserve(state.request)
    state.provision()

    def inventory(**_kwargs: object) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=item.managed_instance_id,
                    runtime_revision_id=item.runtime_revision_id,
                    container_id=item.container_id,
                    image_id=item.image_id,
                    runtime_state=item.runtime_state,
                    health_state=item.health_state,
                )
                for item in state.records
            ]
        )

    state.service._inventory = inventory
    logs = snapshot()
    logs.exit_metadata = logs.exit_metadata.model_copy(
        update=dict(
            managed_instance_id=str(fixture.TARGET_ID),
            runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
            container_id=fixture.CONTAINER_ID,
            image_id=fixture.IMAGE_ID,
        )
    )
    reads: list[object] = []

    def read(**kwargs: object) -> Any:
        reads.append(kwargs)
        return logs

    retained = RetainedDiagnosticsService(
        state.env.sessions,
        SimpleNamespace(
            managed_container_inventory=inventory,
            read_stopped_managed_logs=read,
        ),
        data_root=root,
        clock=lambda: NOW,
    )
    capture = retained.capture(
        fixture.TARGET_ID,
        DiagnosticCaptureInput(
            request_id=uuid4(),
            expected_runtime_revision_id=fixture.RUNTIME_REVISION_ID,
            expected_container_id=fixture.CONTAINER_ID,
            retain_redacted_logs=True,
        ),
    )
    capture_hash = capture.payload_sha256
    assert capture_hash is not None
    reviews = SessionReplacementReviewService(state.service)

    def request() -> ReplacementReviewRequest:
        offer = reviews.offer(fixture.TARGET_ID)
        return ReplacementReviewRequest(
            **offer.model_dump(),
            request_id=uuid4(),
            confirm_review=True,
            acknowledge_uncollected_crash_reports=True,
            evidence=[
                ReplacementEvidenceSelection(
                    capture_id=capture.capture_id, payload_sha256=capture_hash
                )
            ],
        )

    discard = DiagnosticDiscardInput(
        expected_state="retained",
        expected_payload_sha256=capture.payload_sha256,
        confirm_discard=True,
    )
    return SimpleNamespace(
        state=state,
        reviews=reviews,
        retained=retained,
        capture=capture,
        request=request,
        discard=discard,
        reads=reads,
    )


def test_review_replay_holds_exact_evidence_without_touching_session_or_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    before = env.state.service.status()
    result = env.reviews.create(request)
    assert env.reviews.create(request) == result == env.reviews.get(result.review_id)
    assert result.snapshot.mode == "fresh_test" and not result.executes_replacement
    assert result.snapshot.profile_disposal_authorized is False
    assert result.snapshot.native_crash_reports == "not_collected"
    assert env.state.service.status() == before and len(env.state.records) == 1
    assert len(env.reads) == 1  # Review never rereads logs or dispatches a lifecycle.
    with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
        env.retained.discard(env.capture.capture_id, env.discard)
    restarted = SessionReplacementReviewService(env.state.service)
    assert restarted.active() == result
    with env.state.env.sessions() as session:
        assert (
            len(session.scalars(select(DevelopmentSessionEvidenceHoldRow)).all()) == 1
        )


def test_cancel_releases_only_its_holds_and_retains_receipt_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    result = env.reviews.create(request)
    with pytest.raises(SessionReplacementReviewError, match="review_changed"):
        env.reviews.cancel(
            result.review_id,
            ReplacementReviewCancel(
                expected_snapshot_sha256="f" * 64, confirm_cancel=True
            ),
        )
    cancel = ReplacementReviewCancel(
        expected_snapshot_sha256=result.snapshot_sha256, confirm_cancel=True
    )
    cancelled = env.reviews.cancel(result.review_id, cancel)
    assert cancelled.state == "cancelled" and cancelled.cancelled_at is not None
    assert env.reviews.cancel(result.review_id, cancel) == cancelled
    assert env.reviews.create(request) == cancelled and env.reviews.active() is None
    assert (
        env.retained.discard(env.capture.capture_id, env.discard).state == "discarded"
    )
    assert env.state.service.status().phase == "bound" and len(env.state.records) == 1


@pytest.mark.parametrize(
    "change", ["container", "runtime", "workspace", "generation", "image", "next_pin"]
)
def test_stale_displayed_selection_refuses_without_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    field, value = {
        "container": ("expected_container_id", "d" * 64),
        "runtime": ("expected_runtime_revision_id", uuid4()),
        "workspace": ("expected_workspace_generation", 2),
        "generation": ("expected_generation", 2),
        "image": ("expected_image_id", "sha256:" + "d" * 64),
        "next_pin": ("next_runtime_revision_id", uuid4()),
    }[change]
    with pytest.raises(SessionReplacementReviewError, match="selection_changed"):
        env.reviews.create(request.model_copy(update={field: value}))
    assert env.reviews.active() is None and env.retained.read(env.capture.capture_id)


@pytest.mark.parametrize(
    "change",
    ["running", "missing", "unrelated", "old_pin", "uncertain", "pending", "workspace"],
)
def test_current_races_uncertainty_and_other_resources_block_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    if change == "running":
        env.state.records[:] = [
            replace(env.state.records[0], runtime_state=ContainerRuntimeState.RUNNING)
        ]
    elif change == "missing":
        env.state.records.clear()
    elif change == "unrelated":
        env.state.records.append(
            replace(
                env.state.records[0],
                managed_instance_id=str(uuid4()),
                container_id="e" * 64,
            )
        )
    else:
        with env.state.env.sessions.begin() as session:
            if change == "old_pin":
                session.get(
                    ManagedInstanceRow, str(fixture.TARGET_ID)
                ).instance_type = "persistent"
            elif change == "uncertain":
                session.get(DevelopmentSessionRow, 1).phase = "uncertain"
            elif change == "pending":
                session.add(
                    ManagedProvisionReservationRow(
                        managed_instance_id=str(fixture.TARGET_ID),
                        runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                        operation_id=str(uuid4()),
                        phase="uncertain",
                        session_generation=1,
                        created_at="2026-10-25T00:00:00.000000Z",
                    )
                )
            else:
                session.get(
                    AgentWorkspaceRow, str(env.state.env.workspace_id)
                ).generation += 1
    with pytest.raises((SessionReplacementReviewError, DevelopmentSessionConflict)):
        env.reviews.create(request)
    assert env.reviews.active() is None and len(env.reads) == 1


@pytest.mark.parametrize("change", ["discarded", "digest", "body", "binding"])
def test_evidence_must_still_be_retained_exact_and_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    if change == "discarded":
        env.retained.discard(env.capture.capture_id, env.discard)
    elif change == "digest":
        request.evidence[0].payload_sha256 = "a" * 64
    else:
        with env.state.env.sessions.begin() as session:
            row = session.get(ManagedDiagnosticCaptureRow, str(env.capture.capture_id))
            if change == "body":
                row.payload_json = "{}"
            else:
                row.container_id = "e" * 64
    with pytest.raises(
        SessionReplacementReviewError, match=r"evidence_(changed|invalid)"
    ):
        env.reviews.create(request)
    assert env.reviews.active() is None


def test_switch_retains_one_workspace_and_immutable_new_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    target = make_target(name="Other version")
    with env.state.env.sessions.begin() as session:
        original = ManagedInstanceRepository(session).get(fixture.TARGET_ID)
        assert original is not None
        TargetRepository(session).create(target)
        ManagedInstanceRepository(session).create(
            replace(original, target_id=target.id)
        )
    offer = env.reviews.offer(target.id)
    request = env.request().model_copy(update=offer.model_dump())
    review = env.reviews.create(request)
    assert review.snapshot.mode == "switch_version"
    assert review.snapshot.workspace_id == env.state.env.workspace_id
    assert review.snapshot.request.next_target_id == target.id
    assert (
        len(env.state.records) == 1
        and env.state.service.status().target_id == fixture.TARGET_ID
    )


def test_concurrent_review_claim_and_discard_serialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()

    def claim_or_discard(index: int) -> str:
        try:
            if index == 0:
                env.reviews.create(request)
                return "held"
            env.retained.discard(env.capture.capture_id, env.discard)
            return "discarded"
        except (SessionReplacementReviewError, RetainedDiagnosticsError):
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim_or_discard, (0, 1)))
    assert sorted(results) in (["held", "refused"], ["discarded", "refused"])
    if "held" in results:
        assert env.retained.read(env.capture.capture_id).capture.state == "retained"


def test_open_review_blocks_absent_close_without_releasing_any_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    env.reviews.create(env.request())
    env.state.records.clear()
    with pytest.raises(DevelopmentSessionConflict, match="replacement_review_open"):
        env.state.service.close_absent(1)
    assert env.state.service.status().phase == "bound"


@pytest.mark.parametrize("value", [False, 1, "true"])
def test_review_acknowledgements_are_exact_and_inputs_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    env = environment(tmp_path, monkeypatch)
    body = env.request().model_dump()
    for key in ("confirm_review", "acknowledge_uncollected_crash_reports"):
        with pytest.raises(ValidationError):
            ReplacementReviewRequest.model_validate({**body, key: value})
    with pytest.raises(ValidationError):
        ReplacementReviewRequest.model_validate({**body, "delete_profile": True})
    with pytest.raises(ValidationError):
        ReplacementReviewRequest.model_validate(
            {**body, "evidence": body["evidence"] * 2}
        )


def test_review_request_identity_and_saved_digest_cannot_be_rebound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    result = env.reviews.create(request)
    with pytest.raises(SessionReplacementReviewError, match="request_conflict"):
        env.reviews.create(request.model_copy(update={"expected_generation": 2}))
    with pytest.raises(SessionReplacementReviewError, match="already_open"):
        env.reviews.create(request.model_copy(update={"request_id": uuid4()}))
    with env.state.env.sessions.begin() as session:
        session.get(
            DevelopmentSessionReplacementReviewRow, str(result.review_id)
        ).snapshot_json = "{}"
    with pytest.raises(SessionReplacementReviewError, match="review_invalid"):
        env.reviews.get(result.review_id)
    with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
        env.retained.discard(env.capture.capture_id, env.discard)


def test_reconciliation_is_observed_without_clearing_its_durable_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    with env.state.env.sessions.begin() as session:
        row = session.get(
            DevelopmentSourceDeploymentOperationRow, str(fixture.BASELINE_OPERATION_ID)
        )
        assert row is not None
        row.state = "reconciliation_required"
        row.reconciliation_required = True
        row.failure_reason = "broker_runtime_dispatch_unconfirmed"
    with pytest.raises(SessionReplacementReviewError, match="reconciliation_required"):
        env.reviews.create(request)
    with env.state.env.sessions() as session:
        row = session.get(
            DevelopmentSourceDeploymentOperationRow, str(fixture.BASELINE_OPERATION_ID)
        )
        assert row is not None and row.state.value == "reconciliation_required"
    assert env.reviews.active() is None


def test_committed_review_survives_cancellation_and_replays_without_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    commit = Session.commit

    def cancelled_after_commit(session: Session) -> None:
        commit(session)
        raise KeyboardInterrupt

    monkeypatch.setattr(Session, "commit", cancelled_after_commit)
    with pytest.raises(KeyboardInterrupt):
        env.reviews.create(request)
    result = env.reviews.active()
    assert result is not None
    env.state.records.clear()
    assert env.reviews.create(request) == result
    with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
        env.retained.discard(env.capture.capture_id, env.discard)


def test_review_capacity_never_overwrites_cancelled_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import session_replacement_reviews as module

    env = environment(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MAX_REVIEWS", 1)
    result = env.reviews.create(env.request())
    env.reviews.cancel(
        result.review_id,
        ReplacementReviewCancel(
            expected_snapshot_sha256=result.snapshot_sha256,
            confirm_cancel=True,
        ),
    )
    with pytest.raises(SessionReplacementReviewError, match="history_capacity"):
        env.reviews.create(env.request())
    assert env.reviews.get(result.review_id).state == "cancelled"


def test_review_checks_storage_before_claim_without_exposing_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(SessionReplacementReviewError, match="storage_reserve"):
        env.reviews.create(request)
    assert env.reviews.active() is None


def test_concurrent_different_reviews_cannot_claim_two_open_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path, monkeypatch)
    request = env.request()

    def claim(index: int) -> str:
        try:
            env.reviews.create(request.model_copy(update={"request_id": uuid4()}))
            return "held"
        except SessionReplacementReviewError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert sorted(results) == ["held", "session_replacement_review_already_open"]
    assert env.retained.read(env.capture.capture_id).capture.state == "retained"

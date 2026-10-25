"""One real durable admission policy spans fake sequential profile replacement."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.retained_diagnostics import (
    DiagnosticCaptureInput,
    RetainedDiagnosticsError,
)
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.application.session_replacement_reviews import (
    ReplacementEvidenceSelection,
    ReplacementReviewCancel,
    ReplacementReviewRequest,
    SessionReplacementReviewError,
)
from kodi_lab.application.session_replacements import (
    SessionReplacementError,
    SessionReplacementRequest,
    SessionReplacementService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError, RuntimeImageUnavailableError
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ProvisionManagedContainerResult,
)
from kodi_lab.domain import RuntimeContainerArtifact, RuntimeDefinition
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedProvisionReservationRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_retained_diagnostics import snapshot
from tests.application.test_runtime_retirement_receipts import (
    Broker as RetirementBroker,
)
from tests.application.test_session_replacement_reviews import (
    environment as review_environment,
)
from tests.storage.test_repositories import make_target


class Broker(RetirementBroker):
    def __init__(self, state: Any) -> None:
        super().__init__(state)
        self.create_mode = "success"
        self.creates: list[tuple[str, str, str]] = []
        self.removed: list[str] = []
        self.block_removal: tuple[Event, Event] | None = None

    def deprovision_managed_container(self, **kwargs: object) -> Any:
        if self.block_removal is not None:
            entered, release = self.block_removal
            entered.set()
            assert release.wait(10)
        self.removed.append(str(kwargs["expected_container_id"]))
        self.calls.append(kwargs)
        if self.mode != "remaining":
            self.state.records.clear()
        if self.mode == "lost_reply":
            raise ClientTransportError("fixture removal reply was lost")
        if self.mode == "cancelled":
            raise KeyboardInterrupt
        return DeprovisionManagedContainerResult(
            managed_instance_id=str(kwargs["managed_instance_id"]),
            runtime_revision_id=str(kwargs["expected_runtime_revision_id"]),
            removed_container_id="e" * 64
            if self.mode == "wrong_reply"
            else str(kwargs["expected_container_id"]),
            removed=True,
        )

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult:
        with self.state.env.sessions() as session:
            instance = ManagedInstanceRepository(session).get(UUID(managed_instance_id))
            assert instance is not None
            artifact = RuntimeContainerArtifactRepository(session).get(
                instance.runtime_revision_id
            )
            assert artifact is not None

        def create() -> ProvisionedManagedContainerRecord:
            if self.create_mode == "missing_image":
                raise RuntimeImageUnavailableError("fixture pre-create cache refusal")
            assert not self.state.records, (
                "never create a second stopped or running profile"
            )
            container_id = f"{len(self.creates) + 16:064x}"
            self.state.records[:] = [
                ManagedContainerRecord(
                    managed_instance_id,
                    container_id,
                    artifact.image_id,
                    ContainerRuntimeState.STOPPED,
                    ContainerHealthState.NOT_CONFIGURED,
                    str(instance.runtime_revision_id),
                )
            ]
            self.creates.append(
                (managed_instance_id, str(instance.runtime_revision_id), container_id)
            )
            if self.create_mode == "unknown_create":
                raise RuntimeError("fixture create reply was lost")
            return ProvisionedManagedContainerRecord(
                managed_instance_id,
                str(instance.runtime_revision_id),
                container_id,
                artifact.image_id,
                ContainerRuntimeState.STOPPED,
                True,
            )

        result = self.state.admission.run_provision(
            instance_id=managed_instance_id,
            revision_id=str(instance.runtime_revision_id),
            dispatch=create,
        )
        return ProvisionManagedContainerResult.model_validate(asdict(result))


def build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_path: Path | None = None,
) -> Any:
    env = review_environment(tmp_path, monkeypatch, database_path=database_path)
    broker = Broker(env.state)
    readiness = ReadinessTaskRegistry(max_workers=1)
    status = ManagedRuntimeStatusService(env.state.env.sessions, broker, readiness)
    lifecycle = ManagedRuntimeLifecycleService(
        env.state.env.sessions, cast(Any, broker), readiness, status
    )
    service = SessionReplacementService(env.state.service, lifecycle)
    review = env.reviews.create(env.request())
    operation = SessionReplacementRequest(
        request_id=uuid4(),
        expected_snapshot_sha256=review.snapshot_sha256,
        confirm_profile_disposal=True,
    )
    return SimpleNamespace(
        env=env,
        broker=broker,
        readiness=readiness,
        lifecycle=lifecycle,
        service=service,
        review=review,
        operation=operation,
        sessions=env.state.env.sessions,
    )


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    result = build(tmp_path, monkeypatch)
    yield result
    result.readiness.close()


def discard(case: Any) -> Any:
    return case.env.retained.discard(case.env.capture.capture_id, case.env.discard)


def test_fresh_retires_exact_profile_and_atomically_prepares_successor(
    case: Any,
) -> None:
    before = case.env.state.env.workspaces.get(case.env.state.env.workspace_id)
    result = case.service.execute(case.review.review_id, case.operation)
    assert result.phase == "successor_bound" and result.successor.generation == 2
    assert result.successor.workspace_id == case.review.snapshot.workspace_id
    assert (
        result.successor.container_id
        != case.review.snapshot.request.expected_container_id
    )
    assert (
        len(case.broker.removed)
        == len(case.broker.creates)
        == len(case.env.state.records)
        == 1
    )
    with case.sessions() as session:
        assert session.get(DevelopmentSessionRow, 1).phase == "closed"
        assert session.get(DevelopmentSessionPolicyRow, 1).active_generation == 2
    assert case.env.state.env.workspaces.get(case.env.state.env.workspace_id) == before
    assert case.env.reviews.get(case.review.review_id).state == "completed"
    assert case.env.reviews.active() is None
    assert (
        case.env.retained.read(case.env.capture.capture_id).capture.state == "retained"
    )
    assert (
        discard(case).state == "discarded"
    )  # Completion releases the hold, not the saved body.


def test_status_and_exact_post_replay_never_probe_or_redispatch(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = case.service.execute(case.review.review_id, case.operation)

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("status and replay must not dispatch or observe")

    monkeypatch.setattr(case.env.state.service, "_inventory", forbidden)
    monkeypatch.setattr(case.lifecycle, "deprovision", forbidden)
    monkeypatch.setattr(case.lifecycle, "provision", forbidden)
    assert case.service.get(case.review.review_id) == result
    assert case.service.execute(case.review.review_id, case.operation) == result
    with pytest.raises(SessionReplacementError, match="request_conflict"):
        case.service.execute(
            case.review.review_id,
            case.operation.model_copy(update={"request_id": uuid4()}),
        )


@pytest.mark.parametrize("value", [False, 1, "true"])
def test_final_disposal_confirmation_requires_actual_boolean(
    case: Any, value: object
) -> None:
    with pytest.raises(ValidationError):
        SessionReplacementRequest.model_validate(
            {**case.operation.model_dump(), "confirm_profile_disposal": value}
        )
    assert not case.broker.calls


@pytest.mark.parametrize(
    "mode", ["lost_reply", "wrong_reply", "remaining", "cancelled"]
)
def test_unconfirmed_retirement_never_closes_old_slot_or_releases_evidence(
    case: Any, mode: str
) -> None:
    case.broker.mode = mode
    if mode == "cancelled":
        with pytest.raises(KeyboardInterrupt):
            case.service.execute(case.review.review_id, case.operation)
        result = case.service.get(case.review.review_id)
    else:
        result = case.service.execute(case.review.review_id, case.operation)
    assert result.phase == "retirement_unconfirmed"
    assert case.env.state.service.status().generation == 1 and not case.broker.creates
    with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
        discard(case)
    with pytest.raises(
        SessionReplacementReviewError, match="execution_requires_review"
    ):
        case.env.reviews.cancel(
            case.review.review_id,
            ReplacementReviewCancel(
                expected_snapshot_sha256=case.review.snapshot_sha256,
                confirm_cancel=True,
            ),
        )
    for method in (case.service.continue_retired, case.service.release_retired):
        with pytest.raises(SessionReplacementError, match="retirement_unconfirmed"):
            method(case.review.review_id, case.operation)


def test_concurrent_duplicate_final_requests_dispatch_one_retirement_and_one_create(
    case: Any,
) -> None:
    entered, release = Event(), Event()
    case.broker.block_removal = entered, release
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(case.service.execute, case.review.review_id, case.operation)
        assert entered.wait(10)
        second = pool.submit(
            case.service.execute, case.review.review_id, case.operation
        )
        interim = second.result(10)
        assert interim.phase == "retirement_unconfirmed" and not case.broker.creates
        with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
            discard(case)
        with pytest.raises(
            SessionReplacementReviewError, match="execution_requires_review"
        ):
            case.env.reviews.cancel(
                case.review.review_id,
                ReplacementReviewCancel(
                    expected_snapshot_sha256=case.review.snapshot_sha256,
                    confirm_cancel=True,
                ),
            )
        release.set()
        assert first.result(10).phase == "successor_bound"
    assert len(case.broker.removed) == len(case.broker.creates) == 1


def test_stale_final_review_refuses_before_any_claim_or_disposal(case: Any) -> None:
    with pytest.raises(SessionReplacementError, match="review_changed"):
        case.service.execute(
            case.review.review_id,
            case.operation.model_copy(update={"expected_snapshot_sha256": "f" * 64}),
        )
    assert not case.broker.calls and case.service.get(case.review.review_id) is None


def test_handoff_failure_keeps_exclusion_until_explicit_continuation(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = case.service._handoff

    def refuse(*_args: object) -> int:
        raise SessionReplacementError("fixture next runtime approval changed")

    monkeypatch.setattr(case.service, "_handoff", refuse)
    with pytest.raises(SessionReplacementError):
        case.service.execute(case.review.review_id, case.operation)
    assert case.service.get(case.review.review_id).phase == "retired"
    assert case.env.state.service.status().generation == 1
    with pytest.raises(ManagedCapacityError):
        case.broker.provision_managed_container(
            managed_instance_id=str(fixture.TARGET_ID)
        )
    with pytest.raises(RetainedDiagnosticsError, match="held_for_session_review"):
        discard(case)
    assert (
        case.service.execute(case.review.review_id, case.operation).phase == "retired"
    )
    monkeypatch.setattr(case.service, "_handoff", handoff)
    assert (
        case.service.continue_retired(case.review.review_id, case.operation).phase
        == "successor_bound"
    )
    assert len(case.broker.removed) == 1


def test_explicit_release_of_confirmed_retired_slot_preserves_saved_captures(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def pause(*_args: object) -> int:
        raise SessionReplacementError("fixture paused after retirement")

    monkeypatch.setattr(case.service, "_handoff", pause)
    with pytest.raises(SessionReplacementError):
        case.service.execute(case.review.review_id, case.operation)
    released = case.service.release_retired(case.review.review_id, case.operation)
    assert (
        released.phase == "released_after_retirement"
        and case.env.state.service.status() is None
    )
    assert (
        case.service.release_retired(case.review.review_id, case.operation) == released
    )
    assert (
        case.env.retained.read(case.env.capture.capture_id).capture.state == "retained"
    )
    assert case.env.reviews.get(case.review.review_id).state == "cancelled"
    assert not case.broker.creates


@pytest.mark.parametrize(
    "mode,phase",
    [
        ("missing_image", "successor_reserved"),
        ("unknown_create", "preparation_incomplete"),
    ],
)
def test_prepare_failure_keeps_exact_successor_and_never_retries_create_on_replay(
    case: Any, mode: str, phase: str
) -> None:
    case.broker.create_mode = mode
    result = case.service.execute(case.review.review_id, case.operation)
    assert result.phase == phase and result.successor.generation == 2
    count = len(case.broker.creates)
    assert case.service.execute(case.review.review_id, case.operation) == result
    assert len(case.broker.creates) == count
    if mode == "unknown_create":
        with case.sessions() as session:
            row = session.get(ManagedProvisionReservationRow, str(fixture.TARGET_ID))
            assert row is not None and row.phase == "uncertain"
        assert result.successor.phase == "uncertain"
        with pytest.raises(ManagedCapacityError):
            case.broker.provision_managed_container(
                managed_instance_id=str(fixture.TARGET_ID)
            )


def test_handoff_commit_failure_rolls_back_all_three_policy_changes(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = Session.commit

    def refuse_handoff(session: Session) -> None:
        if any(
            isinstance(row, DevelopmentSessionPolicyRow) and row.active_generation == 2
            for row in session.dirty
        ):
            raise RuntimeError("fixture handoff commit failed")
        commit(session)

    monkeypatch.setattr(Session, "commit", refuse_handoff)
    with pytest.raises(RuntimeError, match="handoff commit failed"):
        case.service.execute(case.review.review_id, case.operation)
    assert case.service.get(case.review.review_id).phase == "retired"
    with case.sessions() as session:
        assert session.get(DevelopmentSessionPolicyRow, 1).active_generation == 1
        assert session.get(DevelopmentSessionRow, 1).phase == "bound"
        assert session.get(DevelopmentSessionRow, 2) is None
    assert not case.broker.creates


def test_cancellation_after_handoff_commit_replays_reserved_successor_without_create(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = Session.commit

    def cancelled_after_handoff(session: Session) -> None:
        handoff = any(
            isinstance(row, DevelopmentSessionPolicyRow) and row.active_generation == 2
            for row in session.dirty
        )
        commit(session)
        if handoff:
            raise KeyboardInterrupt

    monkeypatch.setattr(Session, "commit", cancelled_after_handoff)
    with pytest.raises(KeyboardInterrupt):
        case.service.execute(case.review.review_id, case.operation)
    result = case.service.execute(case.review.review_id, case.operation)
    assert result.phase == "successor_reserved" and result.successor.generation == 2
    assert not case.broker.creates and len(case.broker.removed) == 1
    monkeypatch.setattr(Session, "commit", commit)
    assert (
        case.service.continue_retired(case.review.review_id, case.operation).phase
        == "successor_bound"
    )


def next_version(case: Any, version: str = "20.5") -> Any:
    target = replace(make_target(name="Next cached Kodi"), kodi_version=version)
    definition = RuntimeDefinition(
        id=uuid4(), slug="fixture-next-" + uuid4().hex, kodi_version=version
    )
    with case.sessions.begin() as session:
        old_revision = RuntimeRevisionRepository(session).get(
            fixture.RUNTIME_REVISION_ID
        )
        old_profile = RuntimeExecutionProfileRepository(session).get(
            fixture.RUNTIME_REVISION_ID
        )
        old_instance = ManagedInstanceRepository(session).get(fixture.TARGET_ID)
        assert (
            old_revision is not None
            and old_profile is not None
            and old_instance is not None
        )
        revision = replace(
            old_revision, id=uuid4(), runtime_definition_id=definition.id
        )
        RuntimeDefinitionRepository(session).create(definition)
        RuntimeRevisionRepository(session).create(revision)
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(
                runtime_revision_id=revision.id, image_id="sha256:" + "d" * 64
            )
        )
        RuntimeExecutionProfileRepository(session).create(
            replace(
                old_profile,
                runtime_revision_id=revision.id,
                expected_kodi_version=version,
            )
        )
        TargetRepository(session).create(target)
        ManagedInstanceRepository(session).create(
            replace(old_instance, target_id=target.id, runtime_revision_id=revision.id)
        )
    return target


def select_review(case: Any, target_id: UUID) -> tuple[Any, Any]:
    record = case.env.state.records[0]
    value = snapshot()
    value.exit_metadata = value.exit_metadata.model_copy(
        update=dict(
            managed_instance_id=record.managed_instance_id,
            runtime_revision_id=record.runtime_revision_id,
            container_id=record.container_id,
            image_id=record.image_id,
        )
    )
    case.env.retained._broker.read_stopped_managed_logs = lambda **_kwargs: value
    capture = case.env.retained.capture(
        UUID(record.managed_instance_id),
        DiagnosticCaptureInput(
            request_id=uuid4(),
            expected_runtime_revision_id=UUID(record.runtime_revision_id),
            expected_container_id=record.container_id,
            retain_redacted_logs=True,
        ),
    )
    selection = case.env.reviews.offer(target_id)
    assert capture.payload_sha256 is not None
    review = case.env.reviews.create(
        ReplacementReviewRequest(
            **selection.model_dump(),
            request_id=uuid4(),
            confirm_review=True,
            acknowledge_uncollected_crash_reports=True,
            evidence=[
                ReplacementEvidenceSelection(
                    capture_id=capture.capture_id, payload_sha256=capture.payload_sha256
                )
            ],
        )
    )
    operation = SessionReplacementRequest(
        request_id=uuid4(),
        expected_snapshot_sha256=review.snapshot_sha256,
        confirm_profile_disposal=True,
    )
    return review, operation


def test_sequential_version_switches_keep_workspace_and_never_two_profiles(
    case: Any,
) -> None:
    # Runtime acquisition/eligibility is doubled; exact distinct stored pins,
    # lifecycle claims and shared admission use real temporary SQLite.
    target = next_version(case)
    case.env.reviews.cancel(
        case.review.review_id,
        ReplacementReviewCancel(
            expected_snapshot_sha256=case.review.snapshot_sha256, confirm_cancel=True
        ),
    )
    first_review, first_input = select_review(case, target.id)
    first = case.service.execute(first_review.review_id, first_input)
    assert first.phase == "successor_bound" and first.successor.target_id == target.id
    second_review, second_input = select_review(case, fixture.TARGET_ID)
    second = case.service.execute(second_review.review_id, second_input)
    assert (
        second.phase == "successor_bound"
        and second.successor.target_id == fixture.TARGET_ID
    )
    assert (
        second.successor.workspace_id
        == first.successor.workspace_id
        == case.review.snapshot.workspace_id
    )
    assert first.successor.runtime_revision_id != second.successor.runtime_revision_id
    assert len(case.broker.creates) == len(case.broker.removed) == 2
    assert len(case.env.state.records) == 1
    assert case.service.get(first_review.review_id).successor.phase == "closed"


def test_competing_advanced_target_cannot_occupy_the_handoff_slot(case: Any) -> None:
    unrelated = next_version(case)
    entered, release = Event(), Event()
    case.broker.block_removal = entered, release
    with ThreadPoolExecutor(max_workers=2) as pool:
        replacement = pool.submit(
            case.service.execute, case.review.review_id, case.operation
        )
        assert entered.wait(10)
        with pytest.raises(ManagedCapacityError):
            case.broker.provision_managed_container(
                managed_instance_id=str(unrelated.id)
            )
        release.set()
        assert replacement.result(10).phase == "successor_bound"
    with pytest.raises(ManagedCapacityError):
        case.broker.provision_managed_container(managed_instance_id=str(unrelated.id))
    assert len(case.broker.creates) == 1 and len(case.env.state.records) == 1


def test_revoked_cached_runtime_blocks_before_profile_disposal(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def revoked(*_args: object) -> None:
        raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    monkeypatch.setattr(case.env.state.service, "_runtime", revoked)
    with pytest.raises(DevelopmentSessionConflict, match="runtime_ineligible"):
        case.service.execute(case.review.review_id, case.operation)
    assert not case.broker.calls and len(case.env.state.records) == 1
    assert case.service.get(case.review.review_id) is None


def test_post_retirement_revocation_preserves_slot_until_explicit_release(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def revoked_after_retirement(*_args: object) -> None:
        calls.append(None)
        if len(calls) > 1:
            raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    monkeypatch.setattr(case.env.state.service, "_runtime", revoked_after_retirement)
    with pytest.raises(DevelopmentSessionConflict, match="runtime_ineligible"):
        case.service.execute(case.review.review_id, case.operation)
    assert case.service.get(case.review.review_id).phase == "retired"
    assert case.env.state.service.status().generation == 1
    assert not case.broker.creates and not case.env.state.records
    assert (
        case.service.release_retired(case.review.review_id, case.operation).phase
        == "released_after_retirement"
    )


def test_known_session_history_capacity_refuses_before_disposing_current_profile(
    case: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import session_replacements as module

    monkeypatch.setattr(module, "MAX_GENERATIONS", 1)
    with pytest.raises(SessionReplacementError, match="generation_capacity"):
        case.service.execute(case.review.review_id, case.operation)
    assert not case.broker.calls and len(case.env.state.records) == 1
    assert case.service.get(case.review.review_id) is None


def test_partial_history_restore_cannot_claim_a_completed_atomic_handoff(
    case: Any,
) -> None:
    case.service.execute(case.review.review_id, case.operation)
    with case.sessions.begin() as session:
        old = session.get(DevelopmentSessionRow, 1)
        assert old is not None
        old.phase = "bound"
    with pytest.raises(SessionReplacementError, match="history_invalid"):
        case.service.get(case.review.review_id)


def test_distinct_execution_nonce_cannot_steal_an_inflight_review(case: Any) -> None:
    entered, release = Event(), Event()
    case.broker.block_removal = entered, release
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(case.service.execute, case.review.review_id, case.operation)
        assert entered.wait(10)
        try:
            with pytest.raises(SessionReplacementError, match="request_conflict"):
                case.service.execute(
                    case.review.review_id,
                    case.operation.model_copy(update={"request_id": uuid4()}),
                )
        finally:
            release.set()
        assert first.result(10).phase == "successor_bound"
    assert len(case.broker.removed) == len(case.broker.creates) == 1


def test_execution_nonce_cannot_be_rebound_to_a_later_review(case: Any) -> None:
    first = case.service.execute(case.review.review_id, case.operation)
    review, operation = select_review(case, fixture.TARGET_ID)
    with pytest.raises(SessionReplacementError, match="request_conflict"):
        case.service.execute(
            review.review_id,
            operation.model_copy(update={"request_id": case.operation.request_id}),
        )
    assert case.service.get(review.review_id) is None
    assert case.env.state.service.status() == first.successor
    assert len(case.broker.removed) == len(case.broker.creates) == 1

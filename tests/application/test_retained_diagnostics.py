"""Durable selected evidence survives disposal without claiming mutation authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from kodi_lab.application.retained_diagnostics import (
    DiagnosticCaptureInput,
    DiagnosticDiscardInput,
    RetainedDiagnosticsError,
    RetainedDiagnosticsService,
)
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.broker.stopped_diagnostics import StoppedRuntimeDiagnostics
from kodi_lab.broker.stopped_logs import (
    ALL_ARTIFACTS,
    StoppedLogArtifact,
    StoppedLogSnapshot,
)
from kodi_lab.storage.models import (
    ManagedDiagnosticCaptureRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from tests.broker.test_provisioning_resolver import (
    IMAGE_ID,
    INSTANCE_ID,
    REVISION_ID,
    resolver_with_state,
)

CONTAINER_ID = "c" * 64
NOW = datetime(2026, 10, 25, tzinfo=UTC)
NOW_TEXT = "2026-10-25T00:00:00.000000Z"


def request(**changes: Any) -> DiagnosticCaptureInput:
    return DiagnosticCaptureInput.model_validate(
        dict(
            request_id=uuid4(),
            expected_runtime_revision_id=REVISION_ID,
            expected_container_id=CONTAINER_ID,
            retain_redacted_logs=True,
            **changes,
        )
    )


def snapshot(*, legacy: bool = False) -> StoppedLogSnapshot:
    return StoppedLogSnapshot(
        schema_revision=1 if legacy else 2,
        scope="fixed_internal_log_tails"
        if legacy
        else "fixed_internal_and_docker_log_tails",
        docker_output="not_collected" if legacy else "bounded_window",
        exit_metadata=StoppedRuntimeDiagnostics(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=CONTAINER_ID,
            image_id=IMAGE_ID,
            container_status="exited",
            exit_code=139,
            oom_killed=False,
            started_at=None,
            finished_at=None,
            observed_at=NOW,
        ),
        artifacts=[
            StoppedLogArtifact.model_validate(
                dict(
                    kind=kind,
                    state="available",
                    source_size_bytes=32,
                    content="TAIL-evidence-line [REDACTED]\n",
                    source_sha256="a" * 64,
                )
            )
            for kind in (ALL_ARTIFACTS[:4] if legacy else ALL_ARTIFACTS)
        ],
        observed_at=NOW,
    )


class Broker:
    reads = 0
    inventories = 0
    present = True
    state = ContainerRuntimeState.STOPPED
    hook: Any = None
    failure: BaseException | None = None
    value: StoppedLogSnapshot

    def __init__(self) -> None:
        self.value = snapshot()

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        assert managed_instance_id == str(INSTANCE_ID)
        self.inventories += 1
        return ManagedContainerInventoryResult(
            containers=[]
            if not self.present
            else [
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    runtime_revision_id=str(REVISION_ID),
                    image_id=IMAGE_ID,
                    runtime_state=self.state,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            ]
        )

    def read_stopped_managed_logs(self, **kwargs: Any) -> StoppedLogSnapshot:
        assert kwargs == {
            "managed_instance_id": str(INSTANCE_ID),
            "expected_runtime_revision_id": str(REVISION_ID),
            "expected_container_id": CONTAINER_ID,
        }
        self.reads += 1
        if self.hook:
            self.hook()
        if self.failure:
            raise self.failure
        return self.value


@dataclass
class Environment:
    service: RetainedDiagnosticsService
    broker: Broker
    sessions: Any


def environment(tmp_path: Path) -> Environment:
    resolver = resolver_with_state(tmp_path, runtime_uid=1000, runtime_gid=1000)
    sessions = resolver._session_factory
    broker = Broker()
    return Environment(
        RetainedDiagnosticsService(
            sessions, broker, data_root=tmp_path, clock=lambda: NOW
        ),
        broker,
        sessions,
    )


def operation(env: Environment, **changes: Any) -> UUID:
    identity = uuid4()
    values: dict[str, Any] = dict(
        id=str(identity),
        client_id=str(uuid4()),
        request_id=str(uuid4()),
        workspace_id=str(uuid4()),
        development_source_id=str(uuid4()),
        target_id=str(INSTANCE_ID),
        runtime_revision_id=str(REVISION_ID),
        target_class="internal_lab",
        deployment_mode="direct",
        generation=7,
        addon_id="plugin.video.test",
        addon_version="1.0.0",
        state="failed",
        phase="inventory",
        mutation_started=True,
        target_stopped=True,
        sync_started=True,
        sync_completed=True,
        target_started=True,
        inventory_classification="failed",
        failure_code="fixture_inventory_failure",
        original_container_id=CONTAINER_ID,
        requested_at=NOW_TEXT,
        started_at=NOW_TEXT,
        completed_at=NOW_TEXT,
    )
    values.update(changes)
    with env.sessions.begin() as session:
        session.add(McpWorkspaceDeploymentOperationRow(**values))
    return identity


def test_capture_replays_after_restart_and_target_removal_without_another_read(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    req = request()
    captured = env.service.capture(INSTANCE_ID, req)
    assert captured.state == "retained" and env.broker.reads == 1
    assert (
        not captured.authorizes_profile_disposal and captured.operation_context is None
    )
    assert "TAIL-evidence-line" not in str(env.service.list())
    with env.sessions.begin() as session:
        session.execute(delete(TargetRow).where(TargetRow.id == str(INSTANCE_ID)))
    env.broker.present = False
    restarted = RetainedDiagnosticsService(
        env.sessions,
        env.broker,
        data_root=tmp_path,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    assert (
        restarted.read(captured.capture_id)
        .logs.artifacts[0]
        .content.startswith("TAIL-evidence-line")
    )
    assert restarted.capture(INSTANCE_ID, req).capture_id == captured.capture_id
    assert env.broker.reads == 1
    assert restarted.list(INSTANCE_ID)[0].review_due is False


def test_only_explicit_exact_terminal_operation_is_associated(tmp_path: Path) -> None:
    env = environment(tmp_path)
    op = operation(env)
    result = env.service.capture(INSTANCE_ID, request(deployment_operation_id=op))
    assert result.operation_context is not None
    assert (
        result.operation_context.operation_id == op
        and result.operation_context.generation == 7
    )
    assert result.operation_context.relationship == "same_container_after_deployment"
    assert result.operation_context.state == "failed"
    assert not result.authorizes_profile_disposal


@pytest.mark.parametrize(
    "change",
    [
        {"original_container_id": "d" * 64},
        {"target_id": str(uuid4())},
        {"runtime_revision_id": str(uuid4())},
        {"state": "running", "completed_at": None},
        {"original_container_id": None},
    ],
)
def test_mismatched_or_inflight_operation_cannot_claim_log_attribution(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    env = environment(tmp_path)
    op = operation(env, **change)
    with pytest.raises(RetainedDiagnosticsError, match="operation_binding_unavailable"):
        env.service.capture(INSTANCE_ID, request(deployment_operation_id=op))
    assert env.broker.reads == 0 and env.service.list() == []


def test_replay_nonce_cannot_be_rebound_to_another_container_or_operation(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    req = request()
    env.service.capture(INSTANCE_ID, req)
    for changed in (
        {"expected_container_id": "f" * 64},
        {"deployment_operation_id": uuid4()},
        {"expected_runtime_revision_id": uuid4()},
    ):
        with pytest.raises(RetainedDiagnosticsError, match="request_conflict"):
            env.service.capture(INSTANCE_ID, req.model_copy(update=changed))
    assert env.broker.reads == 1


@pytest.mark.parametrize(
    "failure", [RuntimeError("PRIVATE error must not escape"), KeyboardInterrupt()]
)
def test_interrupted_read_retains_failure_and_never_replays_mutation(
    tmp_path: Path, failure: BaseException
) -> None:
    env = environment(tmp_path)
    req = request()
    env.broker.failure = failure
    with pytest.raises(
        KeyboardInterrupt
        if isinstance(failure, KeyboardInterrupt)
        else RetainedDiagnosticsError
    ):
        env.service.capture(INSTANCE_ID, req)
    result = env.service.capture(INSTANCE_ID, req)
    assert (
        result.state == "failed"
        and result.failure_code == "diagnostic_capture_unavailable"
    )
    assert env.broker.reads == 1
    with env.sessions() as session:
        row = session.get(ManagedDiagnosticCaptureRow, str(result.capture_id))
        assert row is not None and row.payload_json is None
    assert "PRIVATE" not in str(result)


def test_cancellation_after_committed_publication_preserves_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment(tmp_path)
    original = env.service._publish

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(env.service, "_publish", interrupted)
    req = request()
    with pytest.raises(KeyboardInterrupt):
        env.service.capture(INSTANCE_ID, req)
    result = env.service.capture(INSTANCE_ID, req)
    assert result.state == "retained"
    assert env.service.read(result.capture_id).logs.artifacts[0].content
    assert env.broker.reads == 1


def test_wrong_result_identity_or_current_drift_fails_without_retaining_body(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    env.broker.value.exit_metadata.container_id = "d" * 64
    with pytest.raises(RetainedDiagnosticsError, match="binding_changed"):
        env.service.capture(INSTANCE_ID, request())
    assert env.service.list()[0].state == "failed"
    env.broker.value = snapshot()
    env.broker.hook = lambda: setattr(env.broker, "present", False)
    with pytest.raises(RetainedDiagnosticsError, match="binding_changed"):
        env.service.capture(INSTANCE_ID, request())
    assert all(capture.state == "failed" for capture in env.service.list())


def test_discard_requires_the_exact_reviewed_state_and_digest_and_keeps_history(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    result = env.service.capture(INSTANCE_ID, request())
    wrong = DiagnosticDiscardInput(
        expected_state="retained",
        expected_payload_sha256="0" * 64,
        confirm_discard=True,
    )
    with pytest.raises(RetainedDiagnosticsError, match="state_changed"):
        env.service.discard(result.capture_id, wrong)
    correct = wrong.model_copy(
        update={"expected_payload_sha256": result.payload_sha256}
    )
    discarded = env.service.discard(result.capture_id, correct)
    assert (
        discarded.state == "discarded"
        and discarded.payload_sha256 == result.payload_sha256
    )
    assert env.service.discard(result.capture_id, correct).state == "discarded"
    with pytest.raises(RetainedDiagnosticsError, match="body_unavailable"):
        env.service.read(result.capture_id)
    assert len(env.service.list()) == 1 and env.broker.reads == 1


def test_abandoned_read_cannot_publish_after_a_late_completion(tmp_path: Path) -> None:
    env = environment(tmp_path)

    def abandon() -> None:
        current = env.service.list()[0]
        env.service.discard(
            current.capture_id,
            DiagnosticDiscardInput(expected_state="capturing", confirm_discard=True),
        )

    env.broker.hook = abandon
    with pytest.raises(RetainedDiagnosticsError, match="state_changed"):
        env.service.capture(INSTANCE_ID, request())
    assert env.service.list()[0].state == "discarded"


def test_concurrent_request_reserves_one_capture_and_does_not_repeat_reads(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    entered, release = Event(), Event()

    def pause() -> None:
        entered.set()
        assert release.wait(5)

    env.broker.hook = pause
    req = request()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(env.service.capture, INSTANCE_ID, req)
        try:
            assert entered.wait(5)
            assert env.service.capture(INSTANCE_ID, req).state == "capturing"
            with pytest.raises(RetainedDiagnosticsError, match="capture_busy"):
                env.service.capture(INSTANCE_ID, request())
            with pytest.raises(RetainedDiagnosticsError, match="capture_busy"):
                env.service.capture(uuid4(), request())
        finally:
            release.set()
        assert future.result().state == "retained"
    assert env.broker.reads == 1


def test_capacity_and_review_age_never_automatically_remove_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kodi_lab.application.retained_diagnostics as module

    env = environment(tmp_path)
    monkeypatch.setattr(module, "MAX_BODIES", 1)
    monkeypatch.setattr(module, "MAX_HISTORY", 2)
    req = request()
    first = env.service.capture(INSTANCE_ID, req)
    monkeypatch.setattr(env.service, "_clock", lambda: NOW + timedelta(days=31))
    assert env.service.status(first.capture_id).review_due
    assert env.service.read(first.capture_id).logs.artifacts[0].content
    with pytest.raises(RetainedDiagnosticsError, match="body_capacity"):
        env.service.capture(INSTANCE_ID, request())
    env.service.discard(
        first.capture_id,
        DiagnosticDiscardInput(
            expected_state="retained",
            expected_payload_sha256=first.payload_sha256,
            confirm_discard=True,
        ),
    )
    env.service.capture(INSTANCE_ID, request())
    with pytest.raises(RetainedDiagnosticsError, match="history_capacity"):
        env.service.capture(INSTANCE_ID, request())
    assert len(env.service.list()) == 2 and env.broker.reads == 2


def test_tampered_retained_body_is_rejected_without_any_runtime_call(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    result = env.service.capture(INSTANCE_ID, request())
    with env.sessions.begin() as session:
        row = session.get(ManagedDiagnosticCaptureRow, str(result.capture_id))
        assert row is not None and row.payload_json is not None
        row.payload_json = row.payload_json.replace(
            "TAIL-evidence-line", "TAMPERED-evidence"
        )
    with pytest.raises(RetainedDiagnosticsError, match="evidence_invalid"):
        env.service.read(result.capture_id)
    assert env.broker.reads == 1


def test_storage_reserve_fails_before_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import namedtuple

    env = environment(tmp_path)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "kodi_lab.application.retained_diagnostics.shutil.disk_usage",
        lambda _: usage(10, 9, 1),
    )
    with pytest.raises(RetainedDiagnosticsError, match="storage_reserve"):
        env.service.capture(INSTANCE_ID, request())
    assert env.broker.reads == 0 and env.service.list() == []


@pytest.mark.parametrize(
    "defect", ["persistent", "production", "wrong_runtime", "absent", "running"]
)
def test_non_disposable_or_changed_scope_never_reaches_file_reads(
    tmp_path: Path, defect: str
) -> None:
    from kodi_lab.domain import InstanceType, TargetType

    env = environment(tmp_path)
    with env.sessions.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        target = session.get(TargetRow, str(INSTANCE_ID))
        assert instance is not None and target is not None
        if defect == "persistent":
            instance.instance_type = InstanceType.PERSISTENT
        elif defect == "production":
            target.target_type = TargetType.EXTERNAL_PRODUCTION
    if defect == "absent":
        env.broker.present = False
    elif defect == "running":
        env.broker.state = ContainerRuntimeState.RUNNING
    req = request()
    if defect == "wrong_runtime":
        req = req.model_copy(update={"expected_runtime_revision_id": uuid4()})
    with pytest.raises(RetainedDiagnosticsError, match="binding_changed"):
        env.service.capture(INSTANCE_ID, req)
    assert env.broker.reads == 0 and not env.service.list()


def test_retained_reconciliation_fence_is_not_cleared_by_capture(
    tmp_path: Path,
) -> None:
    from kodi_lab.domain import ManagedInstanceMutationKind
    from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

    env = environment(tmp_path)
    op = operation(env, state="reconciliation_required")
    with env.sessions.begin() as session:
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(INSTANCE_ID),
                operation_kind=ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT,
                operation_id=str(op),
                lease_token_digest=None,
            )
        )
    result = env.service.capture(INSTANCE_ID, request(deployment_operation_id=op))
    assert result.state == "retained" and not result.authorizes_profile_disposal
    with env.sessions() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
        row = session.get(McpWorkspaceDeploymentOperationRow, str(op))
        assert lease is not None and lease.operation_id == str(op)
        assert row is not None and row.state == "reconciliation_required"


def test_unbound_capture_does_not_guess_a_nearby_operation(tmp_path: Path) -> None:
    env = environment(tmp_path)
    operation(env)
    result = env.service.capture(INSTANCE_ID, request())
    assert result.operation_context is None


def test_schema_one_retained_captures_remain_readable_beside_new_docker_windows(
    tmp_path: Path,
) -> None:
    env = environment(tmp_path)
    env.broker.value = snapshot(legacy=True)
    first = env.service.capture(INSTANCE_ID, request())
    env.broker.value = snapshot()
    second = env.service.capture(INSTANCE_ID, request())
    old = env.service.read(first.capture_id)
    new = env.service.read(second.capture_id)
    assert old.logs.schema_revision == 1 and len(old.logs.artifacts) == 4
    assert old.logs.docker_output == "not_collected"
    assert new.logs.schema_revision == 2 and len(new.logs.artifacts) == 5
    assert new.logs.artifacts[-1].kind == "docker_output"
    assert old.capture.payload_sha256 == first.payload_sha256


def test_reserve_checks_database_filesystem_when_data_root_is_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import namedtuple

    env = environment(tmp_path)
    elsewhere = tmp_path / "other-files"
    elsewhere.mkdir()
    monkeypatch.setattr(env.service, "_data_root", elsewhere)
    usage = namedtuple("usage", "total used free")

    def disk_usage(path: Path) -> Any:
        return usage(10**10, 0, 10**10) if path == elsewhere else usage(10, 9, 1)

    monkeypatch.setattr(
        "kodi_lab.application.retained_diagnostics.shutil.disk_usage", disk_usage
    )
    with pytest.raises(RetainedDiagnosticsError, match="storage_reserve"):
        env.service.capture(INSTANCE_ID, request())
    assert env.broker.reads == 0 and env.service.list() == []


@pytest.mark.parametrize(
    "code,expected",
    [
        (
            "managed_diagnostics_log_layout_unsupported",
            "managed_diagnostics_log_layout_unsupported",
        ),
        ("managed_diagnostics_deadline", "managed_diagnostics_deadline"),
        ("unreviewed_backend_reason", "diagnostic_capture_unavailable"),
    ],
)
def test_only_allowlisted_broker_failure_codes_are_retained_without_messages(
    tmp_path: Path, code: str, expected: str
) -> None:
    from kodi_lab.broker.errors import RemoteBrokerError

    env = environment(tmp_path)
    env.broker.failure = RemoteBrokerError(code, "PRIVATE raw message", request_id=None)
    with pytest.raises(RetainedDiagnosticsError, match=expected):
        env.service.capture(INSTANCE_ID, request())
    result = env.service.list()[0]
    assert result.failure_code == expected and "PRIVATE" not in str(result)


@pytest.mark.parametrize("value", [1, "true", False])
def test_retention_and_discard_require_literal_boolean_confirmation(
    value: object,
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DiagnosticCaptureInput.model_validate(
            {
                "request_id": uuid4(),
                "expected_runtime_revision_id": REVISION_ID,
                "expected_container_id": CONTAINER_ID,
                "retain_redacted_logs": value,
            }
        )
    with pytest.raises(ValidationError):
        DiagnosticDiscardInput.model_validate(
            {"expected_state": "capturing", "confirm_discard": value}
        )

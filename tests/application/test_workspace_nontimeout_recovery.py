"""Non-timeout recovery requires independent post-execution containment evidence."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.workspace_execution_recovery import (
    WorkspaceExecutionRecoveryError,
    WorkspaceExecutionRecoveryOutcome,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import InstanceRuntimeState
from kodi_lab.kodi.readiness import UsabilityState
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpClientTargetGrantRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    WorkspaceExecutionRecoveryOperationRow,
)
from tests.application import test_mcp_workspace_development as fixture


def _stopped_ambiguity(
    tmp_path: Path,
) -> tuple[fixture.Environment, UUID, UUID]:
    env, request_id, execution_id = fixture._ambiguous_recovery_environment(
        tmp_path, non_timeout=True
    )
    env.broker.state = ContainerRuntimeState.STOPPED
    env.development._recovery._clock = lambda: fixture.NOW + timedelta(seconds=3)
    with env.sessions.begin() as session:
        instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
        assert instance is not None
        instance.runtime_state = InstanceRuntimeState.STOPPED
        instance.runtime_state_revision += 1
        session.add(
            ManagedInstanceLifecycleOperationRow(
                id=str(uuid4()),
                original_managed_instance_id=str(fixture.TARGET_ID),
                managed_instance_id=str(fixture.TARGET_ID),
                instance_name="Disposable recovery",
                kind="stop",
                state="succeeded",
                prior_state="running_usable",
                resulting_state="stopped",
                requested_at="2026-09-20T18:00:01.000000Z",
                started_at="2026-09-20T18:00:01.000000Z",
                completed_at="2026-09-20T18:00:02.000000Z",
                reconciliation_required=False,
            )
        )
    return env, request_id, execution_id


def test_completed_non_timeout_recovery_preserves_execution_and_stop_history(
    tmp_path: Path,
) -> None:
    env, _request_id, execution_id = _stopped_ambiguity(tmp_path)
    with env.sessions() as session:
        before = dict(
            session.execute(select(McpWorkspaceExecutionOperationRow.__table__))
            .mappings()
            .one()
        )
        stop_before = dict(
            session.execute(select(ManagedInstanceLifecycleOperationRow.__table__))
            .mappings()
            .one()
        )
    assert before["state"] == before["execution_classification"] == "ambiguous"
    request_id = uuid4()
    result = env.development.recover_execution(
        client_id=env.client_id,
        request_id=request_id,
        execution_operation_id=execution_id,
    )
    assert result.outcome is WorkspaceExecutionRecoveryOutcome.RECOVERED
    assert (
        not ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )
    assert env.broker.last_stop_kwargs is not None
    assert env.broker.last_stop_kwargs["expected_runtime_state"] is (
        ContainerRuntimeState.STOPPED
    )
    assert env.smoke.count == 1
    stops, starts = env.broker.stop_count, env.broker.start_count
    assert (
        env.development.recover_execution(
            client_id=env.client_id,
            request_id=request_id,
            execution_operation_id=execution_id,
        )
        == result
    )
    with pytest.raises(WorkspaceExecutionRecoveryError, match="recovery_not_allowed"):
        env.development.recover_execution(
            client_id=env.client_id,
            request_id=uuid4(),
            execution_operation_id=execution_id,
        )
    assert (env.broker.stop_count, env.broker.start_count) == (stops, starts)
    with env.sessions() as session:
        after = dict(
            session.execute(select(McpWorkspaceExecutionOperationRow.__table__))
            .mappings()
            .one()
        )
        stop_after = dict(
            session.execute(select(ManagedInstanceLifecycleOperationRow.__table__))
            .mappings()
            .one()
        )
        recovery = session.get(
            WorkspaceExecutionRecoveryOperationRow, str(result.recovery_operation_id)
        )
        assert recovery is not None
        assert recovery.execution_operation_id == str(execution_id)
        assert recovery.containment_confirmed
        assert recovery.readiness_confirmed
        assert recovery.provenance_confirmed
        assert after == before
        assert stop_after == stop_before


@pytest.mark.parametrize(
    "variant",
    [
        "missing_stop",
        "old_stop",
        "future_stop",
        "result_present",
        "unfinished_phase",
        "instance_running",
        "broker_running",
        "wrong_container",
        "wrong_runtime",
        "stale_generation",
        "wrong_client",
        "missing_grant",
        "active_lease",
        "newer_start",
        "stop_not_associated",
        "stale_revision",
        "missing_runtime_identity",
    ],
)
def test_non_timeout_evidence_failures_do_not_dispatch_recovery(
    tmp_path: Path, variant: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _request_id, execution_id = _stopped_ambiguity(tmp_path)
    caller = env.client_id
    with env.sessions.begin() as session:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
        stop = session.scalar(select(ManagedInstanceLifecycleOperationRow))
        assert execution is not None and instance is not None and stop is not None
        if variant == "missing_stop":
            session.delete(stop)
        elif variant == "old_stop":
            stop.requested_at = stop.started_at = stop.completed_at = fixture.NOW_TEXT
        elif variant == "future_stop":
            stop.completed_at = "2026-09-20T18:01:00.000000Z"
        elif variant == "result_present":
            execution.item_count = 1
        elif variant == "unfinished_phase":
            execution.phase = "invoke"
        elif variant == "instance_running":
            instance.runtime_state = InstanceRuntimeState.RUNNING
        elif variant == "broker_running":
            env.broker.state = ContainerRuntimeState.RUNNING
        elif variant == "wrong_container":
            original = env.broker.managed_container_inventory().containers[0]
            monkeypatch.setattr(
                env.broker,
                "managed_container_inventory",
                lambda: SimpleNamespace(
                    containers=[original.model_copy(update={"container_id": "f" * 64})]
                ),
            )
        elif variant == "wrong_runtime":
            deployment = session.get(
                McpWorkspaceDeploymentOperationRow, execution.deployment_id
            )
            assert deployment is not None
            deployment.runtime_revision_id = str(uuid4())
        elif variant == "stale_generation":
            workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
            assert workspace is not None
            workspace.generation += 1
        elif variant == "wrong_client":
            caller = uuid4()
        elif variant == "missing_grant":
            grant = session.get(
                McpClientTargetGrantRow,
                {"client_id": str(caller), "target_id": str(fixture.TARGET_ID)},
            )
            assert grant is not None
            session.delete(grant)
        elif variant == "active_lease":
            session.add(
                ManagedInstanceMutationLeaseRow(
                    managed_instance_id=str(fixture.TARGET_ID),
                    operation_kind="lifecycle",
                    operation_id=str(uuid4()),
                )
            )
        elif variant == "newer_start":
            stop.kind = "start"  # type: ignore[assignment]
        elif variant == "stop_not_associated":
            stop.managed_instance_id = None
        elif variant == "stale_revision":
            instance.runtime_state_revision = execution.original_runtime_state_revision  # type: ignore[assignment]
        elif variant == "missing_runtime_identity":
            execution.original_container_id = None
            execution.original_runtime_state_revision = None
    stops, starts = env.broker.stop_count, env.broker.start_count
    with pytest.raises(WorkspaceExecutionRecoveryError, match="recovery_not_allowed"):
        env.development.recover_execution(
            client_id=caller,
            request_id=uuid4(),
            execution_operation_id=execution_id,
        )
    assert (env.broker.stop_count, env.broker.start_count) == (stops, starts)
    with env.sessions() as session:
        assert session.scalar(select(WorkspaceExecutionRecoveryOperationRow)) is None
    assert (
        ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )


@pytest.mark.parametrize("timestamp", [None, "invalid"])
def test_incomplete_or_malformed_execution_evidence_is_rejected_by_storage(
    tmp_path: Path, timestamp: str | None
) -> None:
    env, _request_id, execution_id = _stopped_ambiguity(tmp_path)
    with pytest.raises(IntegrityError), env.sessions.begin() as session:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        assert execution is not None
        execution.completed_at = timestamp
    with env.sessions() as session:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        assert execution is not None and execution.completed_at is not None
        assert execution.completed_at != "invalid"
        assert session.scalar(select(WorkspaceExecutionRecoveryOperationRow)) is None
    assert (
        ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )


@pytest.mark.parametrize("failure", ["readiness", "provenance"])
def test_non_timeout_failed_verification_cannot_clear_reconciliation(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _request_id, execution_id = _stopped_ambiguity(tmp_path)
    if failure == "readiness":
        env.development._recovery._readiness_factory = lambda _broker, _profile: (
            SimpleNamespace(
                wait_until_usable=lambda _target: SimpleNamespace(
                    usability_state=UsabilityState.NOT_USABLE,
                )
            )
        )
    else:

        def fail_verify(**_kwargs: object) -> object:
            raise RuntimeError("Provenance unavailable")

        monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", fail_verify)
    request_id = uuid4()
    result = env.development.recover_execution(
        client_id=env.client_id,
        request_id=request_id,
        execution_operation_id=execution_id,
    )
    assert result.outcome is WorkspaceExecutionRecoveryOutcome.CONTAINED_BUT_NOT_READY
    assert (
        ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )
    stops, starts = env.broker.stop_count, env.broker.start_count
    assert (
        env.development.recover_execution(
            client_id=env.client_id,
            request_id=request_id,
            execution_operation_id=execution_id,
        )
        == result
    )
    assert (env.broker.stop_count, env.broker.start_count) == (stops, starts)
    assert env.smoke.count == 1

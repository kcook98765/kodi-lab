"""Historical ambiguity stays immutable while later stopped proof is recorded."""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.historical_workspace_execution_containment import (
    HistoricalContainmentError,
    HistoricalContainmentResult,
    HistoricalWorkspaceExecutionContainmentService,
)
from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import InstanceRuntimeState, InstanceType, TargetType
from kodi_lab.storage.models import (
    HistoricalWorkspaceExecutionContainmentResolutionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceExecutionOperationRow,
    RuntimeContainerArtifactRow,
    TargetRow,
    WorkspaceExecutionRecoveryOperationRow,
)
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application.test_mcp_workspace_development import (
    IMAGE_ID,
    NOW,
    RUNTIME_REVISION_ID,
    TARGET_ID,
    Environment,
    SmokeCounter,
    _ambiguous_recovery_environment,
    _restarted_development,
)


def _historical(
    tmp_path: Path,
) -> tuple[
    Environment,
    HistoricalWorkspaceExecutionContainmentService,
    UUID,
    UUID,
    UUID,
    int,
]:
    env, execution_request, execution_id = _ambiguous_recovery_environment(tmp_path)
    later = (
        (NOW + timedelta(minutes=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    stop_id = uuid4()
    with env.sessions.begin() as session:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        assert execution is not None
        execution.execution_kind = None
        execution.original_container_id = None
        execution.original_runtime_state_revision = None
        instance = session.get(ManagedInstanceRow, str(TARGET_ID))
        assert instance is not None
        instance.runtime_state = InstanceRuntimeState.STOPPED
        instance.runtime_state_revision += 1
        revision = instance.runtime_state_revision
        session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(RUNTIME_REVISION_ID), image_id=IMAGE_ID
            )
        )
        session.add(
            ManagedInstanceLifecycleOperationRow(
                id=str(stop_id),
                original_managed_instance_id=str(TARGET_ID),
                managed_instance_id=str(TARGET_ID),
                instance_name="Slice 4 disposable",
                kind="stop",
                requested_at=later,
                started_at=later,
                completed_at=later,
                prior_state="running_usable",
                resulting_state="stopped",
                state="succeeded",
                failure_reason=None,
                reconciliation_required=False,
            )
        )
    env.broker.state = ContainerRuntimeState.STOPPED
    service = HistoricalWorkspaceExecutionContainmentService(env.sessions, env.broker)
    return env, service, execution_request, execution_id, stop_id, revision


def _resolve(
    service: HistoricalWorkspaceExecutionContainmentService,
    execution_id: UUID,
    revision: int,
    request_id: UUID | None = None,
) -> HistoricalContainmentResult:
    return service.resolve(
        request_id=request_id or uuid4(),
        execution_operation_id=execution_id,
        managed_instance_id=TARGET_ID,
        expected_runtime_state_revision=revision,
    )


def test_historical_containment_only_clears_exact_blocker_and_preserves_replay(
    tmp_path: Path,
) -> None:
    env, service, execution_request, execution_id, stop_id, revision = _historical(
        tmp_path
    )
    reconciliation = ManagedInstanceReconciliationService(env.sessions)
    assert reconciliation.status(TARGET_ID).blocking
    request_id = uuid4()
    result = _resolve(service, execution_id, revision, request_id)
    assert result.stop_operation_id == stop_id
    assert result.original_execution_state == "ambiguous"
    original_inventory = env.broker.managed_container_inventory

    def unexpected_inventory() -> ManagedContainerInventoryResult:
        raise AssertionError("resolution replay observed the broker")

    env.broker.managed_container_inventory = unexpected_inventory  # type: ignore[method-assign]
    assert _resolve(service, execution_id, revision, request_id) == result
    assert _resolve(service, execution_id, revision, uuid4()) == result
    assert not reconciliation.status(TARGET_ID).blocking
    reconciliation.require_allowed(TARGET_ID, ReconciliationAction.START)
    with env.sessions() as session:
        original = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        assert original is not None
        assert original.state == "ambiguous"
        assert original.execution_kind is None
        assert original.original_container_id is None
        count = session.scalar(
            select(func.count(HistoricalWorkspaceExecutionContainmentResolutionRow.id))
        )
        assert count == 1
    with env.sessions.begin() as session:
        ManagedInstanceLifecycleOperationRepository(session).prune_terminal(
            TARGET_ID, retain=0
        )
        assert session.get(ManagedInstanceLifecycleOperationRow, str(stop_id))
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=execution_request,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.smoke.count == 1
    env.broker.managed_container_inventory = original_inventory  # type: ignore[method-assign]
    with pytest.raises(IntegrityError), env.sessions.begin() as session:
        row = session.get(
            HistoricalWorkspaceExecutionContainmentResolutionRow,
            str(result.resolution_id),
        )
        assert row is not None
        session.delete(row)
    assert not reconciliation.status(TARGET_ID).blocking
    with env.sessions.begin() as session:
        instance = session.get(ManagedInstanceRow, str(TARGET_ID))
        assert instance is not None
        instance.runtime_state = InstanceRuntimeState.RUNNING
        instance.runtime_state_revision += 1
    env.broker.state = ContainerRuntimeState.RUNNING
    env.smoke.invoke = SmokeCounter.invoke.__get__(env.smoke, SmokeCounter)  # type: ignore[method-assign]
    resumed = _restarted_development(env, tmp_path)
    new_execution = resumed.execute(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    assert new_execution.state == "succeeded"


@pytest.mark.parametrize(
    "change",
    [
        "external",
        "persistent",
        "projected_running",
        "broker_running",
        "artifact_mismatch",
        "artifact_missing",
        "lease",
        "wrong_target",
        "not_ambiguous",
        "undispatched",
        "not_timeout",
        "new_smoke_missing_identity",
        "recovered",
        "stale_revision",
        "no_stop",
        "wrong_stop_instance",
        "wrong_deployment",
        "other_blocker",
    ],
)
def test_historical_containment_rejects_ineligible_without_changing_reconciliation(
    tmp_path: Path, change: str
) -> None:
    env, service, _request, execution_id, stop_id, revision = _historical(tmp_path)
    with env.sessions.begin() as session:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        instance = session.get(ManagedInstanceRow, str(TARGET_ID))
        target = session.get(TargetRow, str(TARGET_ID))
        stop = session.get(ManagedInstanceLifecycleOperationRow, str(stop_id))
        assert execution and instance and target and stop
        if change == "external":
            target.target_type = TargetType.EXTERNAL_DEVELOPMENT
        elif change == "persistent":
            instance.instance_type = InstanceType.PERSISTENT
        elif change == "projected_running":
            instance.runtime_state = InstanceRuntimeState.RUNNING
        elif change == "artifact_mismatch":
            artifact = session.get(
                RuntimeContainerArtifactRow, str(RUNTIME_REVISION_ID)
            )
            assert artifact is not None
            artifact.image_id = "sha256:" + "d" * 64
        elif change == "artifact_missing":
            artifact = session.get(
                RuntimeContainerArtifactRow, str(RUNTIME_REVISION_ID)
            )
            assert artifact is not None
            session.delete(artifact)
        elif change == "lease":
            session.add(
                ManagedInstanceMutationLeaseRow(
                    managed_instance_id=str(TARGET_ID),
                    operation_kind="lifecycle",
                    operation_id=str(uuid4()),
                )
            )
        elif change == "wrong_target":
            execution.target_id = str(uuid4())
        elif change == "not_ambiguous":
            execution.state = "failed"
        elif change == "undispatched":
            execution.invocation_dispatched = False
        elif change == "not_timeout":
            execution.execution_classification = "ambiguous"
        elif change == "new_smoke_missing_identity":
            execution.execution_kind = "smoke"
        elif change == "recovered":
            session.add(
                WorkspaceExecutionRecoveryOperationRow(
                    id=str(uuid4()),
                    client_id=str(env.client_id),
                    request_id=str(uuid4()),
                    execution_operation_id=str(execution_id),
                    state="completed",
                    phase="finalized",
                    outcome="recovered",
                    owned_runtime_state_revision=revision,
                    containment_confirmed=True,
                    readiness_confirmed=True,
                    provenance_confirmed=True,
                    requested_at=stop.requested_at,
                    completed_at=stop.completed_at,
                )
            )
        elif change == "no_stop":
            session.delete(stop)
        elif change == "wrong_stop_instance":
            stop.original_managed_instance_id = str(uuid4())
        elif change == "wrong_deployment":
            execution.deployment_id = str(uuid4())
        elif change == "other_blocker":
            values = {
                column.name: getattr(execution, column.name)
                for column in McpWorkspaceExecutionOperationRow.__table__.columns
            }
            values["id"] = str(uuid4())
            values["request_id"] = str(uuid4())
            session.add(McpWorkspaceExecutionOperationRow(**values))
    if change == "broker_running":
        env.broker.state = ContainerRuntimeState.RUNNING
    if change == "stale_revision":
        revision -= 1
    with pytest.raises(HistoricalContainmentError, match="containment_not_allowed"):
        _resolve(service, execution_id, revision)
    with env.sessions() as session:
        count = session.scalar(
            select(func.count(HistoricalWorkspaceExecutionContainmentResolutionRow.id))
        )
        assert count == 0


def test_historical_containment_request_conflict(tmp_path: Path) -> None:
    _env, service, _request, execution_id, _stop, revision = _historical(tmp_path)
    request_id = uuid4()
    _resolve(service, execution_id, revision, request_id)
    with pytest.raises(HistoricalContainmentError, match="request_id_conflict"):
        service.resolve(
            request_id=request_id,
            execution_operation_id=uuid4(),
            managed_instance_id=TARGET_ID,
            expected_runtime_state_revision=revision,
        )


def test_populated_historical_containment_refuses_downgrade(
    tmp_path: Path,
) -> None:
    env, service, _request, execution_id, _stop, revision = _historical(tmp_path)
    _resolve(service, execution_id, revision)
    engine = env.sessions.kw["bind"]
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    command.stamp(config, "0066_historical_workspace_execution_containment")
    with pytest.raises(RuntimeError, match="persisted historical containment"):
        command.downgrade(config, "0065_workspace_execution_recovery")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0066_historical_workspace_execution_containment"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM "
                    "historical_workspace_execution_containment_resolutions"
                )
            )
            == 1
        )

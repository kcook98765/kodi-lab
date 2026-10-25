"""Recover stopped evidence without re-dispatch, and retain uncertainty otherwise."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.activity import ManagedInstanceActivityService
from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
)
from kodi_lab.application.workspace_deployment_recovery import (
    WorkspaceDeploymentRecoveryService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import InspectMcpWorkspaceDeploymentResult
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentRow,
    McpWorkspaceDeploymentOperationRow,
)
from tests.application import test_mcp_workspace_development as fixture


def inventory_failure(tmp_path: Path, *, historical: bool = False) -> tuple[Any, Any]:
    env = fixture._environment(tmp_path)
    inventory = env.development._inventory.get(fixture.TARGET_ID)
    env.development._inventory = cast(
        Any,
        SimpleNamespace(
            get=lambda _target: inventory.model_copy(update={"items": []}),
            original=env.development._inventory,
        ),
    )
    with env.sessions() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        assert workspace is not None and workspace.current_tree_digest is not None
        env.broker.sync_source_digest = workspace.current_tree_digest
        env.broker.sync_destination_digest = workspace.current_tree_digest
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_verification_failed"
    ):
        fixture._deploy_workspace(env)
    with env.sessions.begin() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == "reconciliation_required"
        assert row.sync_completed and row.failure_phase == "inventory"
        if historical:
            row.state = "failed"
        session.flush()
        session.expunge(row)
    return env, row


def test_inventory_failure_retains_fence_until_explicit_tree_inspection(
    tmp_path: Path,
) -> None:
    env, row = inventory_failure(tmp_path)
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixture.TARGET_ID
    )
    assert status.blocking and str(status.operation_uuid) == row.id
    assert ReconciliationAction.START in status.blocked_actions
    assert ReconciliationAction.STOP not in status.blocked_actions
    counts = (env.broker.sync_count, env.broker.start_count, env.broker.stop_count)
    broker = EvidenceBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"
    assert result.failure_cause == "deployment_verification_failed"
    assert not result.deployment_succeeded
    assert counts == (
        env.broker.sync_count,
        env.broker.start_count,
        env.broker.stop_count,
    )


@pytest.mark.parametrize("later_conflict", [False, True])
def test_historical_failed_copy_can_be_enrolled_after_no_write_conflict(
    tmp_path: Path, later_conflict: bool
) -> None:
    from datetime import timedelta

    from kodi_lab.storage.managed_activity import timestamp

    env, row = inventory_failure(tmp_path, historical=True)
    if later_conflict:
        values = {
            column.name: getattr(row, column.name)
            for column in McpWorkspaceDeploymentOperationRow.__table__.columns
        }
        values.update(
            id=str(uuid4()),
            request_id=str(uuid4()),
            sync_completed=False,
            target_started=False,
            result_classification=None,
            source_manifest_digest=None,
            verified_destination_digest=None,
            failure_phase="sync",
            failure_code="development_source_replacement_conflict",
            failure_cause="development_source_replacement_conflict",
            requested_at=timestamp(fixture.NOW + timedelta(minutes=1)),
            completed_at=timestamp(fixture.NOW + timedelta(minutes=2)),
        )
        with env.sessions.begin() as session:
            session.add(McpWorkspaceDeploymentOperationRow(**values))
    activity = ManagedInstanceActivityService(env.sessions).list_for_live_instance(
        fixture.TARGET_ID
    )
    assert next(
        item for item in activity if str(item.operation_uuid) == row.id
    ).deployment_recovery_available
    broker = EvidenceBroker(env, row)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    result = service.reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"
    with env.sessions() as session:
        contained = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert contained is not None and contained.state == "failed"
        assert contained.completed_at == row.completed_at
        assert contained.failure_cause == row.failure_cause
        assert contained.recovery_state == "contained_requested_tree"
    replay = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert replay.replayed and broker.reads == 1

    # A new explicit deployment can use the contained tree; the failed request
    # itself is never replayed as a mutation.
    env.development._inventory = env.development._inventory.original
    env.development._clock = lambda: fixture.NOW + timedelta(minutes=10)
    env.development._deployment_rate = BoundedIntervalRateLimiter(
        300, clock=lambda: 2000.0
    )
    env.broker.state = ContainerRuntimeState.RUNNING
    fixture._deploy_workspace(env)
    with env.sessions() as session:
        original = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert original is not None and original.state == "failed"
        assert original.recovery_state == "contained_requested_tree"


def test_historical_inspection_interruption_persists_fence_before_broker_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, row = inventory_failure(tmp_path, historical=True)
    broker = EvidenceBroker(env, row)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )

    def interrupted(_target: UUID, _operation: UUID) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(service, "_reconcile_locked", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.reconcile(fixture.TARGET_ID, UUID(row.id))
    assert broker.reads == 0
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixture.TARGET_ID
    )
    assert status.blocking and str(status.operation_uuid) == row.id
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"


@pytest.mark.parametrize(
    "defect", ["generation", "digest", "container", "runtime", "lease", "baseline"]
)
def test_historical_failed_copy_admission_rejects_drift_without_enrolling(
    tmp_path: Path, defect: str
) -> None:
    from kodi_lab.domain import ManagedInstanceMutationKind
    from kodi_lab.storage.models import ManagedInstanceRow
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

    env, row = inventory_failure(tmp_path, historical=True)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        operation = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert workspace is not None and operation is not None
        if defect == "generation":
            workspace.generation += 1
        elif defect == "digest":
            operation.source_manifest_digest = "sha256:" + "a" * 64
        elif defect == "container":
            operation.original_container_id = "f" * 64
        elif defect == "runtime":
            instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
            assert instance is not None
            operation.runtime_revision_id = str(uuid4())
        elif defect == "baseline":
            baseline = session.get(
                DevelopmentSourceDeploymentRow,
                (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
            )
            assert baseline is not None
            session.delete(baseline)
        else:
            ManagedInstanceMutationLeaseRepository(session).reserve(
                fixture.TARGET_ID,
                kind=ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
                operation_id=uuid4(),
            )
    broker = EvidenceBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "still_requires_admin" and broker.reads == 0
    with env.sessions() as session:
        retained = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert retained is not None and retained.state == "failed"
        assert retained.recovery_state is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sync_completed", False),
        ("verified_destination_digest", None),
        ("source_manifest_digest", None),
        ("failure_phase", "sync"),
        ("failure_code", "development_source_replacement_conflict"),
    ],
)
def test_unrelated_or_incomplete_terminal_failure_cannot_be_enrolled(
    tmp_path: Path, field: str, value: Any
) -> None:
    from kodi_lab.application.workspace_deployment_recovery import (
        WorkspaceDeploymentRecoveryError,
    )

    env, row = inventory_failure(tmp_path, historical=True)
    with env.sessions.begin() as session:
        current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert current is not None
        setattr(current, field, value)
        if field == "sync_completed" and value is False:
            current.target_started = False
    broker = EvidenceBroker(env, row)
    with pytest.raises(WorkspaceDeploymentRecoveryError, match="not_reconcilable"):
        WorkspaceDeploymentRecoveryService(
            env.sessions, broker, data_root=tmp_path / "data"
        ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert broker.reads == 0


def test_historical_failed_copy_unavailable_proof_retains_the_new_fence(
    tmp_path: Path,
) -> None:
    env, row = inventory_failure(tmp_path, historical=True)
    broker = EvidenceBroker(env, row, fail=True)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "still_requires_admin"
    assert (
        ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )
    with env.sessions() as session:
        current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert current is not None and current.verified_destination_digest is not None
        assert current.recovery_digest is None
        assert current.failure_cause == "deployment_verification_failed"


def test_historical_enrollment_crosses_real_broker_resolver_state_contract(
    tmp_path: Path,
) -> None:
    from kodi_lab.broker.development_sources import (
        SqliteDevelopmentSourceDeploymentResolver,
    )

    env, row = inventory_failure(tmp_path, historical=True)
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        env.sessions, development_root=tmp_path / "sources", data_root=tmp_path / "data"
    )

    class ResolvedBroker(EvidenceBroker):
        def inspect_mcp_workspace_deployment(
            self, **kwargs: Any
        ) -> InspectMcpWorkspaceDeploymentResult:
            spec = resolver.resolve_workspace_reconciliation(**kwargs)
            assert spec.require_stopped and spec.requested_source_digest is not None
            self.reads += 1
            return InspectMcpWorkspaceDeploymentResult(
                deployment_operation_id=row.id,
                managed_instance_id=spec.managed_instance_id,
                development_source_id=spec.development_source_id,
                runtime_revision_id=spec.runtime_revision_id,
                container_id=spec.trusted_container_id,
                requested_manifest_digest=spec.requested_source_digest,
                requested_destination_digest=spec.verified_destination_digest,
                installed_manifest_digest=spec.verified_destination_digest,
                matches_requested=True,
                matches_prior=spec.verified_destination_digest
                == spec.alternate_destination_digest,
            )

    broker = ResolvedBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree" and broker.reads == 1


def broken(
    tmp_path: Path, phase: str = "after_stop", *, database_path: Path | None = None
) -> tuple[Any, Any]:
    env = fixture._environment(tmp_path, database_path=database_path)

    def fail(selected: str) -> None:
        if selected == phase:
            raise RuntimeError("injected interruption; never publish this text")

    env.development._failure = fail
    with pytest.raises(McpWorkspaceDevelopmentError):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixture.TARGET_ID,
        )
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == "reconciliation_required"
        session.expunge(row)
    return env, row


class EvidenceBroker:
    def __init__(
        self, env: Any, row: Any, *, prior: bool = False, fail: bool = False
    ) -> None:
        self.env, self.row, self.prior, self.fail = env, row, prior, fail
        self.reads = 0
        self.prior_digest: str | None = None

    def managed_container_inventory(self) -> Any:
        result = self.env.broker.managed_container_inventory()
        return result.model_copy(
            update={
                "containers": [
                    item.model_copy(
                        update={"runtime_revision_id": str(fixture.RUNTIME_REVISION_ID)}
                    )
                    for item in result.containers
                ]
            }
        )

    def inspect_mcp_workspace_deployment(
        self, **kwargs: Any
    ) -> InspectMcpWorkspaceDeploymentResult:
        self.reads += 1
        if self.fail:
            raise RuntimeError("unavailable or partial tree")
        assert kwargs == dict(
            deployment_operation_id=self.row.id,
            managed_instance_id=str(fixture.TARGET_ID),
            expected_container_id=fixture.CONTAINER_ID,
        )
        with self.env.sessions() as session:
            workspace = session.get(AgentWorkspaceRow, str(self.env.workspace_id))
            baseline = session.get(
                DevelopmentSourceDeploymentRow,
                (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
            )
            assert workspace is not None and baseline is not None
            expected = workspace.current_tree_digest
            actual = (
                (self.prior_digest or baseline.verified_destination_digest)
                if self.prior
                else expected
            )
        return InspectMcpWorkspaceDeploymentResult(
            deployment_operation_id=self.row.id,
            managed_instance_id=str(fixture.TARGET_ID),
            development_source_id=str(fixture.SOURCE_ID),
            runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
            container_id=fixture.CONTAINER_ID,
            requested_manifest_digest=expected,
            requested_destination_digest=expected,
            installed_manifest_digest=actual,
            matches_requested=actual == expected,
            matches_prior=self.prior,
        )


@pytest.mark.parametrize(
    "phase",
    ["after_stop", "after_sync_before_readiness", "after_readiness_before_evidence"],
)
@pytest.mark.parametrize("prior", [False, True])
def test_verified_stopped_recovery_preserves_receipt_and_never_claims_success(
    tmp_path: Path, phase: str, prior: bool
) -> None:
    env, row = broken(tmp_path, phase)
    broker = EvidenceBroker(env, row, prior=prior)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    counts = (env.broker.stop_count, env.broker.sync_count, env.broker.start_count)
    result = service.reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == (
        "contained_prior_tree" if prior else "contained_requested_tree"
    )
    assert not result.deployment_succeeded
    assert str(result.request_id) == row.request_id
    assert result.failed_phase in {"stop", "sync", "readiness"}
    assert "injected interruption" not in result.model_dump_json()
    assert (
        not ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )
    replay = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert replay.replayed and broker.reads == 1
    assert (
        env.broker.stop_count,
        env.broker.sync_count,
        env.broker.start_count,
    ) == counts
    history = ManagedInstanceActivityService(env.sessions).list_for_live_instance(
        fixture.TARGET_ID
    )
    item = next(item for item in history if str(item.operation_uuid) == row.id)
    assert "contained_" in item.summary


def test_partial_copy_or_unavailable_broker_stays_blocking(tmp_path: Path) -> None:
    env, row = broken(tmp_path)
    broker = EvidenceBroker(env, row, fail=True)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    assert (
        service.reconcile(fixture.TARGET_ID, UUID(row.id)).outcome
        == "still_requires_admin"
    )
    assert (
        ManagedInstanceReconciliationService(env.sessions)
        .status(fixture.TARGET_ID)
        .blocking
    )
    with env.sessions() as session:
        stored = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert stored is not None and stored.failure_phase == "stop"


def test_running_target_offers_stop_without_automatic_mutation(tmp_path: Path) -> None:
    env, row = broken(tmp_path)
    env.broker.state = ContainerRuntimeState.RUNNING
    broker = EvidenceBroker(env, row)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    result = service.reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.reason == "stop_target_then_recheck" and broker.reads == 0
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixture.TARGET_ID
    )
    assert ReconciliationAction.STOP not in status.blocked_actions
    assert ReconciliationAction.START in status.blocked_actions


def test_historical_operation_uses_retained_baseline_not_current_container_guess(
    tmp_path: Path,
) -> None:
    env, row = broken(tmp_path)
    with env.sessions.begin() as session:
        current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert current is not None
        current.original_container_id = None
        current.original_runtime_state_revision = None
        current.failure_phase = current.failure_cause = None
    broker = EvidenceBroker(env, row, prior=True)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    result = service.reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_prior_tree" and result.failed_phase is None


def test_changed_workspace_does_not_clear_fence(tmp_path: Path) -> None:
    env, row = broken(tmp_path)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        assert workspace is not None
        workspace.generation += 1
    broker = EvidenceBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "still_requires_admin" and broker.reads == 0


def test_recovered_tree_allows_a_new_deployment_but_does_not_replay_the_old_one(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    env, row = broken(tmp_path)
    broker = EvidenceBroker(env, row)
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    assert result.outcome == "contained_requested_tree"
    env.development._failure = lambda _phase: None
    env.development._clock = lambda: fixture.NOW + timedelta(minutes=10)
    env.development._deployment_rate = BoundedIntervalRateLimiter(
        300, clock=lambda: 2000.0
    )
    counts = (env.broker.sync_count, env.broker.start_count)
    with pytest.raises(McpWorkspaceDevelopmentError):
        env.development.deploy(
            client_id=env.client_id,
            request_id=UUID(row.request_id),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixture.TARGET_ID,
        )
    assert counts == (env.broker.sync_count, env.broker.start_count)
    env.broker.state = ContainerRuntimeState.RUNNING
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixture.TARGET_ID,
    )
    assert deployed.state == "succeeded"
    assert env.broker.sync_count == counts[0] + 1


def test_concurrent_rechecks_publish_one_containment_receipt(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env, row = broken(tmp_path)
    broker = EvidenceBroker(env, row)
    service = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.reconcile(fixture.TARGET_ID, UUID(row.id)), range(2)
            )
        )
    assert {result.outcome for result in results} == {"contained_requested_tree"}
    assert sum(result.replayed for result in results) == 1
    assert broker.reads == 1


@pytest.mark.parametrize("contradictory_container", [False, True])
def test_latest_verified_workspace_tree_is_the_prior_copy(
    tmp_path: Path, contradictory_container: bool
) -> None:
    from datetime import timedelta

    from kodi_lab.storage.managed_activity import timestamp

    env, row = broken(tmp_path)
    values = {
        column.name: getattr(row, column.name)
        for column in McpWorkspaceDeploymentOperationRow.__table__.columns
    }
    values.update(
        id=str(uuid4()),
        request_id=str(uuid4()),
        state="succeeded",
        failure_code=None,
        failure_cause=None,
        failure_phase=None,
        source_manifest_digest="sha256:" + "b" * 64,
        verified_destination_digest="sha256:" + "d" * 64,
        mutation_started=True,
        target_stopped=True,
        sync_started=True,
        sync_completed=True,
        target_started=True,
        inventory_classification="matched",
        addon_enabled=True,
        result_classification="deployed",
        requested_at=timestamp(fixture.NOW - timedelta(minutes=2)),
        started_at=timestamp(fixture.NOW - timedelta(minutes=2)),
        completed_at=timestamp(fixture.NOW - timedelta(minutes=1)),
        original_container_id="f" * 64
        if contradictory_container
        else fixture.CONTAINER_ID,
    )
    with env.sessions.begin() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
        )
        assert baseline is not None
        baseline.verified_at = timestamp(fixture.NOW - timedelta(minutes=3))
        session.add(McpWorkspaceDeploymentOperationRow(**values))
    broker = EvidenceBroker(env, row, prior=True)
    broker.prior_digest = "sha256:" + "d" * 64
    result = WorkspaceDeploymentRecoveryService(
        env.sessions, broker, data_root=tmp_path / "data"
    ).reconcile(fixture.TARGET_ID, UUID(row.id))
    if contradictory_container:
        assert result.outcome == "still_requires_admin"
    else:
        assert result.outcome == "contained_prior_tree"
        with env.sessions() as session:
            current = session.get(McpWorkspaceDeploymentOperationRow, row.id)
            assert current is not None
            assert current.source_manifest_digest == "sha256:" + "b" * 64
            assert current.verified_destination_digest == "sha256:" + "d" * 64

"""Repeated deployment admission uses exact verified receipts, never fallback."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
)
from kodi_lab.application.workspace_launch import (
    WorkspaceLaunchError,
    WorkspaceLaunchService,
)
from kodi_lab.broker.operations import VerifyAttachedDevelopmentSourceDestinationResult
from kodi_lab.broker.smoke_diagnostics import SqliteSmokeLogAuthorityResolver
from kodi_lab.kodi.addons import KodiAddonSmokeTimeoutError
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
    RepositoryWorkspaceDeploymentRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application import test_repository_existing_adoption as adoption
from tests.application import test_workspace_launch as launches
from tests.application.test_session_profile_lineage import fresh_binding
from tests.application.test_session_workspace_admission import bind


def repeated(
    tmp_path: Path, count: int = 3
) -> tuple[fixtures.Environment, list[str], launches.Bridge, WorkspaceLaunchService]:
    env = fixtures._environment(tmp_path)
    env.development._deployment_rate = BoundedIntervalRateLimiter(0)
    ids = []
    for index in range(count):
        instant = fixtures.NOW + timedelta(seconds=index)

        def clock(instant: datetime = instant) -> datetime:
            return instant

        env.development._clock = clock
        result = env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
        )
        assert result.state == "succeeded"
        ids.append(str(result.deployment_id))
    bridge = launches.Bridge()
    service = WorkspaceLaunchService(
        env.sessions,
        env.development,
        bridge,
        data_root=tmp_path / "data",
        clock=lambda: fixtures.NOW + timedelta(minutes=1),
    )
    return env, ids, bridge, service


def invoke(
    env: fixtures.Environment, service: WorkspaceLaunchService, action: str
) -> Any:
    if action == "execute":
        return fixtures._execute_workspace(env)
    return launches.launch(env, service)


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("action", ["execute", "launch"])
def test_latest_repeated_verified_deployment(
    tmp_path: Path, count: int, action: str
) -> None:
    env, ids, bridge, service = repeated(tmp_path, count)
    result = invoke(env, service, action)
    assert result.state == ("succeeded" if action == "execute" else "accepted")
    if action == "execute":
        assert str(result.deployment_id) == ids[-1]
    else:
        with env.sessions() as session:
            row = session.get(McpWorkspaceLaunchRow, str(result.launch_id))
            assert row is not None and row.deployment_id == ids[-1]
    assert env.smoke.count == (1 if action == "execute" else 0)
    assert env.broker.verify_count == (2 if action == "execute" else 1)
    assert bridge.calls == ([] if action == "execute" else [False])


@pytest.mark.parametrize("action", ["execute", "launch"])
@pytest.mark.parametrize(
    "changes",
    [
        {"state": "failed", "failure_code": "deployment_interrupted"},
        {"state": "ambiguous", "failure_code": "deployment_ambiguous"},
        {"state": "reconciliation_required", "failure_code": "deployment_ambiguous"},
        {"state": "pending", "started_at": None, "completed_at": None},
        {"state": "running", "completed_at": None},
        {"state": "failed", "inventory_classification": "unknown"},
        {"state": "failed", "sync_completed": False, "target_started": False},
        {"verified_destination_digest": "sha256:" + "f" * 64},
        {"state": "failed", "verified_destination_digest": None},
        {"source_manifest_digest": "sha256:" + "e" * 64},
        {"generation": 1},
        {"phase": "inventory"},
        {"addon_enabled": False},
        {"original_container_id": "b" * 64},
        {"original_container_id": None},
    ],
)
def test_newer_attempt_never_falls_back(
    tmp_path: Path, action: str, changes: dict[str, Any]
) -> None:
    env, ids, bridge, service = repeated(tmp_path)
    with env.sessions.begin() as session:
        latest = session.get(McpWorkspaceDeploymentOperationRow, ids[-1])
        assert latest is not None
        for key, value in changes.items():
            setattr(latest, key, value)
    try:
        result = invoke(env, service, action)
    except (McpWorkspaceDevelopmentError, WorkspaceLaunchError):
        pass
    else:
        assert action == "launch" and result.state == "failed"
    assert env.smoke.count == 0 and bridge.calls == []


@pytest.mark.parametrize("action", ["execute", "launch"])
@pytest.mark.parametrize("tie", [False, True])
def test_receipt_order_is_deterministic_and_ties_refuse(
    tmp_path: Path, action: str, tie: bool
) -> None:
    env, ids, bridge, service = repeated(tmp_path, 2)
    lower_id, higher_id = sorted(ids)
    with env.sessions.begin() as session:
        lower = session.get(McpWorkspaceDeploymentOperationRow, lower_id)
        higher = session.get(McpWorkspaceDeploymentOperationRow, higher_id)
        assert lower is not None and higher is not None
        # A lexically smaller UUID can be newest. Equal times cannot establish
        # authority, regardless of UUID or identical successful copy evidence.
        higher.requested_at = fixtures.NOW_TEXT
        lower.requested_at = (
            higher.requested_at if tie else "2026-09-20T18:00:01.000000Z"
        )
    if tie:
        with pytest.raises((McpWorkspaceDevelopmentError, WorkspaceLaunchError)):
            invoke(env, service, action)
        assert env.smoke.count == 0 and bridge.calls == []
        with env.sessions() as session:
            assert session.scalar(select(McpWorkspaceExecutionOperationRow)) is None
    else:
        result = invoke(env, service, action)
        if action == "execute":
            assert str(result.deployment_id) == lower_id
        else:
            with env.sessions() as session:
                row = session.get(McpWorkspaceLaunchRow, str(result.launch_id))
                assert row is not None and row.deployment_id == lower_id


@pytest.mark.parametrize("action", ["execute", "launch"])
@pytest.mark.parametrize("drift", ["bytes", "generation", "recorded_digest"])
def test_changed_workspace_refuses(tmp_path: Path, action: str, drift: str) -> None:
    env, _ids, bridge, service = repeated(tmp_path)
    if drift == "bytes":
        tree = tmp_path / "data" / "agent-workspaces" / str(env.workspace_id) / "tree"
        (tree / "main.py").write_text("# unrecorded change\n", encoding="utf-8")
    else:
        with env.sessions.begin() as session:
            workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
            assert workspace is not None
            if drift == "generation":
                workspace.generation += 1
            else:
                workspace.current_tree_digest = "sha256:" + "f" * 64
    with pytest.raises((McpWorkspaceDevelopmentError, WorkspaceLaunchError)):
        invoke(env, service, action)
    assert env.smoke.count == 0 and bridge.calls == []


@pytest.mark.parametrize("action", ["execute", "launch"])
@pytest.mark.parametrize("change", ["replaced", "retired", "revision"])
def test_profile_change_refuses_repeated_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, change: str
) -> None:
    env, _ids, bridge, service = repeated(tmp_path)
    if change == "replaced":
        fresh_binding(env, monkeypatch)
    elif change == "revision":
        bind(env, mismatch="revision")
    else:
        bind(env)
        with env.sessions.begin() as session:
            selected = session.get(DevelopmentSessionRow, 1)
            assert selected is not None
            selected.phase = "closed"
    with pytest.raises((McpWorkspaceDevelopmentError, WorkspaceLaunchError)):
        invoke(env, service, action)
    assert env.smoke.count == 0 and bridge.calls == []


def test_exact_execution_replay_keeps_original_receipt_after_newer_failure(
    tmp_path: Path,
) -> None:
    env, ids, _bridge, _service = repeated(tmp_path)
    request = uuid4()
    first = fixtures._execute_workspace(env, request_id=request)
    with env.sessions.begin() as session:
        latest = session.get(McpWorkspaceDeploymentOperationRow, ids[-1])
        assert latest is not None
        latest.state = "failed"
    assert fixtures._execute_workspace(env, request_id=request) == first
    assert env.smoke.count == 1 and env.broker.verify_count == 2


def test_ambiguous_repeated_deployment_execution_is_never_retried(
    tmp_path: Path,
) -> None:
    env, _ids, _bridge, _service = repeated(tmp_path)

    def timeout(_addon_id: str) -> Any:
        env.smoke.count += 1
        raise KodiAddonSmokeTimeoutError(5)

    env.smoke.invoke = timeout  # type: ignore[assignment]
    request = uuid4()
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
            fixtures._execute_workspace(env, request_id=request)
    assert env.smoke.count == 1


def test_post_invocation_destination_verification_remains_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _ids, _bridge, _service = repeated(tmp_path)
    verify = env.broker.verify_mcp_workspace_destination

    def drift(**kwargs: Any) -> VerifyAttachedDevelopmentSourceDestinationResult:
        result = verify(**kwargs)
        return result.model_copy(update={"matched": env.smoke.count == 0})

    monkeypatch.setattr(env.broker, "verify_mcp_workspace_destination", drift)
    request = uuid4()
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
            fixtures._execute_workspace(env, request_id=request)
    assert env.smoke.count == 1 and env.broker.verify_count == 2
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None and row.invocation_dispatched
        assert row.state == "reconciliation_required"


@pytest.mark.parametrize("action", ["execute", "launch"])
@pytest.mark.parametrize("failure", ["cooldown", "interrupted", "uncertain"])
def test_active_profile_unbound_latest_attempt_is_refusal_evidence(
    tmp_path: Path, action: str, failure: str
) -> None:
    env, _ids, bridge, service = repeated(tmp_path)
    bind(env)
    env.development._clock = lambda: fixtures.NOW + timedelta(seconds=4)
    request = uuid4()
    if failure == "cooldown":
        limiter = BoundedIntervalRateLimiter(300, clock=lambda: 1000.0)
        assert limiter.allow(f"{env.client_id}:{fixtures.TARGET_ID}")
        env.development._deployment_rate = limiter
        with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
            env.development.deploy(
                client_id=env.client_id,
                request_id=request,
                workspace_id=env.workspace_id,
                expected_generation=0,
                target_id=fixtures.TARGET_ID,
            )
    else:
        claimed, created = env.development._claim_deployment(
            env.client_id, request, env.workspace_id, 0, fixtures.TARGET_ID
        )
        assert created and claimed.original_container_id is None
        if failure == "uncertain":
            with env.sessions.begin() as session:
                row = session.get(McpWorkspaceDeploymentOperationRow, claimed.id)
                assert row is not None
                row.mutation_started = True
        assert env.development.sweep_startup() == 1
    with env.sessions() as session:
        latest = session.scalar(
            select(McpWorkspaceDeploymentOperationRow).where(
                McpWorkspaceDeploymentOperationRow.request_id == str(request)
            )
        )
        assert latest is not None and latest.original_container_id is None
        assert latest.state == (
            "reconciliation_required" if failure == "uncertain" else "failed"
        )
    with pytest.raises((McpWorkspaceDevelopmentError, WorkspaceLaunchError)):
        invoke(env, service, action)
    assert env.smoke.count == 0 and bridge.calls == []
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceExecutionOperationRow)) is None


class DiagnosticDocker:
    """Exact fake native labels; never connects to a daemon."""

    def containers(self, **_kwargs: object) -> list[dict[str, str]]:
        return [{"Id": fixtures.CONTAINER_ID}]

    def inspect_container(self, _container_id: str) -> dict[str, object]:
        return {
            "Id": fixtures.CONTAINER_ID,
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "io.kodi-lab.managed": "true",
                    "io.kodi-lab.instance-id": str(fixtures.TARGET_ID),
                    "io.kodi-lab.runtime-revision-id": str(
                        fixtures.RUNTIME_REVISION_ID
                    ),
                }
            },
        }

    def close(self) -> None:
        pass


def test_repeated_deployments_keep_production_smoke_log_authority(
    tmp_path: Path,
) -> None:
    env, ids, _bridge, _service = repeated(tmp_path)
    bind(env)
    resolver = SqliteSmokeLogAuthorityResolver(env.sessions, DiagnosticDocker)
    observed = []

    def capture(execution_id: str) -> str:
        authority = resolver.resolve(execution_id)
        observed.append(authority.container_id)
        with env.sessions() as session:
            execution = session.get(McpWorkspaceExecutionOperationRow, execution_id)
            assert execution is not None and execution.deployment_id == ids[-1]
        return authority.container_id

    env.broker.capture_hook = capture
    result = fixtures._execute_workspace(env)
    assert result.state == "succeeded" and env.smoke.count == 1
    assert observed == [fixtures.CONTAINER_ID]
    assert (env.broker.capture_count, env.broker.read_count) == (1, 0)


@pytest.mark.parametrize("newer_failed", [False, True])
def test_external_repeated_direct_deployment_keeps_verified_receipt_checks(
    tmp_path: Path, newer_failed: bool
) -> None:
    env = fixtures._environment(tmp_path)
    bridge, targets = fixtures._configure_external_environment(env)
    latest = None
    for index in range(2):
        instant = fixtures.NOW + timedelta(seconds=index + 1)

        def clock(instant: datetime = instant) -> datetime:
            return instant

        env.development._clock = clock
        latest = env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.EXTERNAL_TARGET_ID,
        )
    assert latest is not None and bridge.replacement_count == 2
    if newer_failed:
        with env.sessions.begin() as session:
            row = session.get(
                McpWorkspaceDeploymentOperationRow, str(latest.deployment_id)
            )
            assert row is not None
            row.state = "failed"
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="execution_not_deployed"
        ):
            env.development.execute(
                client_id=env.client_id,
                request_id=uuid4(),
                workspace_id=env.workspace_id,
                expected_generation=0,
                target_id=fixtures.EXTERNAL_TARGET_ID,
            )
        assert targets.execution_count == 0
    else:
        result = env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.EXTERNAL_TARGET_ID,
        )
        assert result.state == "succeeded"
        assert result.deployment_id == latest.deployment_id
        assert targets.execution_count == 1


@pytest.mark.parametrize("changed_destination", [False, True])
def test_later_repository_receipt_requires_live_verified_destination(
    tmp_path: Path, changed_destination: bool
) -> None:
    env, _repository, target, publication, identity = adoption._approved_existing(
        tmp_path
    )
    adopted = adoption._adopt(env, publication, identity)
    second_id = str(uuid4())
    with env.sessions.begin() as session:
        first = session.get(
            McpWorkspaceDeploymentOperationRow, str(adopted.deployment_id)
        )
        first_link = session.get(
            RepositoryWorkspaceDeploymentRow, str(adopted.deployment_id)
        )
        assert first is not None and first_link is not None
        values = {
            column.key: getattr(first, column.key) for column in first.__table__.columns
        }
        values.update(
            id=second_id,
            client_id=str(env.client_id),
            initiator_kind="mcp",
            repository_origin="planned",
            request_id=str(uuid4()),
            requested_at="2026-09-20T18:00:02.000000Z",
            started_at="2026-09-20T18:00:02.000000Z",
            completed_at="2026-09-20T18:00:03.000000Z",
        )
        linked = {
            column.key: getattr(first_link, column.key)
            for column in first_link.__table__.columns
        }
        linked.update(
            operation_id=second_id,
            origin="planned",
            verified_by=None,
            identity_sha256=None,
            publication_source_id=None,
            approved_at=first_link.created_at,
        )
        if changed_destination:
            digest = "sha256:" + "0" * 64
            values.update(
                source_manifest_digest=digest, verified_destination_digest=digest
            )
            linked["expected_manifest_digest"] = digest
        session.add(McpWorkspaceDeploymentOperationRow(**values))
        session.flush()
        session.add(RepositoryWorkspaceDeploymentRow(**linked))
    if changed_destination:
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="target_provenance_mismatch"
        ):
            env.development.execute(
                client_id=env.client_id,
                request_id=uuid4(),
                workspace_id=env.workspace_id,
                expected_generation=publication.workspace_generation,
                target_id=fixtures.EXTERNAL_TARGET_ID,
            )
        assert target.executions == 0
    else:
        result = env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=publication.workspace_generation,
            target_id=fixtures.EXTERNAL_TARGET_ID,
        )
        assert str(result.deployment_id) == second_id
        assert result.state == "succeeded" and target.executions == 1

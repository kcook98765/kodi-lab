"""Durable preflight uses scope authority and never probes, repairs or reserves."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.domain import (
    AgentWorkspaceState,
    DevelopmentSourceDeploymentConfidence,
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    TargetType,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceRow,
    McpWorkspaceValidationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from tests.application import test_mcp_workspace_development as fixture


def operation(env: Any, **changes: Any) -> DeploymentPreflightInput:
    return DeploymentPreflightInput.model_validate(
        {
            "workspace_id": env.workspace_id,
            "expected_generation": 0,
            "target_id": fixture.TARGET_ID,
            **changes,
        }
    )


def setup_tools(env: Any) -> tuple[Any, Any, Any]:
    admin = McpAdministrationService(env.sessions)
    admin.grant_development_source(env.client_id, fixture.SOURCE_ID)
    admin.grant_target(env.client_id, fixture.TARGET_ID)
    admin.assign_target_preset(fixture.TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    principal = McpAuthenticationService(env.sessions).authenticate(
        admin.issue_credential(env.client_id).token
    )
    assert principal is not None

    class NoTechnicalProbe:
        def resolve(self, _target: Any) -> Any:
            raise AssertionError("preflight must not use converging technical status")

    auth = McpAuthorizationService(
        env.sessions, build_mcp_tool_registry(), targets=NoTechnicalProbe()
    )
    tools = McpTargetToolService(
        env.sessions, auth, cast(Any, None), cast(Any, None), cast(Any, None)
    )
    return tools, principal, admin


def reasons(result: Any) -> dict[str, Any]:
    return {item.name: item for item in result.checks}


def test_native_and_bridge_unknown_stay_separate_from_stored_readiness_without_writes(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    tools, principal, _admin = setup_tools(env)
    # Even a fully matching stored validation does not prove live native state.
    with env.sessions.begin() as session:
        validation = session.scalar(select(McpWorkspaceValidationRow))
        assert validation is not None
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        assert workspace is not None
        validation.tree_digest = workspace.current_tree_digest
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip().split()[0].upper())

    engine = env.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = tools.workspace_deploy_preflight(principal, operation(env))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert set(statements) <= {"SELECT", "PRAGMA"}
    assert result.verdict == "unknown" and not result.reservation_created
    checks = reasons(result)
    assert checks["native"].reason == "native_not_observed"
    assert checks["addon"].state == "unknown"
    assert checks["bridge"].state == "not_required"
    assert (
        result.bridge_trust_state == "uninstalled"
        and result.live_bridge_readiness == "unknown"
    )
    assert checks["source_baseline"].evidence_at == fixture.NOW
    assert checks["source_baseline"].state == "unknown"
    assert env.broker.stop_count == env.broker.sync_count == env.broker.start_count == 0


@pytest.mark.parametrize("defect", ["missing", "stale", "unknown"])
def test_baseline_setup_reasons_are_available_before_a_failed_deployment(
    tmp_path: Path, defect: str
) -> None:
    env = fixture._environment(tmp_path)
    with env.sessions.begin() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)),
        )
        assert baseline is not None
        if defect == "missing":
            session.delete(baseline)
        else:
            baseline.confidence = DevelopmentSourceDeploymentConfidence(defect)
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert result.verdict == "blocked"
    assert reasons(result)["source_baseline"].reason == f"source_baseline_{defect}"
    action = next(item for item in result.next_actions if item.action == "copy_source")
    assert action.authority == "web_administrator" and action.target_state == "stopped"


def test_prior_copy_needs_container_proof_without_requiring_another_source_copy(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    fixture._deploy_workspace(env)
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(operation(env))
    assert (
        reasons(result)["source_baseline"].reason
        == "prior_workspace_requires_container_proof"
    )
    assert not any(item.action == "copy_source" for item in result.next_actions)
    assert result.verdict == "unknown"


@pytest.mark.parametrize(
    "defect",
    [
        "generation",
        "validation_missing",
        "validation_digest",
        "workspace",
        "leases",
        "stopped",
    ],
)
def test_independent_durable_blockers_do_not_mutate_or_claim_liveness(
    tmp_path: Path, defect: str
) -> None:
    env = fixture._environment(tmp_path)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        validation = session.scalar(select(McpWorkspaceValidationRow))
        assert workspace is not None and validation is not None
        if defect == "generation":
            workspace.generation += 1
        elif defect == "validation_missing":
            session.delete(validation)
        elif defect == "validation_digest":
            validation.tree_digest = "sha256:" + "f" * 64
        elif defect == "workspace":
            workspace.state = AgentWorkspaceState.RECONCILIATION_REQUIRED
        elif defect == "leases":
            ManagedInstanceMutationLeaseRepository(session).reserve(
                fixture.TARGET_ID,
                kind=ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
                operation_id=uuid4(),
            )
            GlobalHeavyOperationLeaseRepository(session).reserve(
                kind=GlobalHeavyOperationKind.MCP_WORKSPACE_DEPLOYMENT,
                operation_id=uuid4(),
            )
        else:
            instance = session.get(ManagedInstanceRow, str(fixture.TARGET_ID))
            assert instance is not None
            instance.runtime_state = InstanceRuntimeState.STOPPED
    tools, principal, _admin = setup_tools(env)
    result = tools.workspace_deploy_preflight(principal, operation(env))
    checks = reasons(result)
    expected = {
        "generation": ("generation", "workspace_generation_conflict"),
        "validation_missing": ("validation", "validation_required"),
        "validation_digest": ("validation", "validation_digest_mismatch"),
        "workspace": ("workspace", "workspace_not_ready"),
        "leases": ("target_lease", "target_busy"),
        "stopped": ("native", "native_not_observed"),
    }
    name, reason = expected[defect]
    assert checks[name].reason == reason
    assert result.verdict == ("unknown" if defect == "stopped" else "blocked")
    if defect == "leases":
        assert checks["global_lease"].reason == "global_busy"
        assert not any(
            "operation_id" in item.model_dump() for item in result.next_actions
        )
        with env.sessions() as session:
            assert (
                ManagedInstanceMutationLeaseRepository(session).current(
                    fixture.TARGET_ID
                )
                is not None
            )
            assert GlobalHeavyOperationLeaseRepository(session).current() is not None


@pytest.mark.parametrize(
    "denied", ["source", "target", "permission", "production", "revoked_after_read"]
)
def test_preflight_retains_exact_scope_and_rechecks_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    env = fixture._environment(tmp_path)
    tools, principal, admin = setup_tools(env)
    if denied == "source":
        admin.remove_development_source_grant(env.client_id, fixture.SOURCE_ID)
    elif denied == "target":
        admin.remove_target_grant(env.client_id, fixture.TARGET_ID)
    elif denied == "permission":
        from kodi_lab.application.mcp_administration import (
            READ_ONLY_INSPECTION_PRESET_ID,
        )

        admin.assign_target_preset(fixture.TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID)
    elif denied == "production":
        with env.sessions.begin() as session:
            target = session.get(TargetRow, str(fixture.TARGET_ID))
            assert target is not None
            target.target_type = TargetType.EXTERNAL_PRODUCTION
    service = WorkspaceDeploymentPreflightService.inspect
    reads = []

    def inspect(self: Any, op: Any) -> Any:
        reads.append(op)
        result = service(self, op)
        if denied == "revoked_after_read":
            admin.remove_development_source_grant(env.client_id, fixture.SOURCE_ID)
        return result

    monkeypatch.setattr(WorkspaceDeploymentPreflightService, "inspect", inspect)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy_preflight(principal, operation(env))
    assert len(reads) == int(denied == "revoked_after_read")


def test_repository_assessment_explicitly_unsupported(tmp_path: Path) -> None:
    env = fixture._environment(tmp_path)
    result = WorkspaceDeploymentPreflightService(env.sessions).inspect(
        operation(env, deployment_mode="repository")
    )
    assert result.resolved_mode is None and result.verdict == "unknown"
    assert reasons(result)["bridge"].reason == "bridge_not_assessed"

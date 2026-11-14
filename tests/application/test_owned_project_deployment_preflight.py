"""Private projects waive legacy setup, never authority or durable blockers."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.application.workspace_deployment_preflight import (
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.domain import GlobalHeavyOperationKind, ManagedInstanceMutationKind
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import (
    AgentProjectRow,
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    McpClientRow,
    McpWorkspaceValidationRow,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_session_workspace_admission import bind
from tests.application.test_workspace_deployment_preflight import (
    operation,
    reasons,
    setup_tools,
)
from tests.application.test_workspace_deployment_recovery import inventory_failure


def make_owned(env: fixture.Environment, *, generation: int = 0) -> None:
    """Retain real durable scope, with no manual attachment or source baseline."""
    with env.sessions.begin() as session:
        client = session.get(McpClientRow, str(env.client_id))
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        validation = session.scalar(select(McpWorkspaceValidationRow))
        assert client is not None and workspace is not None and validation is not None
        client.preset_id = str(READ_ONLY_INSPECTION_PRESET_ID)
        client.managed_lab_role_version = 1
        workspace.generation = generation
        validation.generation = generation
        validation.tree_digest = workspace.current_tree_digest
        session.add(
            AgentProjectRow(
                source_id=str(fixture.SOURCE_ID),
                client_id=str(env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="seeded",
                created_at=fixture.NOW_TEXT,
            )
        )
        for model in (
            DevelopmentSourceDeploymentRow,
            ManagedInstanceDevelopmentSourceRow,
        ):
            row = session.get(model, (str(fixture.TARGET_ID), str(fixture.SOURCE_ID)))
            assert row is not None
            session.delete(row)


@pytest.mark.parametrize("previous_deployment", [False, True])
def test_owned_setup_is_not_required_before_and_after_copy_without_writes_or_probes(
    tmp_path: Path, previous_deployment: bool
) -> None:
    env = fixture._environment(tmp_path)
    if previous_deployment:
        fixture._deploy_workspace(env)  # In-memory broker/inventory fixtures only.
    generation = int(previous_deployment)
    make_owned(env, generation=generation)
    tools, principal, admin = setup_tools(env)
    admin.remove_target_grant(env.client_id, fixture.TARGET_ID)
    counts = vars(env.broker).copy()
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip().split()[0].upper())

    engine = env.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = tools.workspace_deploy_preflight(
            principal, operation(env, expected_generation=generation)
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) <= {"SELECT", "PRAGMA"}
    assert vars(env.broker) == counts
    checks = reasons(result)
    assert (
        checks["attachment"].state == checks["source_baseline"].state == "not_required"
    )
    assert checks["attachment"].reason == "owned_project_attachment_not_required"
    assert checks["source_baseline"].reason == "owned_project_baseline_not_required"
    assert checks["source_baseline"].evidence_at is None
    assert checks["validation"].state == "satisfied"
    assert checks["native"].state == checks["addon"].state == "unknown"
    assert result.verdict == "unknown" and not result.reservation_created
    assert not {"attach_source", "copy_source"} & {
        item.action for item in result.next_actions
    }


@pytest.mark.parametrize("defect", ["owner", "role", "credential", "source_grant"])
def test_owned_preflight_rejects_missing_or_revoked_authority_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    env = fixture._environment(tmp_path)
    make_owned(env)
    tools, principal, admin = setup_tools(env)
    if defect == "owner":
        peer = admin.create_client(
            display_name="Another managed agent",
            preset_id=READ_ONLY_INSPECTION_PRESET_ID,
            trusted_managed_lab=True,
        )
        peer_principal = McpAuthenticationService(env.sessions).authenticate(
            admin.issue_credential(peer.id).token
        )
        assert peer_principal is not None
        principal = peer_principal
    elif defect == "role":
        admin.set_managed_lab_role(env.client_id, False)
    elif defect == "credential":
        admin.revoke_credential(env.client_id, principal.credential_id)
    else:
        admin.remove_development_source_grant(env.client_id, fixture.SOURCE_ID)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unauthorized preflight must not inspect private state")

    monkeypatch.setattr(WorkspaceDeploymentPreflightService, "inspect", forbidden)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy_preflight(principal, operation(env))


@pytest.mark.parametrize("revoke", ["role", "credential"])
def test_owned_preflight_rechecks_authority_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revoke: str
) -> None:
    env = fixture._environment(tmp_path)
    make_owned(env)
    tools, principal, admin = setup_tools(env)
    inspect = WorkspaceDeploymentPreflightService.inspect

    def withdraw(self: Any, op: Any, **kwargs: Any) -> Any:
        assert kwargs["client_id"] == principal.client_id
        result = inspect(self, op, **kwargs)
        if revoke == "role":
            admin.set_managed_lab_role(env.client_id, False)
        else:
            admin.revoke_credential(env.client_id, principal.credential_id)
        return result

    monkeypatch.setattr(WorkspaceDeploymentPreflightService, "inspect", withdraw)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_deploy_preflight(principal, operation(env))


def test_project_row_alone_and_a_different_caller_do_not_waive_legacy_setup(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    make_owned(env)
    service = WorkspaceDeploymentPreflightService(env.sessions)
    # A seeded row without the owner's source grant is insufficient.
    for client_id in (env.client_id, uuid4()):
        result = service.inspect(operation(env), client_id=client_id)
        assert reasons(result)["attachment"].reason == "source_not_attached"
        assert reasons(result)["source_baseline"].reason == "source_baseline_missing"
    setup_tools(env)
    # All owner evidence now exists, but it cannot be borrowed by another caller.
    result = service.inspect(operation(env), client_id=uuid4())
    assert reasons(result)["attachment"].state == "blocked"
    assert reasons(result)["source_baseline"].state == "blocked"


@pytest.mark.parametrize(
    ("defect", "check", "reason"),
    [
        ("generation", "generation", "workspace_generation_conflict"),
        ("validation_missing", "validation", "validation_required"),
        ("validation_generation", "validation", "validation_generation_mismatch"),
        ("validation_digest", "validation", "validation_digest_mismatch"),
        ("source", "source", "source_identity_changed"),
        ("leases", "target_lease", "target_busy"),
        ("session", "session", "development_session_requires_review"),
    ],
)
def test_owned_setup_exemption_keeps_independent_blockers(
    tmp_path: Path, defect: str, check: str, reason: str
) -> None:
    env = fixture._environment(tmp_path)
    make_owned(env)
    tools, principal, _admin = setup_tools(env)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        validation = session.scalar(select(McpWorkspaceValidationRow))
        assert workspace is not None and validation is not None
        if defect == "generation":
            workspace.generation += 1
        elif defect == "validation_missing":
            session.delete(validation)
        elif defect == "validation_generation":
            validation.generation += 1
        elif defect == "validation_digest":
            validation.tree_digest = "sha256:" + "f" * 64
        elif defect == "source":
            validation.addon_id = "plugin.video.other"
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
    if defect == "session":
        bind(env, mismatch="phase")
    result = tools.workspace_deploy_preflight(principal, operation(env))
    assert result.verdict == "blocked"
    checks = reasons(result)
    assert checks[check].reason == reason
    assert checks["source_baseline"].state == "not_required"
    if defect == "leases":
        assert checks["global_lease"].reason == "global_busy"
    assert not {"attach_source", "copy_source"} & {
        item.action for item in result.next_actions
    }


@pytest.mark.parametrize("historical", [False, True])
def test_owned_setup_exemption_retains_failed_copy_and_reconciliation_evidence(
    tmp_path: Path, historical: bool
) -> None:
    env, _failed_copy = inventory_failure(tmp_path, historical=historical)
    make_owned(env)
    tools, principal, _admin = setup_tools(env)
    counts = vars(env.broker).copy()
    result = tools.workspace_deploy_preflight(principal, operation(env))
    checks = reasons(result)
    assert checks["source_baseline"].state == "not_required"
    if historical:
        assert (
            checks["deployment_provenance"].reason
            == "deployment_provenance_review_required"
        )
        assert checks["deployment_provenance"].state == "unknown"
    assert checks["reconciliation"].state == ("satisfied" if historical else "blocked")
    assert result.verdict == ("unknown" if historical else "blocked")
    assert "review_history" in {item.action for item in result.next_actions}
    assert not {"attach_source", "copy_source"} & {
        item.action for item in result.next_actions
    }
    assert vars(env.broker) == counts

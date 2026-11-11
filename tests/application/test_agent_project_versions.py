"""Private project work does not borrow or rewrite human session selection."""

from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import delete

from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.broker.development_sources import (
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import (
    AgentProjectRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_development_launch import LaunchEnvironment, environment
from tests.application.test_mcp_workspace_development import NOW_TEXT, SOURCE_ID


def project_environment(
    tmp_path: Path,
    selection: Literal["none", "other"] = "none",
) -> LaunchEnvironment:
    e = environment(tmp_path)
    if selection == "other":
        other = e.env.workspaces.queue_creation(SOURCE_ID)
        lease = e.env.workspaces.acquire(other.operation_id)
        assert lease is not None
        e.env.workspaces.run(lease)
        with e.env.sessions.begin() as db:
            current = db.get(DevelopmentSessionRow, 1)
            assert current is not None
            current.workspace_id = str(other.workspace_id)
    e.admin.set_managed_lab_role(e.env.client_id, True)
    with e.env.sessions.begin() as db:
        db.add(
            AgentProjectRow(
                source_id=str(SOURCE_ID),
                client_id=str(e.env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="seeded",
                created_at=NOW_TEXT,
            )
        )
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        assert policy is not None
        if selection == "none":
            policy.active_generation = None
        policy.retain_profiles = True
        policy.retained_omega_target_id = "908822fe-132a-5283-ac40-04e26d387db3"
    return e


@pytest.mark.parametrize("selection", ["none", "other"])
def test_owned_project_launch_without_global_selection_and_exact_replay(
    tmp_path: Path,
    selection: Literal["none", "other"],
) -> None:
    e = project_environment(tmp_path, selection)
    with e.env.sessions() as db:
        current = db.get(DevelopmentSessionRow, 1)
        assert current is not None
        original_workspace = current.workspace_id
    operation = e.request()
    first = e.tools.development_launch(e.principal, operation)
    assert first.dispatch_accepted
    repeated = e.tools.development_launch(e.principal, operation)
    assert repeated.replayed and repeated.launch_id == first.launch_id
    assert e.bridge.calls == [False]
    with e.env.sessions() as db:
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        assert policy is not None
        assert policy.active_generation == (None if selection == "none" else 1)
        current = db.get(DevelopmentSessionRow, 1)
        assert current is not None and current.workspace_id == original_workspace


def test_uncertain_human_session_still_blocks_project_dispatch(tmp_path: Path) -> None:
    e = project_environment(tmp_path)
    with e.env.sessions.begin() as db:
        current = db.get(DevelopmentSessionRow, 1)
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        assert current is not None and policy is not None
        policy.active_generation = 1
        current.phase = "uncertain"
    with pytest.raises(
        McpAuthorizationError, match="development_session_requires_review"
    ):
        e.tools.development_launch(e.principal, e.request())
    assert e.bridge.calls == []


@pytest.mark.parametrize("denial", ["role", "credential", "source"])
def test_project_context_never_substitutes_current_authorization(
    tmp_path: Path, denial: str
) -> None:
    e = project_environment(tmp_path)
    if denial == "role":
        e.admin.set_managed_lab_role(e.env.client_id, False)
    elif denial == "credential":
        e.admin.revoke_credential(e.env.client_id, e.principal.credential_id)
    else:
        e.admin.remove_development_source_grant(e.env.client_id, SOURCE_ID)
    with pytest.raises(McpAuthorizationError):
        e.tools.development_launch(e.principal, e.request())
    assert e.bridge.calls == []


@pytest.mark.parametrize("collision", [False, True])
def test_first_project_deployment_without_web_attachment_or_source_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: bool,
) -> None:
    env = fixtures._environment(tmp_path)
    admin = McpAdministrationService(env.sessions)
    admin.set_managed_lab_role(env.client_id, True)
    admin.grant_development_source(env.client_id, SOURCE_ID)
    with env.sessions.begin() as db:
        db.execute(delete(DevelopmentSourceDeploymentRow))
        db.execute(delete(ManagedInstanceDevelopmentSourceRow))
        db.add(
            AgentProjectRow(
                source_id=str(SOURCE_ID),
                client_id=str(env.client_id),
                request_id=str(uuid4()),
                parameter_fingerprint="a" * 64,
                state="seeded",
                created_at=NOW_TEXT,
            )
        )
        db.add(
            DevelopmentSessionPolicyRow(
                id=1,
                active_generation=None,
                retain_profiles=True,
                retained_omega_target_id="908822fe-132a-5283-ac40-04e26d387db3",
            )
        )
    inventory = env.development._inventory.get
    monkeypatch.setattr(
        env.development._inventory,
        "get",
        lambda target: (
            inventory(target)
            if collision or env.broker.sync_count
            else inventory(target).model_copy(update={"items": []})
        ),
    )
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        env.sessions,
        development_root=None,
        data_root=tmp_path / "data",
    )
    sync = env.broker.copy_sync_mcp_workspace
    resolved = []

    def broker_copy(**kwargs: Any) -> Any:
        spec = resolver.resolve_workspace_deployment(
            kwargs["managed_instance_id"],
            kwargs["workspace_id"],
            kwargs["expected_generation"],
            kwargs["deployment_operation_id"],
            kwargs["expected_container_id"],
        )
        assert spec.prior_evidence is None
        assert spec.development_root == tmp_path / "data/agent-workspaces" / str(
            env.workspace_id
        )
        resolved.append(spec)
        return sync(**kwargs)

    monkeypatch.setattr(env.broker, "copy_sync_mcp_workspace", broker_copy)
    start = env.broker.start_managed_container
    admission = ManagedCapacityAdmission(
        env.sessions,
        lambda: (
            ManagedContainerRecord(
                managed_instance_id=str(fixtures.TARGET_ID),
                container_id=fixtures.CONTAINER_ID,
                image_id=fixtures.IMAGE_ID,
                runtime_revision_id=str(fixtures.RUNTIME_REVISION_ID),
                runtime_state=env.broker.state,
                health_state=ContainerHealthState.NOT_CONFIGURED,
            ),
        ),
        clock=lambda: fixtures.NOW,
    )

    def broker_start(**kwargs: Any) -> StartManagedContainerRecord:
        def dispatch() -> StartManagedContainerRecord:
            start(**kwargs)
            return StartManagedContainerRecord(
                managed_instance_id=str(fixtures.TARGET_ID),
                container_id=fixtures.CONTAINER_ID,
                runtime_state=ContainerRuntimeState.RUNNING,
                start_performed=True,
            )

        return admission.run_start(
            instance_id=str(fixtures.TARGET_ID),
            revision_id=str(fixtures.RUNTIME_REVISION_ID),
            container_id=fixtures.CONTAINER_ID,
            dispatch=dispatch,
        )

    monkeypatch.setattr(env.broker, "start_managed_container", broker_start)
    request_id = uuid4()
    if collision:
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="deployment_not_allowed"
        ):
            env.development.deploy(
                client_id=env.client_id,
                request_id=request_id,
                workspace_id=env.workspace_id,
                expected_generation=0,
                target_id=fixtures.TARGET_ID,
            )
        assert env.broker.stop_count == env.broker.sync_count == 0
    else:
        first = env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
        )
        assert first.state == "succeeded" and len(resolved) == 1
        repeated = env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
        )
        assert repeated.deployment_id == first.deployment_id and len(resolved) == 1
    with env.sessions() as db:
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        assert policy is not None and policy.active_generation is None

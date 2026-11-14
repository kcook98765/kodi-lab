"""Web and both MCP transports share observation-only durable preflight facts."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
)
from kodi_lab.application.workspace_deployment_preflight import (
    WorkspaceDeploymentPreflightService,
)
from tests.web.conftest import INSTANCE_ID, login
from tests.web.test_mcp import _administration, _legacy_post, _post, _strict_tool_result


@pytest.mark.parametrize("authorized", [False, True])
def test_exact_preflight_wire_scope_and_authenticated_web_without_runtime_convergence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, authorized: bool
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    source = context.settings.development_root / "plugin.video.preflight-wire"
    source.mkdir()
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.preflight-wire" name="Preflight" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n"
    )
    (source / "main.py").write_text("pass\n")
    registered = context.development_source_administration.register_source(
        logical_name="Preflight source", relative_locator=source.name
    )
    context.development_source_attachments.attach_source(INSTANCE_ID, registered.id)
    creation = context.agent_workspaces.queue_creation(registered.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Preflight agent", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    admin.grant_development_source(agent.id, registered.id)
    admin.bind_workspace(agent.id, creation.workspace_id)
    admin.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    if authorized:
        admin.grant_target(agent.id, INSTANCE_ID)
    token = admin.issue_credential(agent.id).token

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("preflight cannot converge, schedule or probe runtime")

    monkeypatch.setattr(context.status, "get", forbidden)
    monkeypatch.setattr(context.status, "get_observation", forbidden)
    monkeypatch.setattr(context.readiness, "start", forbidden)
    inspect = WorkspaceDeploymentPreflightService.inspect
    reads: list[Any] = []

    def track(self: Any, operation: Any, **kwargs: Any) -> Any:
        reads.append(operation)
        return inspect(self, operation, **kwargs)

    monkeypatch.setattr(WorkspaceDeploymentPreflightService, "inspect", track)
    arguments = {
        "workspace_id": str(creation.workspace_id),
        "target_id": str(INSTANCE_ID),
        "expected_generation": 0,
    }
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "workspace_deploy_preflight" in catalog
    for post in (_post, _legacy_post):
        result = post(
            client,
            token,
            "tools/call",
            params={"name": "workspace_deploy_preflight", "arguments": arguments},
        ).json()["result"]
        if authorized:
            assert not result.get("isError")
            _strict_tool_result(
                result, catalog["workspace_deploy_preflight"]["outputSchema"]
            )
            content = result["structuredContent"]
            assert (
                content["verdict"] == "blocked"
                and content["assessment_scope"] == "durable_prerequisites"
            )
            assert any(
                item["reason"] == "source_baseline_missing"
                for item in content["checks"]
            )
            assert not content["reservation_created"]
        else:
            assert result["isError"]
            assert "source_baseline" not in result["content"][0]["text"]
    assert len(reads) == 2 * int(authorized)
    path = f"/api/agent-workspaces/{creation.workspace_id}/deployment-preflight"
    query: dict[str, str | int] = {
        "target_id": str(INSTANCE_ID),
        "expected_generation": 0,
    }
    assert client.get(path, params=query).status_code == 401
    login(client)
    response = client.get(path, params=query)
    assert response.status_code == 200
    assert any(
        item["reason"] == "source_baseline_missing"
        for item in response.json()["checks"]
    )
    assert (
        client.get(path, params={**query, "expected_generation": -1}).status_code == 422
    )
    assert str(context.settings.data_root) not in response.text


def test_owned_project_preflight_wire_schema_and_server_derived_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    context.agent_workspaces._development_root = None
    submitted: list[UUID] = []
    monkeypatch.setattr(
        context.local_projects._creation, "_submit_creation", submitted.append
    )
    admin = _administration(client)
    owner = admin.create_client(
        display_name="Owned preflight agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    token = admin.issue_credential(owner.id).token
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    created = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "project_create",
            "arguments": {
                "request_id": str(uuid4()),
                "addon_id": "script.owned.preflight",
                "display_name": "Owned preflight",
            },
        },
    ).json()["result"]
    assert not created.get("isError"), created
    workspace = created["structuredContent"]["workspace"]
    assert submitted == [UUID(workspace["creation_operation_id"])]
    lease = context.agent_workspaces.acquire(UUID(workspace["creation_operation_id"]))
    assert lease is not None
    context.agent_workspaces.run(lease)
    arguments = {
        "workspace_id": workspace["workspace_id"],
        "expected_generation": 0,
    }
    validated = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_validate", "arguments": arguments},
    ).json()["result"]
    assert not validated.get("isError"), validated
    arguments["target_id"] = str(INSTANCE_ID)
    inspect = WorkspaceDeploymentPreflightService.inspect
    callers: list[UUID | None] = []

    def track(self: Any, operation: Any, **kwargs: Any) -> Any:
        callers.append(kwargs.get("client_id"))
        return inspect(self, operation, **kwargs)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("preflight cannot probe or start the native runtime")

    monkeypatch.setattr(WorkspaceDeploymentPreflightService, "inspect", track)
    monkeypatch.setattr(context.status, "get", forbidden)
    monkeypatch.setattr(context.status, "get_observation", forbidden)
    monkeypatch.setattr(context.readiness, "start", forbidden)
    for post in (_post, _legacy_post):
        result = post(
            client,
            token,
            "tools/call",
            params={"name": "workspace_deploy_preflight", "arguments": arguments},
        ).json()["result"]
        assert not result.get("isError"), result
        _strict_tool_result(
            result, catalog["workspace_deploy_preflight"]["outputSchema"]
        )
        content = result["structuredContent"]
        checks = {item["name"]: item for item in content["checks"]}
        assert checks["attachment"]["state"] == "not_required"
        assert checks["source_baseline"]["state"] == "not_required"
        assert checks["native"]["state"] == checks["addon"]["state"] == "unknown"
        assert content["verdict"] == "unknown"
        assert not {"attach_source", "copy_source"} & {
            item["action"] for item in content["next_actions"]
        }
    assert callers == [owner.id, owner.id]
    # Client identity is transport authority, never a caller-selectable argument.
    rejected = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "workspace_deploy_preflight",
            "arguments": {**arguments, "client_id": str(owner.id)},
        },
    ).json()["result"]
    assert rejected["isError"]
    assert callers == [owner.id, owner.id]
    login(client)
    response = client.get(
        f"/api/agent-workspaces/{workspace['workspace_id']}/deployment-preflight",
        params={"target_id": str(INSTANCE_ID), "expected_generation": 0},
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "unknown"
    assert callers == [owner.id, owner.id, None]

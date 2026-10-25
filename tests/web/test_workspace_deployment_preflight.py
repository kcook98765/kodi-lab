"""Web and both MCP transports share observation-only durable preflight facts."""

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
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

    def track(self: Any, operation: Any) -> Any:
        reads.append(operation)
        return inspect(self, operation)

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

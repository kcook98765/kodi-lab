"""Authenticated MCP creates an owned project with no operator source setup."""

from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from tests.web.test_mcp import (
    _administration,
    _assert_tool_error,
    _post,
    _strict_tool_result,
)


def test_wire_project_creation_schema_replay_and_role_revocation(
    client: TestClient,
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    context.agent_workspaces._development_root = None
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Wire project owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    token = admin.issue_credential(agent.id).token
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "project_create" in catalog
    arguments = {
        "request_id": str(uuid4()),
        "addon_id": "script.wire.project",
        "display_name": "Wire project",
    }

    def call(values: dict[str, Any]) -> dict[str, Any]:
        response = _post(
            client,
            token,
            "tools/call",
            params={"name": "project_create", "arguments": values},
        )
        assert response.status_code == 200
        result = cast(dict[str, Any], response.json()["result"])
        _strict_tool_result(result, catalog["project_create"]["outputSchema"])
        return result

    first = call(arguments)
    assert not first.get("isError")
    project = first["structuredContent"]
    second = call(arguments)
    assert second["structuredContent"]["project_id"] == project["project_id"]
    assert (
        second["structuredContent"]["workspace"]["workspace_id"]
        == project["workspace"]["workspace_id"]
    )
    status = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "project_status",
            "arguments": {"project_id": project["project_id"]},
        },
    ).json()["result"]
    _strict_tool_result(status, catalog["project_status"]["outputSchema"])
    assert (
        status["structuredContent"]["projects"][0]["request_id"]
        == arguments["request_id"]
    )
    assert status["structuredContent"]["projects"][0]["workspace_ids"] == [
        project["workspace"]["workspace_id"]
    ]
    actor = admin.get_client(agent.id)
    assert actor.target_grants == ()
    assert actor.development_source_grants == (UUID(project["project_id"]),)
    admin.set_managed_lab_role(agent.id, False)
    catalog_after = {
        item["name"]
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "project_create" not in catalog_after
    _assert_tool_error(call(arguments), "permission_denied")

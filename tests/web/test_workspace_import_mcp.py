"""The public MCP import contract binds preview bytes and returns valid receipts."""

from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from tests.web.test_mcp import _administration, _post, _strict_tool_result


def test_reviewed_import_round_trip_over_mcp(client: TestClient) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    source = context.settings.development_root / "plugin.video.import-wire"
    source.mkdir()
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.import-wire" name="Import" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n"
    )
    (source / "main.py").write_text("old\n")
    registered = context.development_source_administration.register_source(
        logical_name="Wire import source",
        relative_locator=source.name,
    )
    creation = context.agent_workspaces.queue_creation(registered.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    admin = _administration(client)
    preset = admin.create_custom_preset(
        display_name="Wire import",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=frozenset({"workspace.edit", "development_source.inspect"}),
    )
    agent = admin.create_client(display_name="Wire importer", preset_id=preset.id)
    admin.grant_development_source(agent.id, registered.id)
    admin.bind_workspace(agent.id, creation.workspace_id)
    token = admin.issue_credential(agent.id).token
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert {"workspace_import_preview", "workspace_import"} <= catalog.keys()
    request_number = 0
    clock = [100.0]
    cast(Any, context.mcp_transport.development_edit_limiter)._clock = lambda: clock[0]

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal request_number
        request_number += 1
        clock[0] += 1.0
        response = _post(
            client,
            token,
            "tools/call",
            params={"name": name, "arguments": arguments},
            request_id=request_number,
        )
        assert response.status_code == 200
        result = cast(dict[str, Any], response.json()["result"])
        _strict_tool_result(result, catalog[name]["outputSchema"])
        return result

    (source / "main.py").write_text("reviewed\n")
    (source / "helper.py").write_text("helper\n")
    preview_result = call(
        "workspace_import_preview",
        {"workspace_id": str(creation.workspace_id), "expected_generation": 0},
    )
    assert not preview_result.get("isError")
    preview = preview_result["structuredContent"]
    assert preview["eligible"] and len(preview["files"]) == 2
    arguments = {
        "workspace_id": str(creation.workspace_id),
        "expected_generation": 0,
        "request_id": str(uuid4()),
        "source_digest": preview["source_digest"],
        "preview_digest": preview["preview_digest"],
    }
    applied = call("workspace_import", arguments)
    assert not applied.get("isError")
    assert applied["structuredContent"]["generation"] == 1
    assert applied["structuredContent"]["state"] == "succeeded"
    assert (
        call("workspace_import", arguments)["structuredContent"]
        == applied["structuredContent"]
    )
    assert (
        context.agent_workspaces.read_file(creation.workspace_id, "main.py").content
        == "reviewed\n"
    )
    assert (
        context.agent_workspaces.read_file(creation.workspace_id, "helper.py").content
        == "helper\n"
    )
    # No caller path or supplied bytes are part of this authority contract.
    invalid = call(
        "workspace_import_preview",
        {
            "workspace_id": str(creation.workspace_id),
            "expected_generation": 1,
            "host_path": "/tmp/arbitrary",
        },
    )
    assert invalid["isError"]

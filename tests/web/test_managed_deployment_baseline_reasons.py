"""Authorized baseline reasons survive modern and legacy MCP transport."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.mcp.transport import _tool_error_result
from kodi_lab.storage.models import (
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
)
from tests.web.conftest import INSTANCE_ID
from tests.web.test_mcp import _administration, _legacy_post, _make_target_ready, _post


@pytest.mark.parametrize("authorized", [False, True])
def test_missing_baseline_reason_and_next_action_have_authorized_wire_scope(
    client: TestClient, database: sessionmaker[Session], authorized: bool
) -> None:
    _make_target_ready(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    source = context.settings.development_root / "plugin.video.baseline-wire"
    source.mkdir()
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.baseline-wire" name="Baseline" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n"
    )
    (source / "main.py").write_text("pass\n")
    registered = context.development_source_administration.register_source(
        logical_name="Baseline wire source", relative_locator=source.name
    )
    context.development_source_attachments.attach_source(INSTANCE_ID, registered.id)
    creation = context.agent_workspaces.queue_creation(registered.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Baseline diagnostics", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    admin.grant_development_source(agent.id, registered.id)
    admin.bind_workspace(agent.id, creation.workspace_id)
    admin.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    if authorized:
        admin.grant_target(agent.id, INSTANCE_ID)
    token = admin.issue_credential(agent.id).token
    context.mcp_workspace_development.record_validation(
        workspace_id=creation.workspace_id,
        source_id=registered.id,
        generation=0,
        addon_id=registered.addon_id,
        addon_version=registered.observed_version,
        extension_types=registered.extension_types,
        validated_at=registered.validated_at,
    )
    results = []
    for post in (_post, _legacy_post):
        result = post(
            client,
            token,
            "tools/call",
            params={
                "name": "workspace_deploy",
                "arguments": {
                    "workspace_id": str(creation.workspace_id),
                    "expected_generation": 0,
                    "target_id": str(INSTANCE_ID),
                    "request_id": str(uuid4()),
                },
            },
        ).json()["result"]
        assert result["isError"]
        message = result["content"][0]["text"]
        if authorized:
            assert message.startswith("deployment_not_allowed: source_baseline_missing")
            assert (
                "Web administrator" in message and "exact stopped container" in message
            )
        else:
            assert "source_baseline" not in message
            assert "next_action" not in message
        assert str(context.settings.development_root) not in message
        results.append(message)
    assert results[0] == results[1]
    with database() as session:
        assert session.scalar(select(McpWorkspaceDeploymentOperationRow)) is None
        assert session.scalar(select(McpDevelopmentRequestRow)) is None


def test_unrecognized_error_reason_is_not_exposed() -> None:
    result = _tool_error_result(
        "deployment_not_allowed",
        tool_name="workspace_deploy",
        reason="private/path/credential",
    )
    assert cast(Any, result.content[0]).text == "deployment_not_allowed"

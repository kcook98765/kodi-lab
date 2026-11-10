"""Authenticated launch-only MCP and explicit Web permission configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.local_projects import LocalProjectSelection
from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID
from kodi_lab.application.workspace_launch import (
    WorkspaceLaunchResult,
    WorkspaceLaunchState,
)
from kodi_lab.storage.models import DevelopmentSessionPolicyRow, DevelopmentSessionRow
from tests.application.test_development_launch import PERMISSIONS
from tests.application.test_mcp_workspace_development import NOW_TEXT
from tests.web.conftest import (
    CONTAINER_ID,
    INSTANCE_ID,
    REVISION_ID,
    FakeBroker,
    guard_targeted_status_isolation,
    login,
)
from tests.web.test_local_projects import setup_project
from tests.web.test_mcp import _post, _strict_tool_result
from tests.web.test_ui_state_mcp import _inject_transport


def agent(
    client: TestClient, database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, str, dict[str, Any]]:
    context, developer = setup_project(client)
    submitted: list[UUID] = []
    monkeypatch.setattr(
        context.local_projects._creation, "_submit_creation", submitted.append
    )
    project = context.local_projects.onboard(
        LocalProjectSelection(client_id=UUID(developer), project="hub")
    )
    lease = context.agent_workspaces.acquire(submitted[0])
    assert lease is not None
    context.agent_workspaces.run(lease)
    preset = context.mcp_administration.create_custom_preset(
        display_name="Trusted local launch",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=PERMISSIONS,
    )
    context.mcp_administration.assign_client_preset(UUID(developer), preset.id)
    context.mcp_administration.assign_target_preset(INSTANCE_ID, preset.id)
    context.mcp_administration.grant_target(UUID(developer), INSTANCE_ID)
    credential = context.mcp_administration.issue_credential(UUID(developer))
    with database.begin() as db:
        db.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                workspace_id=str(project.workspace_id),
                source_id=str(project.source_id),
                container_id=CONTAINER_ID,
                phase="bound",
                created_at=NOW_TEXT,
                updated_at=NOW_TEXT,
            )
        )
        db.flush()
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        if policy is None:
            db.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        else:
            policy.active_generation = 1
    return (
        context,
        credential.token,
        {
            "request_id": str(uuid4()),
            "workspace_id": str(project.workspace_id),
            "target_id": str(INSTANCE_ID),
            "expected_generation": 0,
            "extension_ordinal": 0,
        },
    )


def call(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = _post(
        client, token, "tools/call", params={"name": name, "arguments": arguments}
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["result"])


def test_launch_wire_schema_and_transaction_recheck(
    client: TestClient, database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    context, token, arguments = agent(client, database, monkeypatch)
    catalog = {
        t["name"]: t
        for t in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "development_launch" in catalog and "workspace_launch" not in catalog
    schema = catalog["development_launch"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "request_id",
        "workspace_id",
        "target_id",
        "expected_generation",
        "extension_ordinal",
    }
    dispatched: list[UUID] = []

    def accept(**kwargs: Any) -> WorkspaceLaunchResult:
        with database() as db:
            kwargs["authorize"](db)
        dispatched.append(kwargs["request_id"])
        return WorkspaceLaunchResult(
            launch_id=uuid4(),
            request_id=kwargs["request_id"],
            workspace_id=kwargs["workspace_id"],
            target_id=kwargs["target_id"],
            generation=kwargs["expected_generation"],
            extension_ordinal=kwargs["extension_ordinal"],
            state=WorkspaceLaunchState.ACCEPTED,
            dispatch_accepted=True,
            replayed=False,
            code=None,
            next_action="inspect_ui",
        )

    monkeypatch.setattr(context.workspace_launch, "launch", accept)
    result = call(client, token, "development_launch", arguments)
    _strict_tool_result(result, catalog["development_launch"]["outputSchema"])
    assert result["structuredContent"]["dispatch_accepted"] and len(dispatched) == 1
    invalid_arguments: tuple[dict[str, Any], ...] = (
        {"command": "Quit()"},
        {"path": "/tmp/addon"},
        {"arguments": []},
        {"extension_ordinal": True},
    )
    for extra in invalid_arguments:
        assert call(client, token, "development_launch", {**arguments, **extra})[
            "isError"
        ]
    with database.begin() as db:
        current = db.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.phase = "uncertain"
    assert call(client, token, "development_launch", arguments)["isError"]
    assert len(dispatched) == 1


def test_supported_configuration_observes_ui_and_screenshots_but_cannot_control(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, token, arguments = agent(client, database, monkeypatch)
    inventory = broker.managed_container_inventory
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    # Screenshot capture uses a complete inventory; the targeted-status guard
    # deliberately forbids that separate path. Restore the fixture inventory.
    monkeypatch.setattr(broker, "managed_container_inventory", inventory)
    transport = _inject_transport(monkeypatch)
    for name in ("target_status", "inspect_ui_state", "screenshot_capture"):
        result = call(client, token, name, {"target_id": str(INSTANCE_ID)})
        assert not result.get("isError"), (name, result)
    assert "Addons.ExecuteAddon" not in transport.methods
    assert call(
        client,
        token,
        "remote_action",
        {"target_id": str(INSTANCE_ID), "request_id": str(uuid4()), "action": "up"},
    )["isError"]
    assert call(
        client, token, "managed_reset", {"version": "omega", "request_id": str(uuid4())}
    )["isError"]
    assert call(client, token, "workspace_launch", arguments)["isError"]
    execution = {k: v for k, v in arguments.items() if k != "extension_ordinal"}
    assert call(client, token, "workspace_execute", execution)["isError"]
    assert not broker.remote_calls
    assert (
        "target.control"
        not in context.mcp_administration.get_client(
            context.local_projects.clients()[0].id
        ).preset.permissions
    )


def test_permission_checkbox_is_explicit_and_only_under_development_ceiling(
    client: TestClient,
) -> None:
    login(client)
    page = client.get("/mcp-clients")
    assert page.status_code == 200
    label = (
        "Launch a deployed manifest entry of the selected managed development project"
    )
    assert label in page.text
    assert 'value="development.launch"' in page.text
    if destination := os.getenv("KODI_LAB_LAUNCH_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "agents.html").write_text(page.text)

"""Authenticated MCP wiring through the existing coordinated remote dispatcher."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID
from kodi_lab.kodi.launch import ManagedLaunchRuntimeBinding
from kodi_lab.kodi.remote import BrowserRemoteAction, RemoteActionAmbiguousError
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ManagedRemoteGuardRow,
    McpRemoteActionReceiptRow,
)
from tests.application.test_development_launch import PERMISSIONS
from tests.web.conftest import (
    CONTAINER_ID,
    INSTANCE_ID,
    REVISION_ID,
    FakeBroker,
    guard_targeted_status_isolation,
    login,
)
from tests.web.test_development_launch import agent, call
from tests.web.test_mcp import _post, _strict_tool_result
from tests.web.test_ui_state_mcp import _inject_transport


def setup(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, str, dict[str, Any], list[str]]:
    context, token, arguments = agent(client, database, monkeypatch)
    developer = context.local_projects.clients()[0].id
    preset = context.mcp_administration.create_custom_preset(
        display_name="Explicit development menus",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=PERMISSIONS | {"development.navigate"},
    )
    context.mcp_administration.assign_client_preset(developer, preset.id)
    context.mcp_administration.assign_target_preset(INSTANCE_ID, preset.id)
    inventory = broker.managed_container_inventory
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    monkeypatch.setattr(broker, "managed_container_inventory", inventory)
    _inject_transport(monkeypatch)
    arguments.pop("extension_ordinal")
    arguments.update(action="down", observe_ui=True)
    deployment_id = str(uuid4())
    phases: list[str] = []

    # Installation/Bridge verification is exercised against actual captured
    # deployments in application tests. Here only that collaborator is doubled;
    # auth, schema, UI collection, receipts, guards, adapter and captures are real.
    def installed(*_args: Any) -> Any:
        with database() as db:
            guard = db.get(ManagedRemoteGuardRow, str(INSTANCE_ID))
            instance = db.get(ManagedInstanceRow, str(INSTANCE_ID))
            assert instance is not None
            phases.append("preflight" if guard is None else guard.state)
            revision = instance.runtime_state_revision
        return (
            SimpleNamespace(
                id=deployment_id,
                addon_id="script.hub",
                addon_version="1.0.0",
                runtime_revision_id=str(REVISION_ID),
                source_manifest_digest="sha256:" + "a" * 64,
                verified_destination_digest="sha256:" + "b" * 64,
            ),
            revision,
            ManagedLaunchRuntimeBinding(
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                container_id=CONTAINER_ID,
                generation=1,
            ),
        )

    monkeypatch.setattr(context.workspace_launch, "navigation_installation", installed)
    return context, token, arguments, phases


def test_navigate_wire_observation_capture_and_replay_under_guard(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context, token, arguments, phases = setup(client, database, broker, monkeypatch)
    tools = {
        tool["name"]: tool
        for tool in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "development_navigate" in tools and "remote_action" not in tools
    schema = tools["development_navigate"]["inputSchema"]
    assert not schema["additionalProperties"]
    assert schema["properties"]["action"]["enum"] == [
        "up",
        "down",
        "left",
        "right",
        "back",
    ]
    first = call(client, token, "development_navigate", arguments)
    assert not first.get("isError"), first
    _strict_tool_result(first, tools["development_navigate"]["outputSchema"])
    assert phases == ["preflight", "reserved"]
    receipt = first["structuredContent"]["action_receipt"]
    assert receipt["request_id"] == arguments["request_id"]
    assert receipt["post_action_observation"]["status"] == "succeeded"
    assert broker.remote_calls == [BrowserRemoteAction.DOWN]
    replay = call(client, token, "development_navigate", arguments)
    assert replay["structuredContent"] == first["structuredContent"]
    assert broker.remote_calls == [BrowserRemoteAction.DOWN] and len(phases) == 2
    capture = receipt["screenshot_refresh"]
    assert capture is not None
    retrieved = call(
        client,
        token,
        "screenshot_retrieve",
        {
            "target_id": str(INSTANCE_ID),
            "request_id": arguments["request_id"],
            "capture_id": capture["capture_id"],
        },
    )
    assert not retrieved.get("isError"), retrieved
    for extra in (
        {"action": "select"},
        {"command": "Quit()"},
        {"text": "secret"},
        {"expected_generation": True},
    ):
        assert call(client, token, "development_navigate", {**arguments, **extra})[
            "isError"
        ]
    assert call(
        client,
        token,
        "managed_reset",
        {"request_id": str(uuid4()), "generation": "omega"},
    )["isError"]


def test_revocation_is_rechecked_inside_existing_dispatch_guard(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, token, arguments, phases = setup(client, database, broker, monkeypatch)
    installed = context.workspace_launch.navigation_installation

    def revoke(*args: Any) -> Any:
        result = installed(*args)
        if phases[-1] == "reserved":
            developer = context.local_projects.clients()[0].id
            context.mcp_administration.remove_target_grant(developer, INSTANCE_ID)
        return result

    monkeypatch.setattr(context.workspace_launch, "navigation_installation", revoke)
    result = call(client, token, "development_navigate", arguments)
    assert result["isError"] and phases == ["preflight", "reserved"]
    assert not broker.remote_calls
    with database() as db:
        row = db.scalar(select(McpRemoteActionReceiptRow))
        assert row is not None and row.state == "completed"
        assert row.final_result == "development_navigation_context_changed"
        assert db.get(ManagedRemoteGuardRow, str(INSTANCE_ID)) is None


def test_ambiguous_navigation_keeps_original_receipt_and_managed_fence(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _context, token, arguments, phases = setup(client, database, broker, monkeypatch)
    broker.remote_error = RemoteActionAmbiguousError("ack lost")
    for _ in range(2):
        result = call(client, token, "development_navigate", arguments)
        assert result["isError"]
    assert broker.remote_calls == [BrowserRemoteAction.DOWN]
    assert phases == ["preflight", "reserved"]
    with database() as db:
        row = db.scalar(select(McpRemoteActionReceiptRow))
        guard = db.get(ManagedRemoteGuardRow, str(INSTANCE_ID))
        assert row is not None and row.state == "ambiguous"
        assert row.request_id == arguments["request_id"]
        assert guard is not None and guard.state == "ambiguous"
    result = call(
        client, token, "development_navigate", {**arguments, "request_id": str(uuid4())}
    )
    assert result["isError"] and broker.remote_calls == [BrowserRemoteAction.DOWN]


def test_web_navigation_permission_requires_explicit_choice(client: TestClient) -> None:
    login(client)
    page = client.get("/mcp-clients")
    assert page.status_code == 200
    assert 'value="development.navigate"' in page.text
    assert "Select, text, settings, dialogs and custom windows are refused" in page.text
    if destination := os.getenv("KODI_LAB_NAVIGATION_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "agents.html").write_text(page.text)


def test_container_change_at_coordinated_dispatch_is_refused(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, token, arguments, phases = setup(client, database, broker, monkeypatch)
    installed = context.workspace_launch.navigation_installation

    def replace_container(*args: Any) -> Any:
        result = installed(*args)
        if phases[-1] == "reserved":
            assert broker.container is not None
            broker.container = broker.container.model_copy(
                update={"container_id": "b" * 64}
            )
        return result

    monkeypatch.setattr(
        context.workspace_launch, "navigation_installation", replace_container
    )
    assert call(client, token, "development_navigate", arguments)["isError"]
    assert not broker.remote_calls
    with database() as db:
        row = db.scalar(select(McpRemoteActionReceiptRow))
        assert row is not None and row.state == "completed"
        assert db.get(ManagedRemoteGuardRow, str(INSTANCE_ID)) is None


def test_project_revocation_after_ack_preserves_action_without_collecting_ui(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, token, arguments, _phases = setup(client, database, broker, monkeypatch)
    from kodi_lab.application.remote_observations import RemoteObservationService

    original = RemoteObservationService.complete

    def revoke_then_complete(
        self: RemoteObservationService, *args: Any, **kwargs: Any
    ) -> Any:
        developer = context.local_projects.clients()[0].id
        from kodi_lab.storage.models import AgentWorkspaceRow

        with database() as db:
            workspace = db.get(AgentWorkspaceRow, arguments["workspace_id"])
            assert workspace is not None
            source_id = UUID(workspace.development_source_id)
        context.mcp_administration.remove_development_source_grant(developer, source_id)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RemoteObservationService, "complete", revoke_then_complete)
    result = call(client, token, "development_navigate", arguments)
    assert not result.get("isError"), result
    receipt = result["structuredContent"]["action_receipt"]
    assert receipt["outcome"] == "succeeded"
    assert receipt["post_action_observation"]["error_code"] == "observation_denied"
    assert broker.remote_calls == [BrowserRemoteAction.DOWN]

"""Public launch schema, exact grants and current credential authorization."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.workspace_launch import (
    WorkspaceLaunchResult,
    WorkspaceLaunchState,
)
from tests.web.conftest import INSTANCE_ID
from tests.web.test_mcp import _administration, _post, _strict_tool_result


@pytest.mark.parametrize("release", ["19.4", "20.5", "21.3"])
def test_direct_launch_wire_authority_and_closed_schema(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    database: sessionmaker[Session],
    release: str,
) -> None:
    from tests.application.test_nexus_reviewed_bridge import set_release

    set_release(database, release)
    context = cast(FastAPI, client.app).state.kodi_lab
    root = context.settings.development_root / "script.launch-wire"
    root.mkdir()
    (root / "addon.xml").write_text(
        '<addon id="script.launch-wire" name="Launch" version="1.0.0">'
        '<extension point="xbmc.python.script" library="main.py"/></addon>'
    )
    (root / "main.py").write_text("pass\n")
    source = context.development_source_administration.register_source(
        logical_name="Launch wire", relative_locator=root.name
    )
    creation = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Launch wire agent", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    admin.grant_development_source(agent.id, source.id)
    admin.bind_workspace(agent.id, creation.workspace_id)
    admin.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    token = admin.issue_credential(agent.id).token
    tools = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    schema = tools["workspace_launch"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "workspace_id",
        "expected_generation",
        "target_id",
        "request_id",
        "extension_ordinal",
    }
    dispatched: list[UUID] = []

    def accept(**kwargs: Any) -> WorkspaceLaunchResult:
        # Transport binding includes the transaction-safe credential/grant recheck.
        with database() as session:
            kwargs["authorize"](session)
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
    arguments = {
        "workspace_id": str(creation.workspace_id),
        "expected_generation": 0,
        "target_id": str(INSTANCE_ID),
        "request_id": str(uuid4()),
        "extension_ordinal": 0,
    }

    def call(body: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _post(
                client,
                token,
                "tools/call",
                params={"name": "workspace_launch", "arguments": body},
            ).json()["result"],
        )

    assert call(arguments)["isError"] and not dispatched
    admin.grant_target(agent.id, INSTANCE_ID)
    result = call(arguments)
    _strict_tool_result(result, tools["workspace_launch"]["outputSchema"])
    assert result["structuredContent"]["dispatch_accepted"]
    assert len(dispatched) == 1
    for invalid in (
        {**arguments, "command": "Quit()"},
        {**arguments, "extension_ordinal": True},
        {**arguments, "extension_ordinal": 64},
    ):
        assert call(invalid)["isError"]
    assert len(dispatched) == 1
    admin.remove_development_source_grant(agent.id, source.id)
    assert call(arguments)["isError"] and len(dispatched) == 1


def test_web_administrator_can_resolve_exact_stopped_launch_without_mcp_grants(
    client: TestClient, database: sessionmaker[Session], broker: Any
) -> None:
    from datetime import UTC, datetime

    from kodi_lab.broker.docker_inventory import (
        ContainerHealthState,
        ContainerRuntimeState,
    )
    from kodi_lab.broker.operations import ManagedContainerInventoryEntry
    from kodi_lab.storage.models import McpWorkspaceLaunchRow
    from tests.web.conftest import CONTAINER_ID, IMAGE_ID, ORIGIN, REVISION_ID, login

    context = cast(FastAPI, client.app).state.kodi_lab
    root = context.settings.development_root / "script.admin-launch"
    root.mkdir()
    (root / "addon.xml").write_text(
        '<addon id="script.admin-launch" name="Admin launch" version="1.0.0">'
        '<extension point="xbmc.python.script" library="main.py"/></addon>'
    )
    (root / "main.py").write_text("pass\n")
    source = context.development_source_administration.register_source(
        logical_name="Admin receipt", relative_locator=root.name
    )
    creation = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    launch_id = uuid4()
    with database.begin() as session:
        session.add(
            McpWorkspaceLaunchRow(
                id=str(launch_id),
                client_id=str(uuid4()),
                request_id=str(uuid4()),
                workspace_id=str(creation.workspace_id),
                development_source_id=str(source.id),
                target_id=str(INSTANCE_ID),
                deployment_id=str(uuid4()),
                generation=0,
                ordinal=0,
                addon_id="script.admin-launch",
                addon_version="1.0.0",
                tree_digest="sha256:" + "a" * 64,
                runtime_revision_id=str(REVISION_ID),
                runtime_state_revision=0,
                bridge_generation=1,
                container_id=CONTAINER_ID,
                state="ambiguous",
                phase="finalize",
                requested_at=datetime.now(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
            )
        )
    broker.container = ManagedContainerInventoryEntry(
        managed_instance_id=str(INSTANCE_ID),
        container_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        runtime_state=ContainerRuntimeState.STOPPED,
        health_state=ContainerHealthState.NOT_CONFIGURED,
    )
    url = f"/api/instances/{INSTANCE_ID}/launches/{launch_id}/reconcile"
    assert client.post(url, json={}).status_code == 401
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(url, json={}, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            url, json={}, headers={**headers, "Origin": "http://other.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/instances/{uuid4()}/launches/{launch_id}/reconcile",
            json={},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        "Recheck exact launch receipt" in client.get(f"/instances/{INSTANCE_ID}").text
    )
    history = client.get(f"/instances/{INSTANCE_ID}/history")
    assert (
        f"activity-{launch_id}" in history.text
        and "Manifest launch ambiguous" in history.text
    )
    for _ in range(2):
        result = client.post(url, json={}, headers=headers)
        assert result.status_code == 200 and result.json()["state"] == "contained"
        assert not result.json()["dispatch_accepted"]
    assert not any(name in {"start", "stop"} for name, _ in broker.calls)

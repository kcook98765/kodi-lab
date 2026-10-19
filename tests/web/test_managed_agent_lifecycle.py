"""Modern MCP exposes lifecycle only under explicit current managed permission."""

from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_mcp import (
    _administration,
    _assert_tool_error,
    _post,
    _strict_tool_result,
)


def _agent(client: TestClient, *, assign_target: bool = True) -> tuple[UUID, str, UUID]:
    admin = _administration(client)
    preset = admin.create_custom_preset(
        display_name="Managed lifecycle fixture " + str(uuid4()),
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=frozenset({"target.inspect", "managed.lifecycle"}),
    )
    actor = admin.create_client(
        display_name="Lifecycle fixture " + str(uuid4()), preset_id=preset.id
    )
    admin.assign_target_preset(
        INSTANCE_ID, preset.id if assign_target else FULL_LAB_CONTROL_PRESET_ID
    )
    admin.grant_target(actor.id, INSTANCE_ID)
    context = cast(FastAPI, client.app).state.kodi_lab
    context.managed_agent_lifecycle._rate = BoundedIntervalRateLimiter(0)
    return actor.id, admin.issue_credential(actor.id).token, preset.id


def _call(
    client: TestClient, token: str, name: str, request_id: UUID, **arguments: Any
) -> dict[str, Any]:
    response = _post(
        client,
        token,
        "tools/call",
        params={
            "name": name,
            "arguments": {
                "target_id": str(INSTANCE_ID),
                "request_id": str(request_id),
                **arguments,
            },
        },
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["result"])


def _provision(client: TestClient) -> None:
    csrf = login(client)
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/provision",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 200
    )


def test_explicit_lifecycle_permission_applies_to_both_client_and_target(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    _provision(client)
    actor_id, token, preset_id = _agent(client, assign_target=False)
    _assert_tool_error(
        _call(client, token, "managed_ensure_ready", uuid4()), "permission_denied"
    )
    assert not any(name == "start" for name, _ in broker.calls)
    _administration(client).assign_target_preset(INSTANCE_ID, preset_id)
    catalog = _post(client, token, "tools/list").json()["result"]["tools"]
    assert {item["name"] for item in catalog} >= {
        "managed_ensure_ready",
        "managed_release",
    }
    assert "remote_action" not in {item["name"] for item in catalog}
    request_id = uuid4()
    first = _call(client, token, "managed_ensure_ready", request_id)
    assert first.get("isError") is not True
    schema = next(
        item["outputSchema"]
        for item in catalog
        if item["name"] == "managed_ensure_ready"
    )
    _strict_tool_result(first, schema)
    original = first["structuredContent"]
    assert original["receipt_state"] in {"not_ready", "ready"}
    replay = _call(client, token, "managed_ensure_ready", request_id)[
        "structuredContent"
    ]
    assert replay["replayed"] and replay["operation_id"] == original["operation_id"]
    assert replay["hold_until"] == original["hold_until"]
    assert sum(name == "start" for name, _ in broker.calls) == 1
    release_id = uuid4()
    released = _call(client, token, "managed_release", release_id)["structuredContent"]
    assert released["receipt_state"] == "released"
    assert sum(name == "stop" for name, _ in broker.calls) == 1
    _call(client, token, "managed_release", release_id)
    old = _call(client, token, "managed_ensure_ready", request_id)["structuredContent"]
    assert not old["ready"] and old["current_state"] == "stopped"
    assert sum(name == "start" for name, _ in broker.calls) == 1
    assert sum(name == "stop" for name, _ in broker.calls) == 1
    _administration(client).remove_target_grant(actor_id, INSTANCE_ID)
    _assert_tool_error(
        _call(client, token, "managed_ensure_ready", request_id),
        "target_not_found_or_not_authorized",
    )


def test_release_preserves_other_clients_current_use(
    client: TestClient, broker: FakeBroker
) -> None:
    _provision(client)
    _, first, _ = _agent(client)
    _, second, _ = _agent(client)
    _call(client, first, "managed_ensure_ready", uuid4())
    _call(client, second, "managed_ensure_ready", uuid4())
    retained = _call(client, first, "managed_release", uuid4())["structuredContent"]
    assert retained["receipt_state"] == "retained" and retained["code"] == "keep_awake"
    assert not any(name == "stop" for name, _ in broker.calls)
    released = _call(client, second, "managed_release", uuid4())["structuredContent"]
    assert released["receipt_state"] == "released"
    assert sum(name == "stop" for name, _ in broker.calls) == 1


def test_ensure_does_not_provision_and_schema_rejects_raw_lifecycle_authority(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    _, token, _ = _agent(client)
    missing = _call(client, token, "managed_ensure_ready", uuid4())["structuredContent"]
    assert (
        missing["receipt_state"] == "failed" and missing["code"] == "provision_required"
    )
    assert not any(name in {"provision", "start"} for name, _ in broker.calls)
    for extra in ({"command": "docker start anything"}, {"hold_minutes": 241}):
        result = _call(client, token, "managed_ensure_ready", uuid4(), **extra)
        assert result["isError"] is True
    assert not any(name == "start" for name, _ in broker.calls)

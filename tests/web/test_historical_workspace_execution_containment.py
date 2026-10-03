"""The historical containment action belongs only to authenticated Web admin."""

from uuid import uuid4

from fastapi.testclient import TestClient

from kodi_lab.mcp.registry import build_mcp_tool_registry

from .conftest import INSTANCE_ID, ORIGIN, login


def test_historical_containment_web_boundary_and_confirmation(
    client: TestClient,
) -> None:
    path = (
        f"/api/instances/{INSTANCE_ID}/workspace-executions/{uuid4()}"
        "/historical-containment"
    )
    body = {
        "request_id": str(uuid4()),
        "expected_runtime_state_revision": 0,
        "confirmed": True,
    }
    assert client.post(path, json=body, headers=ORIGIN).status_code == 401
    csrf = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    unconfirmed = client.post(path, json={**body, "confirmed": False}, headers=headers)
    malformed = client.post(path, json={**body, "extra": True}, headers=headers)
    assert unconfirmed.status_code == 422
    assert malformed.status_code == 422
    rejected = client.post(path, json=body, headers=headers)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "containment_not_allowed"


def test_historical_containment_has_no_mcp_tool() -> None:
    names = set(build_mcp_tool_registry().names())
    assert not any("historical_containment" in name for name in names)

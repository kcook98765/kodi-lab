from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from .conftest import ORIGIN, login


@pytest.mark.parametrize("classification", ["external", "production"])
def test_preference_api_requires_admin_csrf_and_exact_revision(
    client: TestClient, classification: str
) -> None:
    path = f"/api/targets/{uuid4()}/screenshot-preference"
    assert client.get(path).status_code == 401
    assert (
        client.put(path, json={"enabled": False, "expected_revision": 1}).status_code
        == 401
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    target = client.post(
        f"/api/{classification}-targets",
        headers=headers,
        json={
            "logical_name": "Household Kodi",
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.40",
            "port": 8080,
            "authentication_mode": "none",
            "configured_capabilities": ["target_inspect", "screenshot"],
        },
    )
    assert target.status_code == 201
    path = f"/api/targets/{target.json()['target_id']}/screenshot-preference"
    before = client.get(path)
    assert before.status_code == 200
    assert before.json()["enabled"] is True
    assert before.json()["revision"] == 1
    payload = {"enabled": False, "expected_revision": 1}
    assert client.put(path, json=payload).status_code == 403
    assert (
        client.put(
            path, json=payload, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    changed = client.put(path, json=payload, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["enabled"] is False
    assert changed.json()["revision"] == 2
    assert changed.json()["updated_by"] == "web_admin"
    assert client.put(path, json=payload, headers=headers).status_code == 409
    assert (
        client.put(
            path, json={"enabled": "false", "expected_revision": 2}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/targets/{uuid4()}/screenshot-preference").status_code == 404
    )

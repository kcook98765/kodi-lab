import subprocess
from pathlib import Path
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
    target_id = target.json()["target_id"]
    page = client.get(
        f"/kodi/{target_id}" if classification == "production" else "/external-targets"
    )
    assert page.status_code == 200
    assert f'data-screenshot-preference="{target_id}"' in page.text
    assert f'for="screenshots-{target_id}"' in page.text
    assert "/static/screenshot-preference.js" in page.text
    assert "agents authorized to inspect this exact Kodi" in page.text
    unrelated = client.post(
        f"/api/{classification}-targets",
        headers=headers,
        json={
            "logical_name": "Other Kodi",
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.41",
            "port": 8080,
            "authentication_mode": "none",
            "configured_capabilities": ["target_inspect", "screenshot"],
        },
    ).json()["target_id"]
    path = f"/api/targets/{target_id}/screenshot-preference"
    before = client.get(path)
    assert before.status_code == 200
    assert before.json()["enabled"] is False
    assert before.json()["revision"] == 1
    payload = {"enabled": True, "expected_revision": 1}
    assert client.put(path, json=payload).status_code == 403
    assert (
        client.put(
            path, json=payload, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    changed = client.put(path, json=payload, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["enabled"] is True
    assert changed.json()["revision"] == 2
    assert changed.json()["updated_by"] == "web_admin"
    other = client.get(f"/api/targets/{unrelated}/screenshot-preference").json()
    assert other["enabled"] is False and other["revision"] == 1
    assert (
        client.put(
            path, json={"enabled": False, "expected_revision": 2}, headers=headers
        ).json()["enabled"]
        is False
    )
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


def test_preference_controller_loads_without_mutation_and_saves_exact_target() -> None:
    subprocess.run(
        ["node", str(Path(__file__).with_name("screenshot_preference_harness.cjs"))],
        check=True,
        capture_output=True,
        text=True,
    )

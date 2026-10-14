from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE

from .conftest import EXTERNAL_TARGET_ID, ORIGIN, login, seed_external_target


def test_narrow_preset_is_authenticated_csrf_guarded_and_reviewable(
    client: TestClient,
) -> None:
    selected_permissions = [
        "workspace.inspect",
        "workspace.deploy",
        "development.execute",
    ]
    payload = {
        "display_name": "Physical development only",
        "template_preset_id": None,
        "permissions": selected_permissions,
    }
    assert client.get("/mcp-clients").status_code == 401
    csrf = login(client)
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    presets = client.get("/api/mcp/presets").json()
    payload["template_preset_id"] = next(
        item["id"] for item in presets if item["display_name"] == "Add-on Development"
    )
    assert (
        client.post("/api/mcp/presets", headers=ORIGIN, json=payload).status_code == 403
    )
    response = client.post(
        "/api/mcp/presets",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json=payload,
    )
    assert response.status_code == 201
    assert set(response.json()["permissions"]) == set(selected_permissions)
    page = client.get("/mcp-clients")
    assert page.status_code == 200
    assert "Exact permissions to grant" in page.text
    assert "Physical development only" in page.text
    invalid = client.post(
        "/api/mcp/presets",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={**payload, "display_name": "Invalid subset", "permissions": ["unknown"]},
    )
    assert invalid.status_code == 409


def test_development_amendment_route_requires_auth_csrf_and_closed_values(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    seed_external_target(database)
    path = f"/api/external-targets/{EXTERNAL_TARGET_ID}/capability-amendments"
    body = {
        "expected_capabilities": ["target_inspect"],
        "requested_capabilities": ["target_inspect", "unknown"],
        "expected_bridge_id": "23000000-0000-4000-8000-000000000057",
        "expected_bridge_fingerprint": "a" * 64,
        "confirmed": True,
    }
    assert client.post(path, json=body).status_code == 401
    csrf = login(client)
    assert client.post(path, headers=ORIGIN, json=body).status_code == 403
    response = client.post(path, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "capabilities_invalid"
    assert client.get(path).status_code == 200

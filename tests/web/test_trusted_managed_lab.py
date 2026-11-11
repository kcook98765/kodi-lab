"""The administrator deliberately grants/revokes the role through existing APIs."""

from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
)
from tests.web.conftest import ORIGIN, login
from tests.web.test_mcp import _administration


def test_managed_role_requires_admin_csrf_and_reports_effective_authority(
    client: TestClient,
) -> None:
    admin = _administration(client)
    actor = admin.create_client(
        display_name="Trusted coding agent", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    route = f"/api/mcp/clients/{actor.id}/managed-lab-role"
    payload = {"enabled": True}
    assert client.post(route, json=payload).status_code in {401, 403}
    csrf = login(client)
    assert client.post(route, json=payload, headers=ORIGIN).status_code == 403
    response = client.post(
        route, json=payload, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    )
    assert response.status_code == 200
    assert response.json()["managed_lab_role_version"] == 1
    assert "managed.reset" in response.json()["managed_lab_permissions"]
    page = client.get("/mcp-clients")
    assert page.status_code == 200
    assert 'name="trusted_managed_lab"' in page.text
    assert "data-managed-lab-authority" in page.text
    assert "External Kodi permissions remain separate" in page.text
    admin.set_managed_lab_role(actor.id, False)
    assert admin.get_client(actor.id).managed_lab_role_version is None

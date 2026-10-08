from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.storage.models import ExternalProductionPolicyRow
from tests.storage.test_screenshot_preference_migration import _register_household

from .conftest import ORIGIN, login


def test_read_only_access_requires_session_csrf_origin_and_closed_request(
    client: TestClient, database: sessionmaker[Session], tmp_path: Path
) -> None:
    app = cast(FastAPI, client.app)
    admin = cast(McpAdministrationService, app.state.kodi_lab.mcp_administration)
    target_id, _ = _register_household(database, tmp_path)
    agent = admin.create_client(
        display_name="Existing reader", preset_id=PRODUCTION_INSPECTION_PRESET_ID
    )
    admin.issue_credential(agent.id)
    with database() as session:
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        assert policy is not None
        body = {
            "expected_client_authority_revision": agent.authority_revision,
            "expected_target_policy_revision": policy.policy_revision,
            "expected_identity_generation": policy.identity_generation,
            "expected_bridge_trust_revision": None,
        }
    path = f"/api/mcp/clients/{agent.id}/targets/{target_id}/read-only-access"
    assert client.post(path, json=body).status_code == 401
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(
            path, json=body, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            path, json={**body, "scope": "navigate"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, json={**body, "expected_identity_generation": True}, headers=headers
        ).status_code
        == 422
    )
    assert admin.get_client(agent.id).target_grants == ()
    granted = client.post(path, json=body, headers=headers)
    assert granted.status_code == 200
    result = granted.json()
    assert result["inspection_scope"]["scope"] == "inspect"
    assert result["inspection_scope"]["created_by"] == "web_admin"
    assert result["visual_scope"] is None
    assert result["visual_unavailable_reason"] == "client_missing_visual_permission"
    assert admin.get_client(agent.id).target_grants == (target_id,)
    body["expected_target_policy_revision"] = result["target_policy_revision"]
    assert client.post(path, json=body, headers=headers).json() == result
    assert (
        client.post(
            path.replace(str(agent.id), str(uuid4())), json=body, headers=headers
        ).status_code
        == 404
    )


def test_incompatible_existing_agent_gets_clear_bounded_error(
    client: TestClient, database: sessionmaker[Session], tmp_path: Path
) -> None:
    admin = McpAdministrationService(database)
    target_id, _ = _register_household(database, tmp_path)
    agent = admin.create_client(
        display_name="Development-profile reader",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    admin.issue_credential(agent.id)
    with database() as session:
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        assert policy is not None
        body = {
            "expected_client_authority_revision": agent.authority_revision,
            "expected_target_policy_revision": policy.policy_revision,
            "expected_identity_generation": policy.identity_generation,
        }
    csrf = login(client)
    result = client.post(
        f"/api/mcp/clients/{agent.id}/targets/{target_id}/read-only-access",
        json=body,
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "read_only_preset_incompatible"
    assert "incompatible" in result.json()["error"]["message"]
    assert admin.get_client(agent.id).target_grants == ()

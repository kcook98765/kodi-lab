"""Managed setup reuses explicit administration and the current inspect contract."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import McpCredentialRow
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_kodi_views import household as household
from tests.web.test_mcp import _administration, _assert_tool_error, _post


def test_explicit_managed_inspection_setup_and_saved_state_guidance(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    household: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    admin = _administration(client)
    fixtures: dict[str, str] = {}
    tokens: list[str] = []

    def snapshot() -> dict[str, list[str]]:
        with database() as session:
            return {
                table.name: sorted(
                    repr(tuple(row)) for row in session.execute(select(table))
                )
                for table in Base.metadata.sorted_tables
                if table.name != "web_sessions"
            }

    def render(name: str) -> str:
        before, calls = snapshot(), list(broker.calls)
        response = client.get("/mcp-clients")
        assert response.status_code == 200
        assert snapshot() == before
        assert broker.calls == calls
        assert all(token not in response.text for token in tokens)
        fixtures[name] = response.text
        return response.text

    policies = admin.list_target_policies()
    with monkeypatch.context() as patch:
        patch.setattr(
            admin,
            "list_target_policies",
            lambda: [p for p in policies if p.target_class != "managed"],
        )
        assert "No Managed Kodi prepared" in render("no-target")
    assert "No suitable inspect-only custom preset" in render("empty")
    preset = client.post(
        "/api/mcp/presets",
        headers=headers,
        json={
            "display_name": "Existing managed reader",
            "template_preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
            "permissions": ["target.inspect"],
        },
    )
    assert preset.status_code == 201
    preset_id = preset.json()["id"]
    assert preset.json()["permissions"] == ["target.inspect"]
    render("preset-only")
    created = client.post(
        "/api/mcp/clients",
        headers=headers,
        json={
            "display_name": "My existing agent",
            "preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
        },
    )
    assert created.status_code == 201
    client_id = created.json()["id"]
    render("wrong-client-preset")
    assigned = client.post(
        f"/api/mcp/clients/{client_id}/preset",
        headers=headers,
        json={"preset_id": preset_id},
    )
    assert assigned.status_code == 200
    assert assigned.json()["preset"]["permissions"] == ["target.inspect"]
    render("policy-missing")
    # A token plus a grant still cannot bypass an unassigned target policy.
    issued = admin.issue_credential(UUID(client_id))
    tokens.append(issued.token)
    admin.grant_target(UUID(client_id), INSTANCE_ID)

    def inspect() -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _post(
                client,
                issued.token,
                "tools/call",
                params={
                    "name": "target_status",
                    "arguments": {"target_id": str(INSTANCE_ID)},
                },
            ).json()["result"],
        )

    assert inspect()["isError"] is True
    admin.remove_target_grant(UUID(client_id), INSTANCE_ID)
    policy = client.post(
        f"/api/mcp/target-policies/{INSTANCE_ID}",
        headers=headers,
        json={"preset_id": preset_id},
    )
    assert policy.status_code == 200
    render("grant-missing")
    _assert_tool_error(inspect(), "target_not_found_or_not_authorized")
    granted = client.post(
        f"/api/mcp/clients/{client_id}/targets/{INSTANCE_ID}", headers=headers
    )
    assert granted.json()["target_grants"] == [str(INSTANCE_ID)]
    assert inspect()["isError"] is False
    suitable = render("suitable")
    assert 'data-active-credential="true"' in suitable
    # Neither another target nor production is authorized by the shortcut.
    denied = _post(
        client,
        issued.token,
        "tools/call",
        params={"name": "target_status", "arguments": {"target_id": str(household)}},
    ).json()["result"]
    _assert_tool_error(denied, "target_not_found_or_not_authorized")
    names = {
        tool["name"]
        for tool in _post(client, issued.token, "tools/list").json()["result"]["tools"]
    }
    assert names == {
        "target_list",
        "target_status",
        "addon_list",
        "inspect_ui_state",
        "log_read",
        "screenshot_capture",
        "screenshot_retrieve",
    }
    admin.revoke_credential(UUID(client_id), issued.credential.id)
    render("token-missing")
    issued = admin.issue_credential(UUID(client_id))
    tokens.append(issued.token)
    admin.grant_target(UUID(client_id), household)
    render("extra-grant")
    admin.remove_target_grant(UUID(client_id), household)
    admin.set_client_enabled(UUID(client_id), False)
    render("disabled")
    assert _post(client, issued.token, "tools/list").status_code == 401
    admin.set_client_enabled(UUID(client_id), True)
    with database.begin() as session:
        credential = session.get(McpCredentialRow, str(issued.credential.id))
        assert credential is not None
        credential.created_at = "2000-01-01T00:00:00.000000Z"
        credential.expires_at = "2000-01-01T00:00:00.000001Z"
    assert 'data-active-credential="false"' in render("expired")
    assert _post(client, issued.token, "tools/list").status_code == 401
    admin.revoke_credential(UUID(client_id), issued.credential.id)
    assert 'data-active-credential="false"' in render("revoked")
    assert _post(client, issued.token, "tools/list").status_code == 401
    assert len(admin.list_clients()) == 1
    assert len([p for p in admin.list_presets() if not p.builtin]) == 1
    managed_choices = re.search(
        r"<select data-managed-target[^>]*>(.*?)</select>", suitable, re.S
    )
    assert managed_choices is not None
    assert str(household) not in managed_choices.group(1)
    if destination := os.getenv("KODI_MANAGED_INSPECTION_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixtures.json").write_text(
            json.dumps(
                {
                    "pages": fixtures,
                    "client": client_id,
                    "preset": preset_id,
                    "target": str(INSTANCE_ID),
                }
            )
        )


@pytest.mark.parametrize("operation", ["preset", "policy", "grant", "credential"])
def test_shortcut_operations_keep_existing_session_csrf_and_origin_checks(
    client: TestClient, operation: str
) -> None:
    admin = _administration(client)
    agent = admin.create_client(
        display_name="Protected reader", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    path, body = {
        "preset": (
            f"/api/mcp/clients/{agent.id}/preset",
            {"preset_id": str(READ_ONLY_INSPECTION_PRESET_ID)},
        ),
        "policy": (
            f"/api/mcp/target-policies/{INSTANCE_ID}",
            {"preset_id": str(READ_ONLY_INSPECTION_PRESET_ID)},
        ),
        "grant": (f"/api/mcp/clients/{agent.id}/targets/{INSTANCE_ID}", {}),
        "credential": (
            f"/api/mcp/clients/{agent.id}/credentials",
            {"expiration_seconds": 3600},
        ),
    }[operation]
    assert client.post(path, json=body).status_code == 401
    csrf = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path,
            json=body,
            headers={"Origin": "https://evil.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert admin.get_client(agent.id) == agent
    assert admin.get_target_policy(INSTANCE_ID).preset is None

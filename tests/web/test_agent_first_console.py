"""Simple operator paths preserve explicit authority and Advanced compatibility."""

import json
import os
import re
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from tests.web.conftest import ORIGIN, login
from tests.web.test_mcp import _administration


def test_first_managed_agent_uses_existing_basic_preset_and_explicit_role(
    client: TestClient,
) -> None:
    csrf = login(client)
    admin = _administration(client)
    before = admin.list_clients()
    html = client.get("/mcp-clients").text
    ordinary, advanced = html.split('<details id="agent-advanced"', 1)
    assert "<h1>Agents</h1>" in ordinary
    assert "data-managed-inspection" not in ordinary
    assert "data-managed-inspection" in advanced
    group = re.search(
        r'<optgroup label="Managed Kodi development">(.*?)</optgroup>', ordinary, re.S
    )
    assert group is not None
    assert re.findall(r'<option value="([^"]+)"', group.group(1)) == [
        str(READ_ONLY_INSPECTION_PRESET_ID)
    ]
    role = re.search(r'<input[^>]*name="trusted_managed_lab"[^>]*>', ordinary)
    assert role is not None and "checked" not in role.group()
    assert '<option value="" selected>Choose agent access' in ordinary
    assert admin.list_clients() == before
    response = client.post(
        "/api/mcp/clients",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={
            "display_name": "Coding agent",
            "preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
            "trusted_managed_lab": True,
        },
    )
    assert response.status_code == 201
    created = admin.get_client(UUID(response.json()["id"]))
    assert created.managed_lab_role_version == 1
    assert created.target_grants == () and created.development_source_grants == ()
    assert created.preset.id == READ_ONLY_INSPECTION_PRESET_ID


def test_external_landing_preserves_two_independent_device_flows(
    client: TestClient,
) -> None:
    assert client.get("/external-kodi").status_code == 401
    login(client)
    page = client.get("/external-kodi")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Trusted Managed Lab grants no access here" in page.text
    assert 'href="/production-targets"' in page.text
    assert 'href="/external-targets"' in page.text
    assert 'href="/add-kodi?mode=home"' in page.text
    assert 'href="/add-kodi?mode=development"' in page.text
    assert client.post("/external-kodi").status_code == 405


def test_source_and_session_controls_remain_advanced_and_deep_links_work(
    client: TestClient,
) -> None:
    login(client)
    managed = client.get("/managed-kodi").text
    navigation = re.search(r'<nav id="admin-navigation".*?</nav>', managed, re.S)
    assert navigation is not None
    assert re.findall(r'<a href="([^"]+)"', navigation.group()) == [
        "/managed-kodi",
        "/external-kodi",
        "/mcp-clients",
        "/settings",
    ]
    advanced = client.get("/settings").text
    for path in (
        "/development",
        "/development-sources",
        "/agent-workspaces",
        "/development-session",
    ):
        assert f'href="{path}"' in advanced
        assert client.get(path).status_code == 200


def test_export_agent_first_console_browser_fixtures(client: TestClient) -> None:
    login(client)
    pages = {
        path: client.get(path).text
        for path in (
            "/kodi",
            "/managed-kodi",
            "/external-kodi",
            "/mcp-clients",
            "/settings",
        )
    }
    admin = _administration(client)
    actor = admin.create_client(
        display_name="Fixture coding agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    populated = client.get("/mcp-clients").text
    slots = client.get("/api/managed-kodi/slots")
    assert slots.status_code == 200
    assert all(not slot["needs_review"] for slot in slots.json()["slots"])
    if destination := os.getenv("KODI_AGENT_FIRST_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixtures.json").write_text(
            json.dumps(
                {
                    "pages": pages,
                    "agents_populated": populated,
                    "slots": slots.json(),
                    "client_id": str(actor.id),
                    "readonly_preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
                }
            )
        )

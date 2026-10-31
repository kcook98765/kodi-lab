"""Ordinary setup presentation preserves recorded state and explicit authority."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import ExternalObservationState
from kodi_lab.mcp.stdio_proxy import load_configuration
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import ExternalKodiTargetRow
from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE
from tests.web.conftest import INSTANCE_ID, ORIGIN, login
from tests.web.test_kodi_views import household as household


def test_landing_empty_keeps_next_actions_and_advanced_controls(
    client: TestClient,
) -> None:
    assert client.get("/", follow_redirects=False).status_code == 303
    login(client)
    page = client.get("/kodi").text
    assert "No household Kodi added yet" in page
    assert 'href="/add-kodi"' in page
    assert 'href="/mcp-clients">Connect an agent' in page
    assert "Fixture and reset" not in page
    assert '<section id="managed-details"' not in page
    managed = client.get("/managed-instances").text
    assert 'id="instances"' in managed
    assert f'href="/instances/{INSTANCE_ID}/snapshots"' in managed
    assert "Fixture and reset" in managed


@pytest.mark.parametrize("unreachable", [False, True])
def test_home_household_is_recorded_and_network_free(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    monkeypatch: pytest.MonkeyPatch,
    unreachable: bool,
) -> None:
    if unreachable:
        with database.begin() as session:
            row = session.get(ExternalKodiTargetRow, str(household))
            assert row is not None
            row.observation_state = ExternalObservationState.UNREACHABLE
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Landing attempted live Kodi I/O or changed authority")

    for service, names in [
        (context.status, ["list_statuses", "get_observation"]),
        (context.reset_status, ["list_statuses"]),
        (context.reconciliation, ["status"]),
        (context.reconstruction, ["latest_for_instance"]),
        (context.managed_resources, ["status"]),
        (context.production_targets, ["probe", "set_enabled", "confirm"]),
        (
            context.external_bridges,
            ["reverify", "attempt_pairing", "capture_screenshot"],
        ),
    ]:
        for name in names:
            monkeypatch.setattr(service, name, forbidden)
    statements: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        statements.append(statement)

    bind = database.kw["bind"]
    event.listen(bind, "before_cursor_execute", record)
    try:
        response = client.get("/kodi")
        assert client.get("/").status_code == 200
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert response.status_code == 200
    primary = response.text
    assert primary.count('class="kodi-card"') == 1
    assert primary.count('class="current-kodi-card"') == 1
    assert "app.js" not in primary and "managed-resources.js" not in primary
    assert "Living room" in primary
    assert f'href="/kodi/{household}"' in primary
    assert "Live availability is not checked" in primary
    assert (
        'data-status="disconnected"' in primary
        if unreachable
        else 'data-status="connected"' in primary
    )
    if unreachable:
        assert "Temporarily unreachable" in primary
    assert not [
        statement
        for statement in statements
        if re.match(
            r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", statement, re.I
        )
    ]


def test_managed_failure_visible_in_advanced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    service = cast(FastAPI, client.app).state.kodi_lab.status
    status = service.list_statuses()[0].model_copy(
        update={
            "failure_detail": "Runtime check failed",
            "failure_code": "runtime_revision_mismatch",
        }
    )
    monkeypatch.setattr(service, "list_statuses", lambda: [status])
    page = client.get("/managed-instances").text
    primary, managed = page.split('<section id="managed-details"', 1)
    notice = re.search(r'<p id="managed-attention".*?</p>', primary, re.S)
    assert notice is not None
    assert " hidden" not in notice.group()
    assert "Advanced managed registrations need review" in notice.group()
    assert 'href="#instances"' in notice.group()
    assert "Runtime check failed" in managed


def test_agents_requires_deliberate_preset_and_supported_config(
    client: TestClient,
) -> None:
    assert client.get("/mcp-clients").status_code == 401
    csrf = login(client)
    presets = client.get("/api/mcp/presets").json()
    custom = client.post(
        "/api/mcp/presets",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={
            "display_name": "0065 Historical acceptance only",
            "template_preset_id": next(
                p["id"] for p in presets if p["display_name"] == "Read-only Inspection"
            ),
            "permissions": ["target.inspect"],
        },
    )
    assert custom.status_code == 201
    page = client.get("/mcp-clients").text
    assert "<h1>Agents</h1>" in page
    assert page.index("Create or select a client") < page.index("Create custom preset")
    assert '<option value="" selected>Choose a preset' in page
    recommended = re.search(
        r'<optgroup label="Read-only Home Kodi inspection">(.*?)</optgroup>', page, re.S
    )
    assert recommended is not None
    assert re.findall(r'<option value="([^"]+)"', recommended.group(1)) == [
        p["id"]
        for p in presets
        if p["display_name"]
        in ["Production Inspection", "Production Visual Inspection"]
    ]
    assert custom.json()["id"] not in recommended.group(1)
    assert "read-only health, add-on and UI-state inspection" in page
    assert "if you also need screenshots" in page
    configuration = re.search(r'<pre id="agent-config"[^>]*>(.*?)</pre>', page, re.S)
    assert configuration is not None
    template = json.loads(configuration.group(1))
    assert template["command"] == "kodi-lab-mcp-stdio"
    assert template["args"] == []
    supported = load_configuration(template["env"])
    assert supported.endpoint == "http://127.0.0.1:8100/mcp"
    assert supported.token == "PASTE_NEW_TOKEN_HERE"
    port_help = re.search(r'<p id="agent-port-help">(.*?)</p>', page, re.S)
    assert port_help is not None
    assert "configured port is <strong>8100</strong>" in port_help.group(1)
    assert "<strong>18100</strong> for the Docker host publication" in port_help.group(
        1
    )
    assert "LAN Web address is not supported" in page


@pytest.mark.parametrize("view", ["standard", "development"])
def test_agent_configuration_is_a_named_keyboard_region(
    client: TestClient, view: str
) -> None:
    login(client)
    client.cookies.set(ADMIN_VIEW_COOKIE, view)
    page = client.get("/mcp-clients").text
    configuration = re.search(r'<pre id="agent-config"([^>]*)>', page)
    assert configuration is not None
    attributes = configuration.group(1)
    assert 'role="region"' in attributes
    assert 'tabindex="0"' in attributes
    assert 'aria-label="MCP server configuration template"' in attributes


def test_agents_get_preserves_authority_and_never_recovers_token(
    client: TestClient, database: sessionmaker[Session], household: UUID
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    presets = client.get("/api/mcp/presets").json()
    ordinary = client.post(
        "/api/mcp/clients",
        headers=headers,
        json={
            "display_name": "My agent",
            "preset_id": next(
                p["id"] for p in presets if p["display_name"] == "Production Inspection"
            ),
        },
    )
    assert ordinary.status_code == 201
    identity = ordinary.json()["id"]
    credential = client.post(
        f"/api/mcp/clients/{identity}/credentials",
        headers=headers,
        json={"expiration_seconds": 86400},
    )
    assert credential.status_code == 201

    def snapshot() -> dict[str, list[str]]:
        with database() as session:
            return {
                table.name: sorted(
                    repr(tuple(row)) for row in session.execute(select(table))
                )
                for table in Base.metadata.sorted_tables
                if table.name != "web_sessions"
            }

    before = snapshot()
    page = client.get("/mcp-clients").text
    assert snapshot() == before
    assert credential.json()["token"] not in page
    assert 'data-token-result tabindex="-1"' in page
    assert "<code data-token></code>" in page
    assert '<details class="agent-client-details">' in page
    assert '<details data-agent-advanced class="agent-advanced">' in page
    assert 'Client name <input name="display_name" value="My agent"' in page
    assert 'aria-label="Policy for Living room"' in page
    assert f'data-target-id="{household}"' in page
    for action in [
        "issue",
        "rotate",
        "revoke",
        "grant",
        "remove-grant",
        "grant-production-scope",
    ]:
        assert f'data-action="{action}"' in page
    assert "Exact permissions to grant" in page
    assert "Development access and workspace bindings" in page


@pytest.fixture(autouse=True)
def development_admin_view(request: pytest.FixtureRequest) -> None:
    """Original full-administration presentation remains available."""
    if "client" in request.fixturenames:
        client: TestClient = request.getfixturevalue("client")
        client.cookies.set("kodi_lab_admin_view", "development")


@pytest.mark.parametrize("view", ["standard", "development"])
@pytest.mark.parametrize("populated", [False, True])
def test_agents_prioritize_existing_clients(
    client: TestClient, view: str, populated: bool
) -> None:
    csrf = login(client)
    client.cookies.set(ADMIN_VIEW_COOKIE, view)
    if populated:
        preset = next(
            p
            for p in client.get("/api/mcp/presets").json()
            if p["display_name"] == "Production Inspection"
        )
        created = client.post(
            "/api/mcp/clients",
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            json={"display_name": "My agent", "preset_id": preset["id"]},
        )
        assert created.status_code == 201
        issued = client.post(
            f"/api/mcp/clients/{created.json()['id']}/credentials",
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            json={"expiration_seconds": 86400},
        )
        assert issued.status_code == 201
    page = client.get("/mcp-clients").text
    if populated:
        assert page.index('id="agent-clients-heading"') < page.index(
            "data-agent-onboarding"
        )
        assert "<details data-agent-onboarding>" in page
        assert "<summary>Connect another agent</summary>" in page
        assert "My agent" in page
    else:
        assert "<details data-agent-onboarding open>" in page
        assert "<summary>Connect your first agent</summary>" in page
    assert page.count('id="agent-clients-heading"') == 1
    assert page.count('data-token-result tabindex="-1"') == 1
    assert "Trusted LAN HTTP is unencrypted." in page
    if destination := os.getenv("KODI_ADMIN_VIEW_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (
            root / f"agents-{view}-{'populated' if populated else 'empty'}.html"
        ).write_text(page)

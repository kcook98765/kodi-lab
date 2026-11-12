"""Discoverability never supplies presets, grants, scopes or credentials."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sql_select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.metadata import Base
from tests.web.conftest import (
    EXTERNAL_TARGET_ID,
    ORIGIN,
    FakeBroker,
    login,
    seed_external_target,
)
from tests.web.test_kodi_views import household as household
from tests.web.test_preview_action_consent import approval, preview_flow


class Selects(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.details: list[dict[str, str | None]] = []
        self.selects: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "details":
            self.details.append(attributes)
        if tag == "select":
            self.current = {
                "attributes": attributes,
                "advanced": any(
                    "data-agent-advanced" in item
                    or item.get("class") == "agent-development-details"
                    for item in self.details
                ),
                "options": [],
            }
            self.selects.append(self.current)
        if tag == "option" and self.current is not None:
            self.current["options"].append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self.details.pop()
        if tag == "select":
            self.current = None


@pytest.mark.parametrize("cookie", [None, "standard", "development", "invalid"])
def test_visible_agent_controls_preserve_every_stored_authority_row(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    cookie: str | None,
) -> None:
    seed_external_target(database)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    presets = client.get("/api/mcp/presets").json()
    inspection = next(
        p for p in presets if p["display_name"] == "Production Inspection"
    )
    created = client.post(
        "/api/mcp/clients",
        headers=headers,
        json={
            "display_name": "Existing inspection agent",
            "preset_id": inspection["id"],
        },
    )
    assert created.status_code == 201
    client_id = created.json()["id"]
    assert (
        client.post(
            f"/api/mcp/clients/{client_id}/targets/{household}", headers=headers
        ).status_code
        == 200
    )
    credential = client.post(
        f"/api/mcp/clients/{client_id}/credentials",
        headers=headers,
        json={"expiration_seconds": 3600},
    )
    assert credential.status_code == 201
    existing = client.get("/api/mcp/clients").json()
    if cookie is not None:
        client.cookies.set("kodi_lab_admin_view", cookie)

    def snapshot() -> dict[str, list[str]]:
        with database() as session:
            return {
                table.name: sorted(
                    repr(tuple(row)) for row in session.execute(sql_select(table))
                )
                for table in Base.metadata.sorted_tables
                if table.name != "web_sessions"
            }

    before = snapshot()
    response = client.get("/mcp-clients")
    assert response.status_code == 200 and snapshot() == before
    assert client.get("/api/mcp/clients").json() == existing
    assert credential.json()["token"] not in response.text
    assert f'data-target-id="{household}"' in response.text
    assert f'data-target-id="{EXTERNAL_TARGET_ID}"' in response.text
    assert "deliberately grant Trusted Managed Lab once" in response.text
    parser = Selects()
    parser.feed(response.text)
    for control in parser.selects:
        attributes = control["attributes"]
        selected = [o["value"] for o in control["options"] if "selected" in o]
        if "data-client-preset" in attributes:
            assert selected == [inspection["id"]] and control["advanced"]
        elif attributes.get("name") in {"preset_id", "template_preset_id"}:
            assert selected == [""]
            if not control["advanced"]:
                assert {o["value"] for o in control["options"] if o["value"]} == {
                    p["id"]
                    for p in presets
                    if p["display_name"]
                    in {"Production Inspection", "Read-only Inspection"}
                }
        elif attributes.get("aria-label", "").startswith("Policy for "):
            assert selected == [""] and control["advanced"]
    advanced = response.text.split('<details id="agent-advanced"', 1)[1]
    for preset in presets:
        if preset["assignable"] and preset["display_name"] not in {
            "Production Inspection",
            "Production Visual Inspection",
        }:
            assert f'value="{preset["id"]}"' in advanced


@pytest.mark.parametrize("cookie", [None, "standard", "development", "invalid"])
def test_discoverable_presets_do_not_bypass_production_or_custom_ceilings(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    cookie: str | None,
) -> None:
    seed_external_target(database)
    csrf = login(client)
    if cookie is not None:
        client.cookies.set("kodi_lab_admin_view", cookie)
    presets = {p["display_name"]: p for p in client.get("/api/mcp/presets").json()}
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for target, preset in [
        (household, presets["Full Lab Control"]),
        (EXTERNAL_TARGET_ID, presets["Production Inspection"]),
    ]:
        path = f"/api/mcp/target-policies/{target}"
        body = {"preset_id": preset["id"]}
        assert client.post(path, headers=ORIGIN, json=body).status_code == 403
        assert client.post(path, headers=headers, json=body).status_code == 409
    assert (
        client.post(
            "/api/mcp/presets",
            headers=headers,
            json={
                "display_name": "Over ceiling",
                "template_preset_id": presets["Read-only Inspection"]["id"],
                "permissions": ["workspace.deploy"],
            },
        ).status_code
        == 409
    )
    assert client.get("/api/mcp/clients").json() == []


@pytest.mark.parametrize("cookie", [None, "invalid"])
@pytest.mark.parametrize("action", ["install", "create"])
def test_absent_or_invalid_view_cookie_never_supplies_preview_consent(
    client: TestClient, broker: FakeBroker, cookie: str | None, action: str
) -> None:
    flow = preview_flow(broker)
    if cookie is not None:
        client.cookies.set("kodi_lab_admin_view", cookie)
    path = "/api/runtime-installations/piers" + (
        "/instances" if action == "create" else ""
    )
    body = approval(action)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 401
    csrf = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(path, json={}, headers=headers).status_code == 422
    assert (
        client.post(path, json={**body, "approved": False}, headers=headers).status_code
        == 422
    )
    assert not flow.launches
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == (202 if action == "install" else 409)
    assert client.post(path, json={}, headers=headers).status_code == 422

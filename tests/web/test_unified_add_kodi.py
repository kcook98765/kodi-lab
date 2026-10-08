"""Unified entry composes existing workflows without adding backend authority."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_onboarding import ExternalKodiOnboardingResult
from kodi_lab.application.household_onboarding import (
    HouseholdOnboardingResult,
    HouseholdStartRequest,
)
from tests.storage.test_screenshot_preference_migration import _register_household
from tests.web.conftest import ORIGIN, login


@pytest.mark.parametrize("mode", ["home", "development"])
def test_unified_entry_modes_are_authenticated_and_isolated(
    client: TestClient, mode: str
) -> None:
    path = f"/add-kodi?mode={mode}"
    assert client.get(path).status_code == 401
    login(client)
    page = client.get(path)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in page.headers["content-security-policy"]
    assert "How will you use this Kodi?" in page.text
    assert f'name="kodi_use" value="{mode}" checked' in page.text
    assert f'data-initial-mode="{mode}"' in page.text
    assert page.text.count('name="kodi_use"') == 2
    assert "Home / everyday Kodi" in page.text and "Development Kodi" in page.text
    assert "data-add-kodi" in page.text and "data-pair-kodi" in page.text
    assert "data-pair-gate disabled" in page.text
    assert "data-js-gate disabled" in page.text
    assert "data-development-recovery" in page.text
    assert "data-manual-recovery" in page.text
    assert "data-consent" in page.text
    for script in ["add-kodi-mode.js", "add-kodi.js", "pair-kodi.js"]:
        assert page.text.count(script) == 1
    assert client.post(path).status_code == 405


def test_default_home_and_development_navigation(client: TestClient) -> None:
    login(client)
    page = client.get("/add-kodi").text
    assert 'name="kodi_use" value="home" checked' in page
    assert client.get("/add-kodi?mode=managed").status_code == 422
    assert 'href="/add-kodi"' in client.get("/kodi").text
    assert 'href="/add-kodi?mode=development"' in client.get("/development").text
    assert 'href="/add-kodi?mode=development"' in client.get("/external-targets").text
    assert 'href="/pair-kodi">Pair Kodi' not in client.get("/development").text
    legacy = client.get("/pair-kodi")
    assert legacy.status_code == 200
    assert "<h1>Pair Kodi</h1>" in legacy.text
    assert "How will you use this Kodi?" not in legacy.text
    assert "data-unified-mode" not in legacy.text


def test_existing_api_routing_has_no_mode_or_permission_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    seen: list[str] = []
    operation_id = uuid4()

    def home(payload: HouseholdStartRequest) -> HouseholdOnboardingResult:
        seen.append("home")
        return HouseholdOnboardingResult(
            operation_id=payload.operation_id,
            state="incomplete",
            code="persistence_failed",
            message="Synthetic incomplete result.",
        )

    def development(**_payload: object) -> ExternalKodiOnboardingResult:
        seen.append("development")
        return ExternalKodiOnboardingResult(
            target_id=uuid4(),
            target_name="Development fixture",
            kodi_version="21.3",
            bridge_id=uuid4(),
            bridge_version="1.1.13",
            bridge_fingerprint="a" * 64,
        )

    monkeypatch.setattr(context.household_onboarding, "start", home)
    monkeypatch.setattr(context.external_onboarding, "onboard", development)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    body = {"name": "Fixture", "address": "192.168.50.40", "code": "2345"}
    assert (
        client.post(
            "/api/household-onboarding",
            headers=headers,
            json={**body, "operation_id": str(operation_id)},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/external-targets/onboard-otp", headers=headers, json=body
        ).status_code
        == 201
    )
    assert seen == ["home", "development"]
    routes: list[tuple[str, dict[str, str]]] = [
        ("/api/household-onboarding", {"operation_id": str(operation_id)}),
        ("/api/external-targets/onboard-otp", {}),
    ]
    overrides: list[dict[str, object]] = [
        {"mode": "development"},
        {"target_class": "external_production"},
        {"grant_deployment": True},
    ]
    for path, extra in routes:
        assert client.post(path, json={**body, **extra}).status_code == 403
        for field in overrides:
            assert (
                client.post(
                    path, headers=headers, json={**body, **extra, **field}
                ).status_code
                == 422
            )
    assert seen == ["home", "development"]


def test_development_resume_cannot_reinterpret_a_production_target(
    client: TestClient, database: sessionmaker[Session], tmp_path: Path
) -> None:
    target_id, _ = _register_household(database, tmp_path)
    csrf = login(client)
    writes: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: object,
    ) -> None:
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I):
            writes.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.post(
            f"/api/external-targets/onboarding/{target_id}/resume",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "target_persistence_failed"
    assert not writes
    target = client.get(f"/api/production-targets/{target_id}").json()
    assert target["target_class"] == "external_production"
    assert target["administrative_state"] == "enabled"
    # A target UUID also cannot act as a durable household operation identifier.
    response = client.post(
        f"/api/household-onboarding/{UUID(str(target_id))}/resume",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.json()["code"] == "operation_not_found"

"""Home gives conservative next steps without changing lifecycle authority."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.web.presentation import managed_next_step, state_label
from tests.web.conftest import INSTANCE_ID, login


def test_landing_has_one_summary_and_advanced_keeps_details(client: TestClient) -> None:
    login(client)
    primary = client.get("/kodi").text
    details = client.get("/managed-instances").text
    assert primary.count('class="current-kodi-card"') == 1
    assert "No development Kodi is selected" in primary
    assert 'href="/development-session"' in primary
    assert f'href="/instances/{INSTANCE_ID}"' not in primary
    assert "Advanced status and runtime details" in details
    assert "Advanced fixture and exact runtime pin" in details
    assert 'data-action="deprovision"' not in details
    assert "Review lifecycle controls and runtime removal" in details
    assert 'data-action="start"' not in details
    assert 'href="/managed-resources"' in details
    assert 'href="/settings">Advanced</a>' in primary
    assert 'class="current-kodi-card"' not in details
    assert 'class="kodi-card"' not in details


@pytest.mark.parametrize(
    ("changes", "blocking", "busy", "expected"),
    [
        ({"allowed_actions": ["start"]}, False, False, "review Start"),
        ({"allowed_actions": ["provision"]}, False, False, "review Set up"),
        ({"allowed_actions": ["start"]}, True, False, "do not repeat"),
        ({"allowed_actions": ["start"]}, False, True, "in progress"),
        (
            {"container_state": "unknown", "allowed_actions": ["start"]},
            False,
            False,
            "uncertain",
        ),
        ({"readiness_state": "checking"}, False, False, "being checked"),
        ({"failure_code": "deployment_ambiguous"}, False, False, "original operation"),
        ({"failure_code": "new_error_code"}, False, False, "technical details"),
        ({"usability_state": "usable", "allowed_actions": ["stop"]}, False, False, ""),
    ],
)
def test_guidance_uses_observed_state_and_respects_uncertainty(
    client: TestClient,
    changes: dict[str, object],
    blocking: bool,
    busy: bool,
    expected: str,
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    instance = context.status.list_statuses()[0].model_copy(update=changes)
    result = managed_next_step(instance, blocking=blocking, busy=busy)
    assert expected in result if expected else result == ""
    if (
        blocking
        or busy
        or instance.failure_code
        or instance.container_state == "unknown"
    ):
        assert "review Start" not in result
        assert "review Set up" not in result


def test_unknown_states_remain_uncertain_and_keep_code() -> None:
    assert state_label("not_checked") == "Not checked yet"
    assert state_label("future_state") == "Unrecognized state (future_state)"
    assert state_label("not_usable") != "Stopped"


def test_home_failure_guidance_does_not_offer_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    instance = context.status.list_statuses()[0].model_copy(
        update={
            "failure_code": "deployment_ambiguous",
            "failure_detail": "Retained failure code",
            "allowed_actions": [],
        }
    )
    monkeypatch.setattr(context.status, "list_statuses", lambda: [instance])
    page = client.get("/managed-instances").text
    primary, details = page.split('<section id="managed-details"', 1)
    assert "Advanced managed registrations need review" in primary
    assert "Retained failure code" not in primary
    assert "Retained failure code" in details


def test_stored_reset_failure_is_counted_and_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    reset = context.reset_status.list_statuses()[0].model_copy(
        update={"failure_reason": "Retained reset failure"}
    )
    monkeypatch.setattr(context.reset_status, "list_statuses", lambda: [reset])
    primary = client.get("/managed-instances").text.split(
        '<section id="managed-details"', 1
    )[0]
    assert "Advanced managed registrations need review" in primary
    assert "Retained reset failure" not in primary


def test_advanced_destinations_available_in_both_views(client: TestClient) -> None:
    login(client)
    for mode in ("standard", "development"):
        client.cookies.set("kodi_lab_admin_view", mode)
        page = client.get("/settings").text
        for path in (
            "/development",
            "/managed-resources",
            "/snapshots/preserved",
            "/history/instances",
        ):
            assert f'href="{path}"' in page
        assert "Production mutation emergency control" in page


def test_export_isolated_home_browser_fixtures(client: TestClient) -> None:
    """Optional browser evidence uses real templates and exclusively fixture APIs."""
    sign_in = client.get("/login").text
    login(client)
    routes = {
        path: client.get(path).text
        for path in ("/", "/kodi", "/managed-instances", "/settings", "/development")
    }
    routes["/login"] = sign_in
    assert "Current Kodi" in routes["/kodi"]
    if destination := os.getenv("KODI_LAB_CAMPAIGN_BROWSER_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "home-routes.json").write_text(json.dumps(routes))

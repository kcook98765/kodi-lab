"""Exact Preview admission through existing authenticated runtime controls."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.runtime_installations import preview_acknowledgement
from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.broker.operations import ReviewedInstallationLaunchResult
from kodi_lab.kodi.ui_state import UiObservation, WindowState
from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE

from .conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from .test_runtime_installations import Flow


def test_preview_card_requires_development_mode(
    client: TestClient, broker: FakeBroker
) -> None:
    Flow().attach(broker)
    login(client)
    assert "Kodi 22 Piers RC1" not in client.get("/runtimes").text
    assert "piers" not in [
        item["generation"]
        for item in client.get("/api/runtime-installations").json()["installations"]
    ]
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    page = client.get("/runtimes").text
    assert "Kodi 22 Piers RC1" in page
    assert "Preview / Prerelease" in page
    assert "Verified upstream Flathub binary" in page
    assert "prerelease development software" in page
    preview = client.get("/api/runtime-installations").json()["installations"][-1]
    assert preview["generation"] == "piers"
    assert preview["action"] == "install"
    assert preview["preview_acknowledgement"] == preview_acknowledgement()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release", "22.0-rc.2"),
        ("release", "22.0"),
        ("recipe_revision", 2),
        ("recipe_revision", True),
        ("recipe_digest", "sha256:" + "f" * 64),
        ("acknowledgement", "all-prereleases"),
        ("unknown", "value"),
    ],
)
def test_exact_acknowledgement_rejects_substitution(
    client: TestClient, broker: FakeBroker, field: str, value: object
) -> None:
    flow = Flow()
    flow.attach(broker)
    token = login(client)
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    ack = {**preview_acknowledgement(), field: value}
    result = client.post(
        "/api/runtime-installations/piers",
        json=ack,
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": token},
    )
    assert result.status_code == 422
    assert not flow.launches


def test_preview_install_admission_and_polling(
    client: TestClient, broker: FakeBroker
) -> None:
    flow = Flow()
    flow.attach(broker)
    dynamic: Any = broker

    def launch(**kwargs: Any) -> ReviewedInstallationLaunchResult:
        flow.operation_id = kwargs["operation_id"]
        flow.generation = kwargs["generation"]
        assert (
            kwargs["preview_acknowledgement"].model_dump() == preview_acknowledgement()
        )
        flow.launches.append((flow.generation, flow.operation_id))
        return ReviewedInstallationLaunchResult(
            operation_id=flow.operation_id, generation="piers", newly_accepted=True
        )

    dynamic.launch_reviewed_runtime_installation = launch
    assert client.post("/api/runtime-installations/piers").status_code == 401
    token = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    ack = preview_acknowledgement()
    assert (
        client.post(
            "/api/runtime-installations/piers", json=ack, headers=headers
        ).status_code
        == 404
    )
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    assert (
        client.post(
            "/api/runtime-installations/piers", json=ack, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/runtime-installations/piers", json={}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/runtime-installations/piers/instances", json=ack, headers=headers
        ).status_code
        == 409
    )
    admitted = client.post(
        "/api/runtime-installations/piers", json=ack, headers=headers
    )
    assert admitted.status_code == 202
    assert admitted.json()["status"] == "pending"
    assert len(flow.launches) == 1
    polled = client.get(admitted.json()["status_url"])
    assert polled.json()["generation"] == "piers"
    assert polled.json()["action"] == "none"


def test_managed_ui_inspection_is_authenticated_and_read_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"/api/instances/{INSTANCE_ID}/ui-state"
    assert client.get(url).status_code == 401
    observed = []

    def inspect(_self: ManagedUiObservationService, target: object) -> UiObservation:
        observed.append(target)
        return UiObservation(window=WindowState(id=10004, label="Settings"))

    monkeypatch.setattr(ManagedUiObservationService, "inspect", inspect)
    login(client)
    result = client.get(url)
    assert result.status_code == 200
    assert result.json()["window"] == {"id": 10004, "label": "Settings"}
    assert observed == [INSTANCE_ID]


def test_clean_profile_reset_does_not_claim_beta_fixture_approval(
    client: TestClient,
) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}/reset")
    assert page.status_code == 200
    assert "No fixture is assigned" in page.text
    assert "creates a clean profile" in page.text
    assert "The assigned fixture will be reapplied" not in page.text

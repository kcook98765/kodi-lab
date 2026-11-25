"""Runtime guidance uses durable backend status, with normal mutation admission."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_runtime_installations import Flow, seed_installation


def test_preview_guidance_distinguishes_installation_from_profile_start(
    client: TestClient, broker: FakeBroker
) -> None:
    Flow().attach(broker)
    login(client)
    for cookie in (None, "standard", "development", "invalid"):
        if cookie is not None:
            client.cookies.set(ADMIN_VIEW_COOKIE, cookie)
        page = client.get("/runtimes")
        assert page.status_code == 200
        text = page.text
        assert (
            "5\u201315 minutes" in text and "two cold-installation measurements" in text
        )
        assert "Prepare its working profile, then start Kodi." in text
        assert "It does not start your managed Kodi or grant an agent access." in text
        assert "You can leave and return later" in text


def test_status_exposes_existing_durable_timestamps(
    client: TestClient, broker: FakeBroker, database: sessionmaker[Session]
) -> None:
    Flow().attach(broker)
    login(client)
    seed_installation(database, "omega")
    item = next(
        x
        for x in client.get("/api/runtime-installations").json()["installations"]
        if x["generation"] == "omega"
    )
    assert item["status"] == "failed" and item["action"] == "retry"
    assert item["created_at"] and item["phase_started_at"] and item["completed_at"]
    assert item["failure_message"]


def test_retired_manual_managed_bridge_route_cannot_dispatch(
    client: TestClient, broker: FakeBroker
) -> None:
    token = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/reviewed-bridge",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": token},
    )
    assert response.status_code == 404
    assert not any("bridge" in name for name, _ in broker.calls)

"""Runtime guidance uses durable backend status, with normal mutation admission."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_runtime_installations import Flow, seed_installation


def test_rc1_guidance_distinguishes_installation_from_instances(
    client: TestClient, broker: FakeBroker
) -> None:
    Flow().attach(broker)
    login(client)
    assert "5\u201315 minutes" not in client.get("/runtimes").text
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    text = client.get("/runtimes").text
    assert "5\u201315 minutes" in text and "two cold-installation measurements" in text
    assert "Additional managed instances use the installed runtime image" in text
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


def test_reviewed_bridge_admission_requires_session_and_csrf(
    client: TestClient,
) -> None:
    url = f"/api/instances/{INSTANCE_ID}/reviewed-bridge"
    assert client.post(url, json={}).status_code == 401
    login(client)
    assert client.post(url, json={}, headers=ORIGIN).status_code == 403
    offered = client.get(url)
    assert offered.status_code == 200  # explicit reviewed upgrade now supports Omega
    assert offered.json()["addon_version"] == "1.1.25"


def test_reviewed_bridge_requires_an_existing_running_target(
    client: TestClient, broker: FakeBroker
) -> None:
    token = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/reviewed-bridge",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": token},
    )
    assert response.status_code == 409
    assert not any("bridge" in name for name, _ in broker.calls)

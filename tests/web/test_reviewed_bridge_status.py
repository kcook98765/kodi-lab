"""Bridge status UI keeps preparation, trust and capability evidence distinct."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import ManagedBridgeTrustRow
from tests.web.conftest import INSTANCE_ID, REVISION_ID, login


def test_reviewed_bridge_exposes_explicit_status_refresh(client: TestClient) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert "data-reviewed-bridge-install disabled" in page
    assert "data-reviewed-bridge-refresh>Refresh status" in page
    assert "data-reviewed-bridge-status" in page


@pytest.mark.parametrize("reviewed", [None, False, True])
def test_status_preserves_boolean_continuation_flag_through_web_serialization(
    client: TestClient, database: sessionmaker[Session], reviewed: bool | None
) -> None:
    if reviewed is not None:
        with database.begin() as session:
            session.add(
                ManagedBridgeTrustRow(
                    target_id=str(INSTANCE_ID),
                    runtime_revision_id=str(REVISION_ID),
                    container_id="a" * 64,
                    bridge_port=8765,
                    secret_reference="00000000-0000-4000-8000-000000000301",
                    state="prepared",
                    generation=4,
                    prepared_at="2026-10-26T10:00:00.000000Z",
                    reviewed_installation=reviewed,
                )
            )
    login(client)
    response = client.get(f"/api/instances/{INSTANCE_ID}/reviewed-bridge")
    assert response.status_code == 200
    result = response.json()
    # The browser deliberately requires === true before offering continuation.
    assert result["resume_available"] is (reviewed is True)
    assert result["generation"] == (0 if reviewed is None else 4)


def test_reviewed_bridge_browser_bounds_and_uncertain_request_safety() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("reviewed_bridge_browser.js")),
            str(root / "src/kodi_lab/web/static/reviewed-bridge.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_reviewed_bridge_continuation_requires_auth_csrf_and_generation(
    client: TestClient,
) -> None:
    from tests.web.conftest import ORIGIN

    path = f"/api/instances/{INSTANCE_ID}/reviewed-bridge/resume"
    assert (
        client.post(path, json={"expected_generation": 1}, headers=ORIGIN).status_code
        == 401
    )
    csrf = login(client)
    assert (
        client.post(path, json={"expected_generation": 1}, headers=ORIGIN).status_code
        == 403
    )
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(path, json={}, headers=headers).status_code == 422
    assert (
        client.post(path, json={"expected_generation": 0}, headers=headers).status_code
        == 422
    )
    assert (
        client.post(
            path, json={"expected_generation": 1, "force": True}, headers=headers
        ).status_code
        == 422
    )
    # No prepared reviewed binding exists; continuation cannot install implicitly.
    assert (
        client.post(path, json={"expected_generation": 1}, headers=headers).status_code
        == 409
    )

"""Bridge status UI keeps preparation, trust and capability evidence distinct."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.conftest import INSTANCE_ID, login


def test_reviewed_bridge_exposes_explicit_status_refresh(client: TestClient) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert "data-reviewed-bridge-install disabled" in page
    assert "data-reviewed-bridge-refresh>Refresh status" in page
    assert "data-reviewed-bridge-status" in page


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

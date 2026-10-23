import base64
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.screenshots import ManagedScreenshotService
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.domain import RuntimeSupportTier
from kodi_lab.kodi.screenshots import PNG_SIGNATURE
from kodi_lab.storage.models import (
    ManagedScreenshotRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
)

from .conftest import DEFINITION_ID, INSTANCE_ID, ORIGIN, REVISION_ID, FakeBroker, login


def make_visual_ready(client: TestClient, csrf: str) -> None:
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    provision = client.post(
        f"/api/instances/{INSTANCE_ID}/provision", json={}, headers=headers
    )
    assert provision.status_code == 200
    start = client.post(f"/api/instances/{INSTANCE_ID}/start", json={}, headers=headers)
    assert start.status_code == 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/instances/{INSTANCE_ID}/visual-status")
        if status.json()["screenshot_available_now"]:
            return
        time.sleep(0.01)
    raise AssertionError("visual status did not become ready")


def test_visual_capability_rejects_nearby_unvalidated_profile(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    with database.begin() as session:
        definition = session.get(RuntimeDefinitionRow, str(DEFINITION_ID))
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert definition is not None and profile is not None
        definition.kodi_version = "22.0-beta.1"
        definition.support_tier = RuntimeSupportTier.PREVIEW
        profile.expected_kodi_version = "22.0"
        profile.expected_version_tag = "beta"
        profile.expected_prerelease_ordinal = 1

    login(client)
    response = client.get(f"/api/instances/{INSTANCE_ID}/visual-status")
    assert response.status_code == 200
    assert response.json()["screenshot_supported"] is False


def test_visual_routes_require_authenticated_admin_and_capture_requires_csrf(
    client: TestClient,
) -> None:
    visual = f"/api/instances/{INSTANCE_ID}/visual-status"
    capture = f"/api/instances/{INSTANCE_ID}/screenshots"
    latest = f"/api/instances/{INSTANCE_ID}/screenshots/latest"
    assert client.get(visual).status_code == 401
    assert client.post(capture, json={}).status_code == 401
    assert client.get(latest).status_code == 401

    login(client)
    assert client.post(capture, json={}).status_code == 403
    assert (
        client.post(capture, json={}, headers={"Origin": "http://evil"}).status_code
        == 403
    )


def test_capture_latest_headers_replacement_and_failed_refresh_retention(
    client: TestClient,
    broker: FakeBroker,
    data_root: Path,
    database: sessionmaker[Session],
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    capture_url = f"/api/instances/{INSTANCE_ID}/screenshots"

    first = client.post(capture_url, json={}, headers=headers)
    assert first.status_code == 200
    first_document = first.json()
    first_capture = first_document["capture_id"]
    assert first_document["screenshot"]["latest_capture_available"] is True
    assert first_document["screenshot"]["width"] == 1280
    assert first_document["screenshot"]["height"] == 720
    captured_at = first_document["screenshot"]["captured_at"]

    latest = client.get(f"/api/instances/{INSTANCE_ID}/screenshots/latest")
    assert latest.status_code == 200
    assert latest.content.startswith(PNG_SIGNATURE)
    assert latest.headers["content-type"] == "image/png"
    assert latest.headers["cache-control"] == "no-store"
    assert latest.headers["x-content-type-options"] == "nosniff"
    assert str(data_root) not in latest.text

    second = client.post(capture_url, json={}, headers=headers)
    assert second.status_code == 200
    second_capture = second.json()["capture_id"]
    assert second_capture != first_capture
    cache = data_root / "screenshots" / "instances" / str(INSTANCE_ID)
    assert sorted(item.name for item in cache.iterdir()) == [f"{second_capture}.png"]

    retained_at = second.json()["screenshot"]["captured_at"]
    broker.screenshot_error = BrokerUnavailableError("injected private failure")
    failed = client.post(capture_url, json={}, headers=headers)
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "screenshot_capture_unavailable"
    status = client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()
    assert status["latest_capture_available"] is True
    assert status["capture_id"] == second_capture
    assert status["captured_at"] == retained_at
    assert status["last_error_code"] == "screenshot_capture_unavailable"
    assert captured_at != retained_at or first_capture != second_capture
    assert client.get(
        f"/api/instances/{INSTANCE_ID}/screenshots/latest"
    ).content.startswith(PNG_SIGNATURE)

    context = cast(Any, client.app).state.kodi_lab
    restarted = ManagedScreenshotService(
        database,
        broker,
        context.status,
        context.reconciliation,
        data_root=data_root,
    )
    assert restarted.get_status(INSTANCE_ID).capture_id is not None


def test_missing_cache_file_is_safe_and_not_recreated(
    client: TestClient,
    data_root: Path,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    captured = client.post(
        f"/api/instances/{INSTANCE_ID}/screenshots", json={}, headers=headers
    ).json()
    path = (
        data_root
        / "screenshots"
        / "instances"
        / str(INSTANCE_ID)
        / f"{captured['capture_id']}.png"
    )
    path.unlink()

    status = client.get(f"/api/instances/{INSTANCE_ID}/visual-status")
    assert status.status_code == 200
    assert status.json()["latest_capture_available"] is False
    latest = client.get(f"/api/instances/{INSTANCE_ID}/screenshots/latest")
    assert latest.status_code == 503
    assert latest.json()["error"]["code"] == "screenshot_capture_unavailable"
    assert not path.exists()


def test_screen_preview_has_one_refreshable_capture_summary(client: TestClient) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    empty_page = client.get(f"/instances/{INSTANCE_ID}").text
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    capture_url = f"/api/instances/{INSTANCE_ID}/screenshots"
    first = client.post(capture_url, json={}, headers=headers)
    assert first.status_code == 200
    screenshot = first.json()["screenshot"]
    cached_page = client.get(f"/instances/{INSTANCE_ID}").text
    timestamp = str(datetime.fromisoformat(screenshot["captured_at"]))
    # A second, page-load-only copy can contradict the refreshed image/metadata.
    assert cached_page.count(timestamp) == 1
    assert cached_page.count(f"{screenshot['width']}\u00d7{screenshot['height']}") == 1
    assert cached_page.count(f"{screenshot['byte_size']} bytes") == 1
    assert "data-screenshot-metadata" in cached_page

    if destination := os.getenv("KODI_LAB_SCREENSHOT_BROWSER_EVIDENCE"):
        second = client.post(capture_url, json={}, headers=headers)
        assert second.status_code == 200
        png = client.get(f"{capture_url}/latest")
        assert png.status_code == 200
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "screenshot-fixture.json").write_text(
            json.dumps(
                {
                    "target": str(INSTANCE_ID),
                    "pages": {"empty": empty_page, "cached": cached_page},
                    "previous_timestamp": timestamp,
                    "capture": second.json(),
                    "png": base64.b64encode(png.content).decode("ascii"),
                }
            )
        )


def test_instance_detail_presents_manual_screenshot_without_remote_controls(
    client: TestClient,
) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert page.status_code == 200
    assert "Screen preview" in page.text
    assert "data-preview-pause" in page.text
    assert "data-preview-interval" in page.text
    assert "Live display" not in page.text
    assert "Refresh screenshot" in page.text
    assert "managed-screenshot.js" in page.text
    assert "noVNC" not in page.text
    assert "Browser remote" not in page.text
    assert "Send text" not in page.text


def test_instance_deletion_removes_screenshot_bytes_and_metadata(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    captured = client.post(
        f"/api/instances/{INSTANCE_ID}/screenshots", json={}, headers=headers
    )
    assert captured.status_code == 200
    cache = data_root / "screenshots" / "instances" / str(INSTANCE_ID)
    assert cache.is_dir()

    queued = client.post(
        f"/api/instances/{INSTANCE_ID}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers=headers,
    )
    assert queued.status_code == 202
    operation_id = queued.json()["operation_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/api/instance-delete-operations/{operation_id}").json()
        if status["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert status["state"] == "succeeded"
    assert not cache.exists()
    with database() as session:
        assert session.get(ManagedScreenshotRow, str(INSTANCE_ID)) is None

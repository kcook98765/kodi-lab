from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from itertools import pairwise
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from kodi_lab.application.live_display import (
    LIVE_DISPLAY_REGISTRY,
    LOGGER,
    LiveDisplayError,
    LiveDisplaySessionRegistry,
    ManagedLiveDisplayService,
)
from kodi_lab.application.screenshots import ScreenshotServiceError
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.storage.models import ManagedScreenshotRow, RuntimeExecutionProfileRow

from .conftest import INSTANCE_ID, ORIGIN, REVISION_ID, FakeBroker, login
from .test_managed_screenshots import make_visual_ready

JPEG_SIGNATURE = b"\xff\xd8"
DISPLAY_URL = f"/api/instances/{INSTANCE_ID}/display"
WS_HEADERS = {"Origin": "http://127.0.0.1", "Host": "127.0.0.1"}


def authenticated_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("kodi_lab_session")
    assert token is not None
    return {**WS_HEADERS, "Cookie": f"kodi_lab_session={token}"}


def enable(client: TestClient) -> None:
    csrf = (
        client.get("/")
        .text.split('name="kodi-lab-csrf" content="', 1)[1]
        .split('"', 1)[0]
    )
    attempt_id = str(uuid4())
    cast(Any, client).preview_attempt_id = attempt_id
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/display-policy",
        json={"enabled": True, "attempt_id": attempt_id},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 200, response.text


class FrameSocket:
    def __init__(self, socket: Any) -> None:
        self.socket = socket

    def receive_bytes(self) -> bytes:
        metadata = self.socket.receive_json()
        assert metadata["v"] == 1 and metadata["mime"] == "image/jpeg"
        data = self.socket.receive_bytes()
        self.socket.send_json({"v": 1, "ack": metadata["seq"], "decode_ms": 1})
        return cast(bytes, data)

    def send_text(self, data: str) -> None:
        self.socket.send_text(data)

    def close(self) -> None:
        self.socket.close()


@contextmanager
def connect(client: TestClient, *, prepare: bool = True) -> Iterator[FrameSocket]:
    if prepare:
        enable(client)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        yield FrameSocket(socket)


def assert_denied(client: TestClient, expected_code: int, **kwargs: Any) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as raised,
        client.websocket_connect(DISPLAY_URL, **kwargs),
    ):
        pass
    assert raised.value.code == expected_code


def test_display_capability_is_exact_profile_and_distinct_from_availability(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    csrf = login(client)
    initial = client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()
    assert initial["display_supported"] is True
    assert initial["display_available_now"] is False
    assert initial["display_backend"] == "live_screenshot_stream"
    make_visual_ready(client, csrf)
    ready = client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()
    assert ready["display_supported"] is True
    assert ready["display_available_now"] is True

    with database.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert profile is not None
        profile.expected_kodi_version = "21.4"
    unsupported = client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()
    assert unsupported["screenshot_supported"] is False
    assert unsupported["display_supported"] is False
    assert unsupported["display_available_now"] is False
    assert unsupported["display_backend"] is None
    assert_denied(client, 4409, headers=authenticated_headers(client))


def test_websocket_requires_auth_exact_origin_and_no_query(client: TestClient) -> None:
    assert_denied(client, 4401, headers=dict(WS_HEADERS))
    login(client)
    authenticated = authenticated_headers(client)
    authenticated.pop("Origin")
    assert_denied(client, 4403, headers=authenticated)
    assert_denied(
        client,
        4403,
        headers={**authenticated_headers(client), "Origin": "http://evil"},
    )
    with (
        pytest.raises(WebSocketDisconnect) as raised,
        client.websocket_connect(
            f"{DISPLAY_URL}?token=forbidden", headers=authenticated_headers(client)
        ),
    ):
        pass
    assert raised.value.code == 4403
    untrusted = authenticated_headers(client)
    untrusted["Host"] = "evil.invalid"
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        client.websocket_connect(DISPLAY_URL, headers=untrusted),
    ):
        pass
    assert denied.value.status_code == 400
    missing = "/api/instances/00000000-0000-4000-8000-000000000999/display"
    with (
        pytest.raises(WebSocketDisconnect) as not_found,
        client.websocket_connect(missing, headers=authenticated_headers(client)),
    ):
        pass
    assert not_found.value.reason == "managed_instance_not_found"


def test_binary_frames_are_valid_ephemeral_jpeg_and_do_not_churn_cache(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    durable = client.post(
        f"/api/instances/{INSTANCE_ID}/screenshots", json={}, headers=headers
    ).json()["capture_id"]
    with database() as session:
        before = session.get(ManagedScreenshotRow, str(INSTANCE_ID))
        assert before is not None
        before_attempt = before.last_attempt_at

    started = time.monotonic()
    with connect(client) as websocket:
        first = websocket.receive_bytes()
        first_at = time.monotonic()
        second = websocket.receive_bytes()
        second_at = time.monotonic()
        websocket.close()
    assert first.startswith(JPEG_SIGNATURE)
    assert second.startswith(JPEG_SIGNATURE)
    with Image.open(BytesIO(first)) as image:
        assert image.format == "JPEG" and image.size == (640, 360)
    assert first_at >= started
    assert second_at - first_at >= 0.8

    with database() as session:
        after = session.get(ManagedScreenshotRow, str(INSTANCE_ID))
        assert after is not None
        assert after.capture_id == durable
        assert after.last_attempt_at == before_attempt
    cache = data_root / "screenshots" / "instances" / str(INSTANCE_ID)
    assert [item.name for item in cache.iterdir()] == [f"{durable}.png"]
    staging = data_root / "screenshots" / "staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_client_messages_are_protocol_violations_and_slot_is_released(
    client: TestClient,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        websocket.send_text('{"key":"left"}')
        with pytest.raises(WebSocketDisconnect) as raised:
            websocket.receive_bytes()
        assert raised.value.code == 1008
        assert raised.value.reason == "display_protocol_error"
    with connect(client) as reopened:
        assert reopened.receive_bytes().startswith(JPEG_SIGNATURE)


def test_same_instance_conflict_and_failed_handshake_do_not_consume_slot(
    client: TestClient,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    with connect(client) as first:
        assert first.receive_bytes().startswith(JPEG_SIGNATURE)
        assert_denied(client, 4409, headers=authenticated_headers(client))
    with connect(client) as reopened:
        assert reopened.receive_bytes().startswith(JPEG_SIGNATURE)


def test_accept_failure_releases_instance_and_global_capacity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    original_accept = WebSocket.accept
    accept_attempted = threading.Event()
    audit_events: list[tuple[str, dict[str, Any]]] = []

    def record_audit(message: str, *, extra: dict[str, Any]) -> None:
        audit_events.append((message, extra))

    async def failed_accept(_websocket: WebSocket, **_kwargs: Any) -> None:
        assert LIVE_DISPLAY_REGISTRY.active_count() == 1
        accept_attempted.set()
        raise RuntimeError("injected websocket accept failure")

    monkeypatch.setattr(LOGGER, "info", record_audit)
    monkeypatch.setattr(WebSocket, "accept", failed_accept)
    with (
        pytest.raises(WebSocketDisconnect),
        connect(client),
    ):
        pass

    assert accept_attempted.is_set()
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0
    assert not any(
        message == "managed live display started" for message, _ in audit_events
    )
    ended = [
        extra
        for message, extra in audit_events
        if message == "managed live display ended"
    ]
    assert len(ended) == 1
    assert ended[0]["established"] is False

    monkeypatch.setattr(WebSocket, "accept", original_accept)
    with connect(client) as reopened:
        assert reopened.receive_bytes().startswith(JPEG_SIGNATURE)
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_logout_terminates_stream_before_another_frame(client: TestClient) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        logged_out = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers=ORIGIN,
            follow_redirects=False,
        )
        assert logged_out.status_code == 303
        with pytest.raises(WebSocketDisconnect) as raised:
            websocket.receive_bytes()
        assert raised.value.reason == "display_session_expired"


def test_lifecycle_cancels_display_and_proceeds(client: TestClient) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        stopped = client.post(
            f"/api/instances/{INSTANCE_ID}/stop", json={}, headers=headers
        )
        assert stopped.status_code == 200
        with pytest.raises(WebSocketDisconnect) as raised:
            websocket.receive_bytes()
        assert raised.value.reason in {
            "display_unavailable",
            "display_instance_not_running",
        }


def test_backend_failure_removes_staging_and_releases_slot(
    client: TestClient,
    broker: FakeBroker,
    data_root: Path,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    broker.screenshot_error = BrokerUnavailableError("private injected detail")
    with connect(client, prepare=False) as websocket:
        with pytest.raises(WebSocketDisconnect) as raised:
            websocket.receive_bytes()
        assert raised.value.reason == "display_backend_failed"
    staging = data_root / "screenshots" / "staging"
    assert not staging.exists() or list(staging.iterdir()) == []
    broker.screenshot_error = None
    with connect(client) as reopened:
        assert reopened.receive_bytes().startswith(JPEG_SIGNATURE)


def test_next_capture_waits_for_previous_send_with_zero_frame_queue(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    send_started = threading.Event()
    release_send = threading.Event()
    original_send = WebSocket.send_bytes

    async def controlled_send(websocket: WebSocket, data: bytes) -> None:
        send_started.set()
        await asyncio.to_thread(release_send.wait)
        await original_send(websocket, data)

    monkeypatch.setattr(WebSocket, "send_bytes", controlled_send)
    with connect(client) as websocket:
        assert send_started.wait(timeout=2.0)
        assert broker.screenshot_calls == 2
        release_send.set()
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        assert broker.screenshot_calls == 3


def test_send_timeout_closes_safely_and_releases_capacity(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)

    async def blocked_send(_websocket: WebSocket, _data: bytes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(WebSocket, "send_bytes", blocked_send)
    with connect(client) as websocket:
        with pytest.raises(WebSocketDisconnect) as raised:
            websocket.receive_bytes()
        assert raised.value.reason == "display_backend_failed"
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_remote_http_action_remains_separate_while_display_is_open(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        remote = client.post(
            f"/api/instances/{INSTANCE_ID}/remote-actions",
            json={"action": "left"},
            headers=headers,
        )
        assert remote.status_code == 200
        assert remote.json()["screenshot_refresh"]["result"] == "succeeded"
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
    assert [action.value for action in broker.remote_calls] == ["left"]


def test_transient_durable_capture_contention_skips_without_closing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    original = context.screenshots.capture_preview
    calls = 0

    def contend_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ScreenshotServiceError(
                "screenshot_capture_in_progress",
                "A durable capture owns this interval.",
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(context.screenshots, "capture_preview", contend_once)
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
    assert calls == 4


def test_capture_slower_than_interval_never_creates_catch_up_frames(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    original = broker.capture_managed_kodi_screenshot
    capture_starts: list[float] = []

    def slow_capture(**kwargs: Any) -> Any:
        capture_starts.append(time.monotonic())
        time.sleep(1.05)
        return original(**kwargs)

    monkeypatch.setattr(broker, "capture_managed_kodi_screenshot", slow_capture)
    with connect(client) as websocket:
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
        assert websocket.receive_bytes().startswith(JPEG_SIGNATURE)
    assert len(capture_starts) == 4
    assert all(later - earlier >= 1.0 for earlier, later in pairwise(capture_starts))


def test_hard_lifetime_and_identity_are_revalidated_without_sleep(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    now = 100.0
    registry = LiveDisplaySessionRegistry()
    service = ManagedLiveDisplayService(
        broker,
        context.status,
        context.reconciliation,
        context.screenshots,
        registry=registry,
        monotonic=lambda: now,
    )
    service.enable(INSTANCE_ID, "owner")
    active = service.begin(INSTANCE_ID, "owner")
    now += 900.0
    with pytest.raises(LiveDisplayError) as raised:
        service.capture(active)
    assert raised.value.code == "display_session_expired"
    service.end(active, raised.value.code)
    assert registry.active_count() == 0

    service.enable(INSTANCE_ID, "owner")
    identity_session = service.begin(INSTANCE_ID, "owner")
    assert broker.container is not None
    broker.container = broker.container.model_copy(update={"container_id": "d" * 64})
    with pytest.raises(LiveDisplayError) as identity_changed:
        service.capture(identity_session)
    assert identity_changed.value.code == "display_unavailable"
    service.end(identity_session, identity_changed.value.code)


def test_active_stream_terminates_when_reconciliation_becomes_blocking(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    registry = LiveDisplaySessionRegistry()
    service = ManagedLiveDisplayService(
        broker,
        context.status,
        context.reconciliation,
        context.screenshots,
        registry=registry,
    )
    service.enable(INSTANCE_ID, "owner")
    active = service.begin(INSTANCE_ID, "owner")
    blocking_status = context.reconciliation.status(INSTANCE_ID).model_copy(
        update={"required": True, "blocking": True}
    )
    monkeypatch.setattr(
        context.reconciliation,
        "status",
        lambda _instance_id: blocking_status,
    )
    with pytest.raises(LiveDisplayError) as raised:
        service.capture(active)
    assert raised.value.code == "display_unavailable"
    service.end(active, raised.value.code)
    assert registry.active_count() == 0


def test_ui_is_view_only_and_keeps_semantic_remote_visible(client: TestClient) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert "Live Preview" in page.text
    assert "Screenshot" in page.text
    assert "Browser remote" in page.text
    assert "live-display.js" in page.text
    assert "noVNC" not in page.text
    assert "keyboard" not in page.text.lower()
    assert "pointer" not in page.text.lower()

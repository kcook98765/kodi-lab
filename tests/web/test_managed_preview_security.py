"""Explicit policy, decode backpressure and real worker cleanup at the Web boundary."""

import asyncio
import threading
import time
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect

from kodi_lab.application.live_display import (
    LIVE_DISPLAY_REGISTRY,
    LiveDisplayError,
    LiveDisplayFrameSkipped,
)
from kodi_lab.storage.models import RuntimeExecutionProfileRow
from kodi_lab.web.live_preview import preview_owner
from tests.web.conftest import INSTANCE_ID, ORIGIN, REVISION_ID, FakeBroker, login
from tests.web.test_live_display import DISPLAY_URL, authenticated_headers, enable
from tests.web.test_managed_screenshots import make_visual_ready


def test_contended_slot_recovers_without_waiting_full_adaptive_frame_period(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    display = cast(Any, client.app).state.kodi_lab.live_display
    original_begin, original_capture = display.begin, display.capture
    denied, delivered = threading.Event(), threading.Event()
    attempts: list[float] = []

    def begin(*args: Any, **kwargs: Any) -> Any:
        active = original_begin(*args, **kwargs)
        active.period_seconds = 5.0
        return active

    def capture(*args: Any, **kwargs: Any) -> Any:
        attempts.append(time.monotonic())
        try:
            frame = original_capture(*args, **kwargs)
        except LiveDisplayFrameSkipped:
            denied.set()
            raise
        delivered.set()
        return frame

    monkeypatch.setattr(display, "begin", begin)
    monkeypatch.setattr(display, "capture", capture)
    coordinator = display._screenshots._coordinator
    coordinator.acquire(INSTANCE_ID)
    held = True
    try:
        with client.websocket_connect(
            DISPLAY_URL, headers=authenticated_headers(client)
        ) as socket:
            assert denied.wait(1)
            assert broker.screenshot_calls == 1  # Only the initial native probe.
            coordinator.release(INSTANCE_ID)
            held = False
            assert delivered.wait(1.5), "Released slot waited the full five seconds"
            assert socket.receive_json()["skipped_frames"] >= 1
            socket.receive_bytes()
            assert len(attempts) == 2
            assert 0.2 <= attempts[1] - attempts[0] < 1.5
            # No new capture before a decoded ACK, even after contention.
            time.sleep(0.3)
            assert len(attempts) == 2 and broker.screenshot_calls == 2
    finally:
        if held:
            coordinator.release(INSTANCE_ID)
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_default_disabled_policy_and_csrf_never_capture(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(DISPLAY_URL, headers=authenticated_headers(client)),
    ):
        pass
    route = f"/api/instances/{INSTANCE_ID}/display-policy"
    assert client.post(route, json={"enabled": True}).status_code == 403
    assert (
        client.post(
            route,
            json={"enabled": True},
            headers={"Origin": "http://evil", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            route,
            json={"enabled": True, "python": "anything"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )
    assert broker.screenshot_calls == 0 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_one_frame_and_fresh_delivery_stay_within_inventory_round_trip_budget(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    display = cast(Any, client.app).state.kodi_lab.live_display
    active = display.begin(INSTANCE_ID, preview_owner(csrf))
    inventory_calls: list[dict[str, Any]] = []
    original = broker.managed_container_inventory

    def inventory(**kwargs: Any) -> Any:
        inventory_calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(broker, "managed_container_inventory", inventory)
    try:
        frame = display.capture(active)
        display.validate_delivery(active)
        assert frame.jpeg.startswith(b"\xff\xd8")
        assert broker.screenshot_calls == 2  # Native probe plus this one frame.
        # Exact capture/post-read identity and full final delivery checks remain;
        # repeating full eligibility around them added six costly broker RPCs.
        assert len(inventory_calls) <= 6
    finally:
        display.end(active, "test_complete")
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_capability_change_after_metadata_refuses_completed_frame_pixels(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    entered, release = threading.Event(), threading.Event()
    original = WebSocket.send_json

    async def metadata(websocket: WebSocket, value: Any, **kwargs: Any) -> None:
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        await original(websocket, value, **kwargs)

    monkeypatch.setattr(WebSocket, "send_json", metadata)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert entered.wait(2)
        with database.begin() as session:
            profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
            assert profile is not None
            profile.expected_kodi_version = "21.4"
        release.set()
        assert socket.receive_json()["seq"] == 1
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()
    assert broker.screenshot_calls == 2 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_no_next_capture_until_browser_decodes_and_acks(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        metadata = socket.receive_json()
        socket.receive_bytes()
        assert (
            metadata["seq"] == 1 and broker.screenshot_calls == 2
        )  # native probe + one live frame
        time.sleep(0.3)
        assert broker.screenshot_calls == 2
        socket.send_json({"v": 1, "ack": 1, "decode_ms": 300})
        second = socket.receive_json()
        socket.receive_bytes()
        assert second["seq"] == 2 and broker.screenshot_calls == 3


def test_ack_timeout_closes_without_another_capture(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kodi_lab.web.live_preview.ACK_TIMEOUT_SECONDS", 0.15)
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        socket.receive_json()
        socket.receive_bytes()
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.reason == "display_ack_timeout"
    assert broker.screenshot_calls == 2 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_revocation_while_waiting_for_ack_stops_delivery(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        socket.receive_json()
        socket.receive_bytes()
        response = client.post(
            f"/api/instances/{INSTANCE_ID}/display-policy",
            json={"enabled": False, "attempt_id": cast(Any, client).preview_attempt_id},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 200
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    assert broker.screenshot_calls == 2 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_stop_during_native_probe_never_commits_a_late_grant(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    started = threading.Event()
    release = threading.Event()
    original = context.screenshots.capture_preview
    errors = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(context.screenshots, "capture_preview", blocked)

    def work() -> None:
        try:
            context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
        except LiveDisplayError as error:
            errors.append(error.code)

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(2)
    context.live_display.disable(INSTANCE_ID)
    assert LIVE_DISPLAY_REGISTRY.active_count() == 1
    with pytest.raises(LiveDisplayError, match="cleanup"):
        context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == ["display_policy_denied"]
    with pytest.raises(LiveDisplayError):
        context.live_display.begin(INSTANCE_ID, preview_owner(csrf))
    assert broker.screenshot_calls == 1 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_cancelled_stream_retains_capacity_until_actual_capture_thread_retires(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    context = cast(Any, client.app).state.kodi_lab
    original = context.live_display.capture
    started = threading.Event()
    release = threading.Event()

    def blocked(*args: Any, **kwargs: Any) -> Any:
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(context.live_display, "capture", blocked)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert started.wait(2)
        context.live_display.disable(INSTANCE_ID)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
        assert LIVE_DISPLAY_REGISTRY.active_count() == 1
        with pytest.raises(LiveDisplayError, match="cleanup"):
            context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
        release.set()
        deadline = time.monotonic() + 2
        while LIVE_DISPLAY_REGISTRY.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert LIVE_DISPLAY_REGISTRY.active_count() == 0
    assert (
        broker.screenshot_calls == 1
    )  # cancelled worker never reached another broker capture


@pytest.mark.parametrize("revocation", ["stop", "logout", "expiry", "disconnect"])
def test_revocation_cancels_blocked_pixel_sender_before_it_can_resume(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    context = cast(Any, client.app).state.kodi_lab
    entered, release, cancelled = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    submissions = []
    original = WebSocket.send_bytes

    async def blocked(websocket: WebSocket, payload: bytes) -> None:
        entered.set()
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        submissions.append(len(payload))
        await original(websocket, payload)

    monkeypatch.setattr(WebSocket, "send_bytes", blocked)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        socket.receive_json()
        assert entered.wait(2)
        if revocation == "stop":
            assert (
                client.post(
                    f"/api/instances/{INSTANCE_ID}/display-policy",
                    json={
                        "enabled": False,
                        "attempt_id": cast(Any, client).preview_attempt_id,
                    },
                    headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
                ).status_code
                == 200
            )
        elif revocation == "logout":
            assert (
                client.post(
                    "/logout",
                    data={"csrf_token": csrf},
                    headers=ORIGIN,
                    follow_redirects=False,
                ).status_code
                == 303
            )
        elif revocation == "expiry":
            monkeypatch.setattr(context.auth, "resolve", lambda _token: None)
        else:
            socket.close()
        assert cancelled.wait(2)
        release.set()
        if revocation != "disconnect":
            with pytest.raises(WebSocketDisconnect):
                socket.receive_bytes()
    assert submissions == [] and broker.screenshot_calls == 2
    assert LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_reconciliation_after_metadata_prevents_pixel_submission(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    context = cast(Any, client.app).state.kodi_lab
    entered, release = threading.Event(), threading.Event()
    original = WebSocket.send_json

    async def metadata(websocket: WebSocket, value: Any, **kwargs: Any) -> None:
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        await original(websocket, value, **kwargs)

    monkeypatch.setattr(WebSocket, "send_json", metadata)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert entered.wait(2)
        blocked = context.reconciliation.status(INSTANCE_ID).model_copy(
            update={"required": True, "blocking": True}
        )
        monkeypatch.setattr(context.reconciliation, "status", lambda _id: blocked)
        release.set()
        assert socket.receive_json()["seq"] == 1
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()
    assert broker.screenshot_calls == 2 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_preview_never_reads_retained_still_png(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/screenshots",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 200
    )

    def forbidden(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Live path must not read/decompress retained PNG")

    monkeypatch.setattr(
        "kodi_lab.application.screenshots.read_published_png", forbidden
    )
    enable(client)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert socket.receive_json()["seq"] == 1
        assert socket.receive_bytes().startswith(b"\xff\xd8")
    assert broker.screenshot_calls == 3 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_mutation_cancellation_latch_survives_probe_completion(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    entered, release = threading.Event(), threading.Event()
    original = context.screenshots.capture_preview
    errors = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(context.screenshots, "capture_preview", blocked)

    def work() -> None:
        try:
            context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
        except LiveDisplayError as error:
            errors.append(error.code)

    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(2)
    assert LIVE_DISPLAY_REGISTRY.cancel(
        INSTANCE_ID
    )  # no policy revocation/identity drift
    assert LIVE_DISPLAY_REGISTRY.active_count() == 1
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == ["display_policy_denied"]
    with pytest.raises(LiveDisplayError):
        context.live_display.begin(INSTANCE_ID, preview_owner(csrf))
    assert broker.screenshot_calls == 1 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_delivery_validation_retains_capacity_until_real_worker_exits(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    enable(client)
    context = cast(Any, client.app).state.kodi_lab
    entered, release = threading.Event(), threading.Event()

    def blocked(_session: Any) -> None:
        entered.set()
        assert release.wait(3)
        raise LiveDisplayError("display_unavailable", "late validation failure")

    monkeypatch.setattr(context.live_display, "validate_delivery", blocked)
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert socket.receive_json()["seq"] == 1
        assert entered.wait(2)
        context.live_display.disable(INSTANCE_ID)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_bytes()
        assert LIVE_DISPLAY_REGISTRY.active_count() == 1
        with pytest.raises(LiveDisplayError, match="cleanup"):
            context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
        release.set()
        deadline = time.monotonic() + 2
        while LIVE_DISPLAY_REGISTRY.active_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert LIVE_DISPLAY_REGISTRY.active_count() == 0
    assert broker.screenshot_calls == 2


def test_stop_before_start_route_and_stale_stop_preserve_current_attempt(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    route = f"/api/instances/{INSTANCE_ID}/display-policy"
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    retired, current = str(uuid4()), str(uuid4())
    assert (
        client.post(
            route, json={"enabled": False, "attempt_id": retired}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            route, json={"enabled": True, "attempt_id": retired}, headers=headers
        ).status_code
        == 409
    )
    assert broker.screenshot_calls == 0
    assert (
        client.post(
            route, json={"enabled": True, "attempt_id": current}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            route, json={"enabled": False, "attempt_id": retired}, headers=headers
        ).status_code
        == 200
    )
    with client.websocket_connect(
        DISPLAY_URL, headers=authenticated_headers(client)
    ) as socket:
        assert socket.receive_json()["seq"] == 1
        assert socket.receive_bytes().startswith(b"\xff\xd8")
    assert broker.screenshot_calls == 2 and LIVE_DISPLAY_REGISTRY.active_count() == 0


def test_logout_before_probe_admission_denies_native_capture(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    make_visual_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    entered, release = threading.Event(), threading.Event()
    original = context.live_display._authority
    errors = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(2)
        return value

    monkeypatch.setattr(context.live_display, "_authority", blocked)

    def work() -> None:
        try:
            context.live_display.enable(INSTANCE_ID, preview_owner(csrf))
        except LiveDisplayError as error:
            errors.append(error.code)

    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(2)
    assert (
        client.post(
            "/logout", data={"csrf_token": csrf}, headers=ORIGIN, follow_redirects=False
        ).status_code
        == 303
    )
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == ["display_policy_denied"]
    assert broker.screenshot_calls == 0 and LIVE_DISPLAY_REGISTRY.active_count() == 0

"""Closed JPEG/decoded-ACK transport; one capture/frame outstanding, no queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from starlette.types import Message

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.live_display import (
    LiveDisplayError,
    LiveDisplayFrameSkipped,
    LiveDisplaySession,
    ManagedLiveDisplayService,
)
from kodi_lab.auth.service import WebAuthenticationService

ACK_TIMEOUT_SECONDS = 3.0
SEND_TIMEOUT_SECONDS = 2.0
MIN_FRAME_PERIOD_SECONDS = 0.2


def preview_owner(csrf_token: str) -> str:
    return hashlib.sha256(csrf_token.encode("ascii")).hexdigest()


def decoded_ack(message: Message, sequence: int) -> float:
    raw = message.get("text")
    if (
        message.get("type") != "websocket.receive"
        or not isinstance(raw, str)
        or len(raw.encode("utf-8")) > 256
    ):
        raise LiveDisplayError(
            "display_protocol_error",
            "A bounded decoded-frame acknowledgment is required.",
        )

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate acknowledgment field")
            result[key] = item
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except ValueError:
        raise LiveDisplayError(
            "display_protocol_error", "Invalid frame acknowledgment."
        ) from None
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "ack", "decode_ms"}
        or type(value["v"]) is not int
        or value["v"] != 1
        or type(value["ack"]) is not int
        or value["ack"] != sequence
    ):
        raise LiveDisplayError(
            "display_protocol_error", "Unexpected frame acknowledgment."
        )
    duration = value["decode_ms"]
    if (
        type(duration) not in {int, float}
        or not math.isfinite(duration)
        or not 0 <= duration <= 3000
    ):
        raise LiveDisplayError("display_protocol_error", "Invalid decode duration.")
    return float(duration)


async def stream_preview(
    websocket: WebSocket,
    instance_id: UUID,
    *,
    token: str | None,
    auth: WebAuthenticationService,
    display: ManagedLiveDisplayService,
) -> None:
    authenticated = auth.resolve(token)
    if authenticated is None:
        await websocket.close(code=4401, reason="display_session_expired")
        return
    owner = preview_owner(authenticated.csrf_token)
    # Begin has no capture side effect. If the request is cancelled while its
    # bounded identity work finishes, the completion callback releases its slot.
    beginning = asyncio.create_task(
        asyncio.to_thread(display.begin, instance_id, owner)
    )
    try:
        session = await asyncio.shield(beginning)
    except asyncio.CancelledError:

        def retire_begin(task: asyncio.Task[LiveDisplaySession]) -> None:
            with suppress(BaseException):
                display.end(task.result(), "display_cancelled")

        beginning.add_done_callback(retire_begin)
        raise
    except ManagedInstanceNotFoundError:
        await websocket.close(code=4404, reason="managed_instance_not_found")
        return
    except LiveDisplayError as error:
        await websocket.close(code=4409, reason=error.code)
        return
    end_reason = "display_accept_failed"
    receive: asyncio.Task[Message] | None = None
    sending: asyncio.Task[None] | None = None
    capture: asyncio.Task[Any] | None = None
    loop = asyncio.get_running_loop()

    def current() -> None:
        if auth.resolve(token) is None:
            raise LiveDisplayError(
                "display_session_expired", "The Web session expired."
            )
        display.check_current(session)

    async def pending[T](
        task: asyncio.Task[T], *, deadline: float | None = None, allow_ack: bool = False
    ) -> T:
        assert receive is not None
        while not task.done():
            current()
            if deadline is not None and loop.time() >= deadline:
                raise LiveDisplayError(
                    "display_backend_failed", "Preview delivery timed out."
                )
            await asyncio.wait(
                {task, receive}, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )
            if receive.done() and not (allow_ack and task.done()):
                message = receive.result()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(code=1000)
                raise LiveDisplayError(
                    "display_protocol_error",
                    "No browser message is allowed before a frame is delivered.",
                )
        current()
        if receive.done() and not allow_ack:
            if receive.result()["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=1000)
            raise LiveDisplayError("display_protocol_error", "Unexpected browser data.")
        return task.result()

    async def pacing(deadline: float) -> None:
        assert receive is not None
        while loop.time() < deadline:
            current()
            await asyncio.wait(
                {receive}, timeout=min(0.1, max(0, deadline - loop.time()))
            )
            if receive.done():
                if receive.result()["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(code=1000)
                raise LiveDisplayError(
                    "display_protocol_error", "Unexpected browser data."
                )

    def native_task[T](work: Callable[[], T]) -> asyncio.Task[T]:
        display.reserve_worker(session)

        def invoke() -> T:
            try:
                return work()
            finally:
                display.retire_worker(session)

        try:
            native = loop.run_in_executor(None, invoke)
        except BaseException:
            display.retire_worker(session)
            raise

        def consumed(future: asyncio.Future[T]) -> None:
            with suppress(BaseException):
                future.exception()

        native.add_done_callback(consumed)

        async def native_result() -> T:
            return await asyncio.shield(native)

        task = asyncio.create_task(native_result())
        task.add_done_callback(consumed)
        return task

    async def stop_sender() -> None:
        if sending is not None and not sending.done():
            sending.cancel()
            await asyncio.wait({sending}, timeout=0.5)
            if sending.done():
                with suppress(BaseException):
                    sending.result()

    try:
        await websocket.accept()
        display.mark_established(session)
        receive = asyncio.create_task(websocket.receive())
        next_frame = loop.time()
        sequence = 0
        while True:
            await pacing(next_frame)
            current()
            started = loop.time()
            capture = native_task(lambda: display.capture(session))
            try:
                frame = await pending(capture)
            except LiveDisplayFrameSkipped:
                session.skipped_frames += 1
                next_frame = max(started + session.period_seconds, loop.time() + 0.05)
                continue
            current()
            worker_seconds = loop.time() - started
            sequence += 1
            metadata = {
                "v": 1,
                "type": "frame",
                "seq": sequence,
                "mime": "image/jpeg",
                "width": frame.width,
                "height": frame.height,
                "captured_at": frame.captured_at.isoformat(),
                "age_ms": round(
                    max(
                        0,
                        (datetime.now(UTC) - frame.captured_at).total_seconds() * 1000,
                    ),
                    2,
                ),
                "capture_ms": round(frame.capture_ms, 2),
                "encode_ms": round(frame.encode_ms, 2),
                "skipped_frames": session.skipped_frames,
                "previous_delivery": session.previous_delivery,
                "target_fps": session.target_fps,
                "limited": session.period_seconds > 1 / session.target_fps + 0.01,
            }
            send_started = loop.time()
            sending = asyncio.create_task(websocket.send_json(metadata))
            await pending(sending, deadline=loop.time() + SEND_TIMEOUT_SECONDS)
            metadata_send_ms = (loop.time() - send_started) * 1000
            # Recheck revocable runtime/profile eligibility after metadata. This
            # bounded inventory work runs off-loop; current() still polls auth.
            validation_started = loop.time()
            delivery = native_task(lambda: display.validate_delivery(session))
            await pending(delivery)
            worker_seconds += loop.time() - validation_started

            async def send_pixels(payload: bytes = frame.jpeg) -> None:
                current()  # Immediately before submitting bytes to transport.
                await websocket.send_bytes(payload)

            binary_send_started = loop.time()
            sending = asyncio.create_task(send_pixels())
            await pending(
                sending, deadline=loop.time() + SEND_TIMEOUT_SECONDS, allow_ack=True
            )
            send_ms = metadata_send_ms + (loop.time() - binary_send_started) * 1000
            # The server cannot capture again until this exact frame has been
            # decoded/displayed and acknowledged. ACK timeout never retries.
            assert receive is not None
            ack_deadline = loop.time() + ACK_TIMEOUT_SECONDS
            while not receive.done():
                current()
                if loop.time() >= ack_deadline:
                    raise LiveDisplayError(
                        "display_ack_timeout",
                        "The browser did not decode the frame in time.",
                    )
                await asyncio.wait({receive}, timeout=0.1)
            message = receive.result()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(code=1000)
            decode_ms = decoded_ack(message, sequence)
            receive = asyncio.create_task(websocket.receive())
            current()
            display.record_delivery(session, frame.byte_size)
            session.capture_ms += frame.capture_ms
            session.encode_ms += frame.encode_ms
            session.send_ms += send_ms
            session.decode_ms += decode_ms
            session.previous_delivery = {
                "send_ms": round(send_ms, 2),
                "decode_ms": round(decode_ms, 2),
                "worker_ms": round(worker_seconds * 1000, 2),
            }
            session.recent_worker_seconds = max(
                worker_seconds, session.recent_worker_seconds * 0.9
            )
            if session.frame_count >= 4:
                # Reserve substantial capture/encode headroom. Slower achieved
                # rates are valid; a3FPS selection is an upper target only.
                session.period_seconds = max(
                    MIN_FRAME_PERIOD_SECONDS,
                    1 / session.target_fps,
                    2.5
                    * max(
                        session.recent_worker_seconds,
                        (frame.capture_ms + frame.encode_ms) / 1000,
                    ),
                    (send_ms + decode_ms) / 1000,
                )
            next_frame = max(started + session.period_seconds, loop.time() + 0.05)
    except LiveDisplayError as error:
        end_reason = error.code
        await stop_sender()
        with suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
            await asyncio.wait_for(websocket.close(code=1008, reason=error.code), 0.5)
    except WebSocketDisconnect:
        end_reason = "display_stopped"
    except Exception:
        end_reason = "display_backend_failed"
        await stop_sender()
        with suppress(RuntimeError, WebSocketDisconnect, TimeoutError):
            await asyncio.wait_for(websocket.close(code=1011, reason=end_reason), 0.5)
    finally:
        if receive is not None:
            receive.cancel()
            with suppress(BaseException):
                await receive
        await stop_sender()
        # Do not cancel a thread-backed capture: cancellation is not evidence
        # that its thread stopped. The actual capture thread releases capacity.
        display.end(session, end_reason)

"""Explicitly injected human viewer. Default application has no VNC route.

Authentication uses existing admin sessions/CSRF. An in-memory, one-use tab
ticket attaches one exact catalog/runtime identity. Mode changes require a
verified server handoff and exclusive human input ownership.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import struct
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from kodi_lab.application.live_display import (
    LIVE_DISPLAY_REGISTRY,
    LiveDisplaySession,
    LiveDisplaySessionRegistry,
)
from kodi_lab.application.novnc_control import (
    HumanControlBusyError,
    HumanViewerOwnership,
)
from kodi_lab.auth.service import AuthenticatedWebSession, WebAuthenticationService
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.viewer_diagnostics import diagnostic
from kodi_lab.web.view_attempts import FutureViewCancellation, ViewAttempts


class ExpectedSelection(BaseModel):
    """A stale human page may restrict admission, never choose a new binding."""

    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1, strict=True)
    runtime_revision_id: UUID
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: UUID
    source_id: UUID

    def matches(self, binding: ViewBinding) -> bool:
        return (
            binding.container == self.container_id
            and len(binding.identity) >= 6
            and binding.identity[0] == str(self.runtime_revision_id)
            and binding.identity[2:5]
            == (self.generation, str(self.workspace_id), str(self.source_id))
        )

    @classmethod
    def from_binding(cls, binding: ViewBinding) -> ExpectedSelection | None:
        if len(binding.identity) < 6:
            return None
        return cls.model_validate(
            {
                "generation": binding.identity[2],
                "runtime_revision_id": binding.identity[0],
                "container_id": binding.container,
                "workspace_id": binding.identity[3],
                "source_id": binding.identity[4],
            }
        )


class StartView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: UUID
    rate: Literal[2] = 2
    expected_selection: ExpectedSelection | None = None
    expected_binding: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def binding_fingerprint(instance: UUID, binding: ViewBinding) -> str:
    """Restrict a rendered page to server-resolved identity; confer no authority."""
    return hashlib.sha256(
        json.dumps(
            (
                str(instance),
                binding.container,
                binding.image,
                binding.started,
                binding.identity,
            ),
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class StopView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: UUID


class ChangeMode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: UUID
    next_attempt: UUID


class AdmissionBodyLimit:
    """Bound prototype admission JSON before FastAPI parses even anonymous bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path.startswith("/api/instances/")
            and path.endswith(
                (
                    "/novnc-start",
                    "/novnc-stop",
                    "/novnc-take-control",
                    "/novnc-release-control",
                )
            )
        ):
            await self.app(scope, receive, send)
            return
        body = bytearray()
        deadline = time.monotonic() + 2
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("view body deadline")
                message = await asyncio.wait_for(
                    receive(), max(0.001, deadline - time.monotonic())
                )
                if message["type"] != "http.request":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > 512:
                    raise ValueError("view body exceeded")
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
        except (TimeoutError, ValueError):
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"view_request_too_large"}',
                }
            )
            return
        consumed = False

        async def bounded_receive() -> Any:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


@dataclass
class Attachment:
    ticket: str
    attempt: UUID
    owner: str
    binding: ViewBinding
    rate: int
    session: LiveDisplaySession
    expires: float
    connected: bool = False
    mode: str = "view-only"
    deadline: str | None = None


class PrototypeViewer:
    def __init__(
        self,
        socket_path: Path,
        authority: Callable[[UUID], ViewBinding],
        *,
        registry: LiveDisplaySessionRegistry = LIVE_DISPLAY_REGISTRY,
        ownership: HumanViewerOwnership | None = None,
        enabled_instances: frozenset[UUID] | None = None,
        require_https: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.authority = authority
        self.registry = registry
        self.lock = Lock()
        self.attachments: dict[UUID, Attachment] = {}
        self.attempts = ViewAttempts()
        self.closed = False
        self.admissions = BoundedSemaphore(2)
        self.checks = BoundedSemaphore(2)
        self.ownership = ownership
        self.enabled_instances = enabled_instances
        self.require_https = require_https
        self.handoffs = BoundedSemaphore(1)
        self.mutations = BoundedSemaphore(4)

    async def checked[T](
        self,
        callback: Callable[[], T],
        *,
        cancelled: Any = None,
        timeout: float = 1.0,
        capacity: BoundedSemaphore | None = None,
    ) -> T:
        """Bound DB waits; retain each read slot until its real thread finishes."""
        semaphore = capacity or self.checks
        if not semaphore.acquire(blocking=False):
            raise ValueError("view check capacity reached")

        def execute() -> T:
            try:
                return callback()
            finally:
                semaphore.release()

        work = asyncio.create_task(asyncio.to_thread(execute))
        deadline = time.monotonic() + timeout
        try:
            while not work.done():
                if cancelled is not None and cancelled.is_set():
                    raise ValueError("view check cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("view check deadline")
                await asyncio.wait({work}, timeout=min(0.05, remaining))
            if cancelled is not None and cancelled.is_set():
                raise ValueError("view check cancelled")
            return work.result()
        except BaseException:

            def consume(task: asyncio.Task[T]) -> None:
                with suppress(BaseException):
                    task.exception()

            work.add_done_callback(consume)
            raise

    async def mutate[T](self, callback: Callable[[], T]) -> T:
        return await self.checked(callback, timeout=8, capacity=self.mutations)

    def sweep(self) -> None:
        with self.lock:
            self._expire()

    def start(
        self,
        instance: UUID,
        owner: str,
        body: StartView,
        *,
        mode: str = "view-only",
        deadline: str | None = None,
    ) -> Attachment:
        if not self.admissions.acquire(blocking=False):
            raise ValueError("view admission capacity reached")
        try:
            return self._start(instance, owner, body, mode=mode, deadline=deadline)
        finally:
            self.admissions.release()

    def _start(
        self,
        instance: UUID,
        owner: str,
        body: StartView,
        *,
        mode: str,
        deadline: str | None,
    ) -> Attachment:
        self.attempts.expires(body.attempt)
        binding = self.binding(instance, mode)
        if body.expected_selection is not None and not body.expected_selection.matches(
            binding
        ):
            raise ValueError("view selection changed")
        if body.expected_binding is not None:
            if not secrets.compare_digest(
                body.expected_binding, binding_fingerprint(instance, binding)
            ):
                raise ValueError("view runtime changed")
        elif len(binding.identity) == 3 and binding.identity[2] is None:
            # Standalone admission must carry the original rendered incarnation.
            # Selected development viewers retain their existing selection fence.
            raise ValueError("view runtime context required")
        with self.lock:
            self._expire()
            if self.closed:
                raise ValueError("view attempt unavailable")
            self.attempts.claim(owner, body.attempt)
            if body.attempt in self.attachments:
                raise ValueError("view attempt unavailable")
            session = LiveDisplaySession(
                session_id=uuid4(),
                instance_id=instance,
                runtime_revision_id=UUID(str(binding.identity[0])),
                container_id=binding.container,
                started_at=datetime.now(UTC),
                started_monotonic=time.monotonic(),
                owner=owner,
                authority=binding.identity,
                novnc_mode=cast(Literal["view-only", "interactive"], mode),
            )
            self.registry.acquire(session)
            try:
                if self.ownership is not None:
                    deadline = self.ownership.create(
                        instance, body.attempt, owner, binding, mode, deadline
                    )
                elif mode != "view-only":
                    raise ValueError("interactive ownership unavailable")
            except BaseException:
                self.registry.release(session)
                raise
            attachment = Attachment(
                secrets.token_urlsafe(32),
                body.attempt,
                owner,
                binding,
                body.rate,
                session,
                time.monotonic() + 10,
            )
            attachment.mode = mode
            attachment.deadline = deadline
            self.attachments[body.attempt] = attachment
            return attachment

    def _expire(self) -> None:
        for item in list(self.attachments.values()):
            if not item.connected and time.monotonic() >= item.expires:
                item.session.cancelled.set()
                if self.ownership is not None:
                    self.ownership.revoke(
                        item.session.instance_id, item.attempt, item.owner
                    )
                self.registry.release(item.session)
                del self.attachments[item.attempt]

    def consume(self, instance: UUID, owner: str, ticket: str) -> Attachment:
        with self.lock:
            self._expire()
            for item in self.attachments.values():
                if (
                    item.owner == owner
                    and item.session.instance_id == instance
                    and secrets.compare_digest(item.ticket, ticket)
                    and not item.connected
                    and not item.session.cancelled.is_set()
                ):
                    item.connected = True
                    item.ticket = ""
                    return item
        raise ValueError("view attachment denied")

    def stop(self, owner: str, attempt: UUID) -> None:
        with self.lock:
            self.attempts.cancel(owner, attempt)
            item = self.attachments.get(attempt)
            if item is not None and item.owner == owner:
                item.session.cancelled.set()
                if self.ownership is not None:
                    self.ownership.revoke(
                        item.session.instance_id, item.attempt, item.owner
                    )
                if not item.connected:
                    self.registry.release(item.session)
                    del self.attachments[attempt]

    def end(self, item: Attachment) -> None:
        with self.lock:
            item.session.cancelled.set()
            if self.ownership is not None:
                self.ownership.revoke(
                    item.session.instance_id, item.attempt, item.owner
                )
            self.registry.release(item.session)
            if self.attachments.get(item.attempt) is item:
                del self.attachments[item.attempt]

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for item in list(self.attachments.values()):
                item.session.cancelled.set()
                if self.ownership is not None:
                    self.ownership.revoke(
                        item.session.instance_id, item.attempt, item.owner
                    )
                if not item.connected:
                    self.registry.release(item.session)
                    del self.attachments[item.attempt]

    def handoff(
        self, instance: UUID, owner: str, body: ChangeMode, mode: str
    ) -> Attachment:
        if self.ownership is None or not self.handoffs.acquire(blocking=False):
            raise ValueError("mode change unavailable")
        try:
            self.attempts.expires(body.next_attempt)
            if mode == "interactive":
                self.ownership.require_control_available(instance)
            with self.lock:
                item = self.attachments.get(body.attempt)
                if (
                    item is None
                    or item.owner != owner
                    or item.session.instance_id != instance
                    or item.session.cancelled.is_set()
                    or item.mode == mode
                ):
                    raise ValueError("mode change denied")
                deadline = item.deadline
            self.stop(owner, body.attempt)
            self.ownership.wait_released(instance)
            self.end(item)
            try:
                return self.start(
                    instance,
                    owner,
                    StartView(
                        attempt=body.next_attempt,
                        expected_selection=ExpectedSelection.from_binding(item.binding),
                        expected_binding=binding_fingerprint(instance, item.binding),
                    ),
                    mode=mode,
                    deadline=deadline,
                )
            except HumanControlBusyError as error:
                # The old attachment is already retired. Only the early check
                # may promise an untouched view-only connection to the browser.
                raise ValueError("view successor unavailable") from error
        finally:
            self.handoffs.release()

    def binding(self, instance: UUID, mode: str) -> ViewBinding:
        if mode == "interactive":
            if self.ownership is None:
                raise ValueError("interactive ownership unavailable")
            self.ownership.require_control_available(instance)
        return self.authority(instance)

    def page_binding(self, instance: UUID) -> ViewBinding:
        """Inspect availability within the existing read capacity; admit no worker."""
        if not self.checks.acquire(blocking=False):
            raise ValueError("view check capacity reached")
        try:
            return self.binding(instance, "view-only")
        finally:
            self.checks.release()

    def revoke_instance(self, instance: UUID) -> None:
        self.registry.cancel(instance)
        if self.ownership is not None:
            self.ownership.revoke(instance)

    def retire_owner(self, owner: str) -> None:
        with self.lock:
            attempts = [
                a.attempt for a in self.attachments.values() if a.owner == owner
            ]
        for attempt in attempts:
            self.stop(owner, attempt)


def owner_of(session: AuthenticatedWebSession) -> str:
    return hashlib.sha256(session.csrf_token.encode("ascii")).hexdigest()


async def relay(
    websocket: WebSocket,
    viewer: PrototypeViewer,
    item: Attachment,
    current: Callable[[], bool],
    auth_current: Callable[[], bool],
) -> None:
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    tasks: list[asyncio.Task[str]] = []
    began = time.monotonic()
    phase, reason = "binding_check", "failed"
    accepted = False
    ws_code: object = None
    outcomes = dict.fromkeys(("incoming", "outgoing", "watchdog"), "pending")
    try:
        # Single-use browser ticket was consumed before any worker admission.
        if (
            await viewer.checked(
                lambda: viewer.binding(item.session.instance_id, item.mode),
                cancelled=item.session.cancelled,
            )
            != item.binding
        ):
            reason = "binding_changed"
            raise ValueError("view identity changed")
        phase = "current_check"
        if not current():
            reason = "session_ended"
            raise ValueError("view cancelled")
        phase = "broker_connect"
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(viewer.socket_path), limit=65536), 1
        )
        assert reader is not None and writer is not None
        phase = "broker_handshake"
        challenge = (await asyncio.wait_for(reader.readexactly(64), 1)).decode("ascii")
        request = {
            "instance": str(item.session.instance_id),
            "owner": item.owner,
            "rate": item.rate,
            "challenge": challenge,
            "attempt": str(item.attempt),
            "mode": item.mode,
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), 1)
        if await asyncio.wait_for(reader.readexactly(3), 2) != b"OK\n":
            reason = "worker_refused"
            raise ValueError("view worker refused")
        if not current():
            reason = "session_ended"
            raise ValueError("view cancelled")
        phase = "ws_accept"
        await websocket.accept(subprotocol="novnc")
        accepted = True
        diagnostic(
            "web_accepted",
            attempt=item.attempt,
            instance=item.session.instance_id,
            elapsed_ms=int((time.monotonic() - began) * 1000),
        )
        phase = "streaming"

        async def incoming() -> str:
            nonlocal ws_code
            assert writer is not None
            window = time.monotonic()
            total = 0
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    ws_code = message.get("code")
                    return "browser_disconnect"
                payload = message.get("bytes")
                if message["type"] != "websocket.receive" or not isinstance(
                    payload, bytes
                ):
                    return "invalid_browser_message"
                if not 0 < len(payload) <= 65536:
                    return "input_limit"
                if not current():
                    return "session_ended"
                now = time.monotonic()
                if now - window >= 1:
                    window, total = now, 0
                total += len(payload)
                if total > 131072:
                    return "input_limit"
                writer.write(struct.pack("!BI", 1, len(payload)) + payload)
                await asyncio.wait_for(writer.drain(), 2)

        async def outgoing() -> str:
            assert reader is not None
            while True:
                data = await reader.read(65536)
                if not data or not current():
                    return "broker_eof" if not data else "session_ended"
                await asyncio.wait_for(websocket.send_bytes(data), 2)

        async def watchdog() -> str:
            assert writer is not None
            denial = "authorization_rejected"

            def authorized() -> bool:
                nonlocal denial
                if not auth_current():
                    denial = "authorization_rejected"
                    return False
                if viewer.binding(item.session.instance_id, item.mode) != item.binding:
                    denial = "binding_changed"
                    return False
                if viewer.ownership is not None and not viewer.ownership.renew(
                    item.session.instance_id, item.attempt, item.owner
                ):
                    denial = "ownership_rejected"
                    return False
                return True

            while current():
                if not await viewer.checked(
                    authorized, cancelled=item.session.cancelled
                ):
                    return denial
                if not current():
                    return "session_ended"
                # Browser bytes are always kind 1; only this trusted check may
                # renew the Web lease. A frozen/lost Web expires at the broker.
                writer.write(struct.pack("!BI", 2, 0))
                await asyncio.wait_for(writer.drain(), 1)
                await asyncio.sleep(0.25)
            return "session_ended"

        tasks = [asyncio.create_task(work()) for work in (incoming, outgoing, watchdog)]
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        reason = "task_completed"
        for name, task in zip(outcomes, tasks, strict=True):
            if task in completed:
                try:
                    outcomes[name] = task.result()
                except TimeoutError:
                    outcomes[name] = "timeout"
                except WebSocketDisconnect as error:
                    ws_code = error.code
                    outcomes[name] = "browser_disconnect"
                except (OSError, asyncio.IncompleteReadError):
                    outcomes[name] = "transport_error"
                except asyncio.CancelledError:
                    outcomes[name] = "cancelled"
                except Exception:
                    outcomes[name] = "internal_error"
    except BaseException as error:
        if reason == "failed":
            reason = (
                "timeout"
                if isinstance(error, TimeoutError)
                else "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "transport_error"
                if isinstance(error, (OSError, asyncio.IncompleteReadError))
                else "internal_error"
            )
        raise
    finally:
        item.session.cancelled.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1)
        with suppress(Exception):
            await asyncio.wait_for(websocket.close(code=1000), 1)
        cleanup_completed = False
        try:
            await viewer.mutate(lambda: viewer.end(item))
            cleanup_completed = True
        finally:
            diagnostic(
                "web_ended",
                attempt=item.attempt,
                instance=item.session.instance_id,
                accepted=accepted,
                phase=phase,
                reason=reason,
                ws_code=ws_code,
                close_requested_code=1000,
                cleanup_completed=cleanup_completed,
                elapsed_ms=int((time.monotonic() - began) * 1000),
                **outcomes,
            )


def install_routes(
    app: FastAPI,
    viewer: PrototypeViewer,
    auth: WebAuthenticationService,
    cookie_name: str,
    current_session: Any,
    mutation_session: Any,
    same_origin: Callable[[WebSocket], bool],
    page_context: Callable[[UUID], dict[str, Any]] | None = None,
) -> None:
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    templates.env.globals["viewer_clock_ms"] = viewer.attempts.clock_ms
    login_dependency = Depends(current_session)
    mutation_dependency = Depends(mutation_session)
    app.add_middleware(AdmissionBodyLimit)

    @app.middleware("http")
    async def secure_human_viewer(request: Request, call_next: Any) -> Any:
        path = request.url.path
        viewer_path = path.endswith("/novnc-phase1") or (
            path.startswith("/api/instances/") and "/novnc-" in path
        )
        auth_write = request.method == "POST" and path in {
            "/login",
            "/logout",
            "/admin-view",
        }
        if (
            viewer.require_https
            and (viewer_path or auth_write)
            and request.url.scheme != "https"
        ):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "viewer_requires_https"}, status_code=403)
        return await call_next(request)

    @app.get(
        "/instances/{instance}/novnc-phase1",
        response_class=HTMLResponse,
        name="novnc_phase1_page",
    )
    def page(
        request: Request, instance: UUID, authenticated: Any = login_dependency
    ) -> HTMLResponse:
        if (
            viewer.enabled_instances is not None
            and instance not in viewer.enabled_instances
        ):
            raise HTTPException(404)
        return templates.TemplateResponse(
            request,
            "novnc.html",
            {
                "csrf_token": authenticated.csrf_token,
                "instance": instance,
                **({} if page_context is None else page_context(instance)),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def cancel_failed_attempt(owner: str, attempt: UUID) -> None:
        # A rejected future ID cannot have an admitted worker. Preserve the
        # original refusal/disconnect; this is not a successful Stop response.
        with suppress(FutureViewCancellation):
            await viewer.mutate(lambda: viewer.stop(owner, attempt))

    @app.post("/api/instances/{instance}/novnc-start")
    async def start(
        instance: UUID,
        body: StartView,
        request: Request,
        authenticated: Any = mutation_dependency,
    ) -> dict[str, str]:
        owner = owner_of(authenticated)
        try:
            viewer.attempts.expires(body.attempt)
        except ValueError:
            raise HTTPException(409, "view_unavailable") from None
        task = asyncio.create_task(
            asyncio.to_thread(viewer.start, instance, owner, body)
        )
        try:
            item = await asyncio.shield(task)
        except asyncio.CancelledError:
            await cancel_failed_attempt(owner, body.attempt)
            raise
        except Exception:
            raise HTTPException(409, "view_unavailable") from None
        try:
            current_auth = await viewer.checked(
                lambda: auth.resolve(request.cookies.get(cookie_name))
            )
        except asyncio.CancelledError:
            await cancel_failed_attempt(owner, body.attempt)
            raise
        except Exception:
            current_auth = None
        if current_auth is None:
            await cancel_failed_attempt(owner, body.attempt)
            raise HTTPException(401, "view_session_expired")
        return {"ticket": item.ticket, "mode": item.mode}

    @app.post("/api/instances/{instance}/novnc-stop")
    def stop(
        instance: UUID, body: StopView, authenticated: Any = mutation_dependency
    ) -> dict[str, bool]:
        owner = owner_of(authenticated)
        with viewer.lock:
            item = viewer.attachments.get(body.attempt)
            if item is not None and item.session.instance_id != instance:
                raise HTTPException(409, "view_identity_changed")
        try:
            viewer.stop(owner, body.attempt)
        except FutureViewCancellation:
            raise HTTPException(409, "view_cancellation_unconfirmed") from None
        if viewer.ownership is not None and item is not None and item.owner == owner:
            try:
                viewer.ownership.wait_released(instance)
            except ValueError:
                raise HTTPException(409, "input_release_unproven") from None
        return {"stopped": True}

    async def change(
        instance: UUID,
        body: ChangeMode,
        request: Request,
        authenticated: Any,
        mode: str,
    ) -> dict[str, str]:
        owner = owner_of(authenticated)
        try:
            viewer.attempts.expires(body.next_attempt)
        except ValueError:
            raise HTTPException(409, "view_handoff_unavailable") from None
        task = asyncio.create_task(
            asyncio.to_thread(viewer.handoff, instance, owner, body, mode)
        )
        try:
            item = await asyncio.shield(task)
            if (
                await viewer.checked(
                    lambda: auth.resolve(request.cookies.get(cookie_name))
                )
                is None
            ):
                await cancel_failed_attempt(owner, body.next_attempt)
                raise HTTPException(401, "view_session_expired")
            return {"ticket": item.ticket, "mode": item.mode}
        except asyncio.CancelledError:
            await cancel_failed_attempt(owner, body.next_attempt)
            raise
        except HTTPException:
            raise
        except HumanControlBusyError:
            raise HTTPException(409, "agent_operation_active") from None
        except Exception:
            await cancel_failed_attempt(owner, body.next_attempt)
            raise HTTPException(409, "view_handoff_unavailable") from None

    @app.post("/api/instances/{instance}/novnc-take-control")
    async def take_control(
        instance: UUID,
        body: ChangeMode,
        request: Request,
        authenticated: Any = mutation_dependency,
    ) -> dict[str, str]:
        return await change(instance, body, request, authenticated, "interactive")

    @app.post("/api/instances/{instance}/novnc-release-control")
    async def release_control(
        instance: UUID,
        body: ChangeMode,
        request: Request,
        authenticated: Any = mutation_dependency,
    ) -> dict[str, str]:
        return await change(instance, body, request, authenticated, "view-only")

    @app.websocket("/api/instances/{instance}/novnc")
    async def stream(websocket: WebSocket, instance: UUID) -> None:
        token = websocket.cookies.get(cookie_name)
        try:
            authenticated = await viewer.checked(lambda: auth.resolve(token))
        except Exception:
            authenticated = None
        protocols = websocket.scope.get("subprotocols", [])
        if (
            authenticated is None
            or (viewer.require_https and websocket.url.scheme != "wss")
            or websocket.scope.get("query_string")
            or not same_origin(websocket)
            or len(protocols) != 2
            or protocols[0] != "novnc"
            or not protocols[1].startswith("view.")
        ):
            diagnostic(
                "web_rejected",
                instance=instance,
                reason="authorization_rejected",
                close_requested_code=4403,
            )
            await websocket.close(code=4403)
            return
        try:
            item = await viewer.mutate(
                lambda: viewer.consume(
                    instance, owner_of(authenticated), protocols[1][5:]
                )
            )
        except ValueError:
            diagnostic(
                "web_rejected",
                instance=instance,
                reason="ticket_rejected",
                close_requested_code=4403,
            )
            await websocket.close(code=4403)
            return

        def current() -> bool:
            return (
                not item.session.cancelled.is_set()
                and time.monotonic() - item.session.started_monotonic < 600
            )

        try:
            await relay(
                websocket,
                viewer,
                item,
                current,
                lambda: auth.resolve(token) is not None,
            )
        except Exception:
            with suppress(Exception):
                await websocket.close(code=1011)

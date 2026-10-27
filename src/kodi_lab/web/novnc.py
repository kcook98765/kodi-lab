"""Explicitly injected Phase 1 prototype. Default application has no VNC route.

Authentication uses existing admin sessions/CSRF. An in-memory, one-use tab
ticket attaches one exact catalog/runtime identity. No reconnect or input mode.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from kodi_lab.application.live_display import (
    LIVE_DISPLAY_REGISTRY,
    LiveDisplaySession,
    LiveDisplaySessionRegistry,
)
from kodi_lab.auth.service import AuthenticatedWebSession, WebAuthenticationService
from kodi_lab.broker.novnc import ViewBinding


class StartView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: UUID
    rate: Literal[10, 15] = 10


class StopView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt: UUID


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


class PrototypeViewer:
    def __init__(
        self,
        socket_path: Path,
        authority: Callable[[UUID], ViewBinding],
        *,
        registry: LiveDisplaySessionRegistry = LIVE_DISPLAY_REGISTRY,
    ) -> None:
        self.socket_path = socket_path
        self.authority = authority
        self.registry = registry
        self.lock = Lock()
        self.attachments: dict[UUID, Attachment] = {}
        self.attempts: set[tuple[str, UUID]] = set()
        self.closed = False

    def start(self, instance: UUID, owner: str, body: StartView) -> Attachment:
        binding = self.authority(instance)
        with self.lock:
            self._expire()
            key = (owner, body.attempt)
            if self.closed or key in self.attempts or len(self.attempts) >= 256:
                raise ValueError("view attempt unavailable")
            self.attempts.add(key)
            session = LiveDisplaySession(
                session_id=uuid4(),
                instance_id=instance,
                runtime_revision_id=UUID(str(binding.identity[0])),
                container_id=binding.container,
                started_at=datetime.now(UTC),
                started_monotonic=time.monotonic(),
                owner=owner,
            )
            self.registry.acquire(session)
            attachment = Attachment(
                secrets.token_urlsafe(32),
                body.attempt,
                owner,
                binding,
                body.rate,
                session,
                time.monotonic() + 10,
            )
            self.attachments[body.attempt] = attachment
            return attachment

    def _expire(self) -> None:
        for item in list(self.attachments.values()):
            if not item.connected and time.monotonic() >= item.expires:
                item.session.cancelled.set()
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
            if len(self.attempts) < 256:
                self.attempts.add((owner, attempt))
            item = self.attachments.get(attempt)
            if item is not None and item.owner == owner:
                item.session.cancelled.set()
                if not item.connected:
                    self.registry.release(item.session)
                    del self.attachments[attempt]

    def end(self, item: Attachment) -> None:
        with self.lock:
            item.session.cancelled.set()
            self.registry.release(item.session)
            if self.attachments.get(item.attempt) is item:
                del self.attachments[item.attempt]

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for item in list(self.attachments.values()):
                item.session.cancelled.set()
                if not item.connected:
                    self.registry.release(item.session)
                    del self.attachments[item.attempt]


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
    tasks: list[asyncio.Task[None]] = []
    try:
        # Single-use browser ticket was consumed before any worker admission.
        if (
            await asyncio.to_thread(viewer.authority, item.session.instance_id)
            != item.binding
        ):
            raise ValueError("view identity changed")
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(viewer.socket_path), limit=65536), 1
        )
        assert reader is not None and writer is not None
        challenge = (await asyncio.wait_for(reader.readexactly(64), 1)).decode("ascii")
        request = {
            "instance": str(item.session.instance_id),
            "owner": item.owner,
            "rate": item.rate,
            "challenge": challenge,
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), 1)
        if await asyncio.wait_for(reader.readexactly(3), 2) != b"OK\n":
            raise ValueError("view worker refused")
        await websocket.accept(subprotocol="novnc")

        async def incoming() -> None:
            assert writer is not None
            window = time.monotonic()
            total = 0
            while True:
                message = await websocket.receive()
                payload = message.get("bytes")
                if message["type"] != "websocket.receive" or not isinstance(
                    payload, bytes
                ):
                    return
                if not 0 < len(payload) <= 65536 or not current():
                    return
                now = time.monotonic()
                if now - window >= 1:
                    window, total = now, 0
                total += len(payload)
                if total > 131072:
                    return
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), 2)

        async def outgoing() -> None:
            assert reader is not None
            while True:
                data = await reader.read(65536)
                if not data or not current():
                    return
                await asyncio.wait_for(websocket.send_bytes(data), 2)

        async def watchdog() -> None:
            while current():
                if not await asyncio.to_thread(auth_current):
                    return
                if (
                    await asyncio.to_thread(viewer.authority, item.session.instance_id)
                    != item.binding
                ):
                    return
                await asyncio.sleep(0.25)

        tasks = [asyncio.create_task(work()) for work in (incoming, outgoing, watchdog)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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
            await websocket.close(code=1000)
        viewer.end(item)


def install_routes(
    app: FastAPI,
    viewer: PrototypeViewer,
    auth: WebAuthenticationService,
    cookie_name: str,
    current_session: Any,
    mutation_session: Any,
    same_origin: Callable[[WebSocket], bool],
) -> None:
    login_dependency = Depends(current_session)
    mutation_dependency = Depends(mutation_session)

    @app.get("/instances/{instance}/novnc-phase1", response_class=HTMLResponse)
    def page(instance: UUID, authenticated: Any = login_dependency) -> HTMLResponse:
        csrf = escape(authenticated.csrf_token, quote=True)
        return HTMLResponse(
            "<!doctype html><html><head>"
            '<meta name="viewport" content="width=device-width">'
            f'<meta name="kodi-lab-csrf" content="{csrf}">'
            '<link rel="stylesheet" href="/static/novnc-phase1.css">'
            "<title>Kodi Lab view-only prototype</title>"
            "</head><body><h1>Kodi Lab view-only prototype</h1>"
            "<p>No input or audio.</p>"
            f'<main data-instance="{instance}"><select id="rate">'
            "<option>10</option><option>15</option></select>"
            '<button id="start">Start viewing</button>'
            '<button id="stop" disabled>Stop</button>'
            '<p id="status">Off</p><div id="viewer"></div></main>'
            '<script type="module" src="/static/novnc-phase1.js"></script>'
            "</body></html>",
            headers={
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/instances/{instance}/novnc-start")
    async def start(
        instance: UUID,
        body: StartView,
        request: Request,
        authenticated: Any = mutation_dependency,
    ) -> dict[str, str]:
        owner = owner_of(authenticated)
        task = asyncio.create_task(
            asyncio.to_thread(viewer.start, instance, owner, body)
        )
        try:
            item = await asyncio.shield(task)
        except asyncio.CancelledError:
            viewer.stop(owner, body.attempt)
            raise
        except Exception:
            raise HTTPException(409, "view_unavailable") from None
        if auth.resolve(request.cookies.get(cookie_name)) is None:
            viewer.stop(owner, body.attempt)
            raise HTTPException(401, "view_session_expired")
        return {"ticket": item.ticket, "mode": "view-only"}

    @app.post("/api/instances/{instance}/novnc-stop")
    def stop(
        instance: UUID, body: StopView, authenticated: Any = mutation_dependency
    ) -> dict[str, bool]:
        viewer.stop(owner_of(authenticated), body.attempt)
        return {"stopped": True}

    @app.websocket("/api/instances/{instance}/novnc")
    async def stream(websocket: WebSocket, instance: UUID) -> None:
        token = websocket.cookies.get(cookie_name)
        authenticated = auth.resolve(token)
        protocols = websocket.scope.get("subprotocols", [])
        if (
            authenticated is None
            or websocket.scope.get("query_string")
            or not same_origin(websocket)
            or len(protocols) != 2
            or protocols[0] != "novnc"
            or not protocols[1].startswith("view.")
        ):
            await websocket.close(code=4403)
            return
        try:
            item = viewer.consume(instance, owner_of(authenticated), protocols[1][5:])
        except ValueError:
            await websocket.close(code=4403)
            return

        def current() -> bool:
            return (
                not item.session.cancelled.is_set()
                and time.monotonic() - item.session.started_monotonic < 900
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

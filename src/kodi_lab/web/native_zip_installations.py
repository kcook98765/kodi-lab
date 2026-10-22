"""Bearer-authorized native ZIP requests and exact-peer temporary delivery."""

from __future__ import annotations

import asyncio
import html
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import TypeVar
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.responses import StreamingResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.mcp_administration import (
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.native_zip_installations import (
    NativeZipError,
    NativeZipInstallationService,
    NativeZipRequest,
    NativeZipStatus,
)
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.repository.server import (
    RangeNotSatisfiable,
    RangeSyntaxError,
    single_byte_range,
)

_T = TypeVar("_T")
PREPARATION_TIMEOUT_SECONDS = 15.0
SEND_TIMEOUT_SECONDS = 15.0


class NativeZipBodyLimitMiddleware:
    """Bound small bearer API requests before FastAPI parses JSON."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope.get("path", "").startswith("/api/native-zip-installations")
        ):
            await self._app(scope, receive, send)
            return
        data = bytearray()
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    body = message.get("body", b"")
                    if len(data) + len(body) > 4096:
                        await Response(status_code=413)(scope, receive, send)
                        return
                    data.extend(body)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await Response(status_code=408)(scope, receive, send)
            return
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()

        await self._app(scope, replay, send)


class _VerifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TimedDelivery(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Stream directly: the legacy ASGI disconnect task group's cancellation
        # shielding must not extend the explicit send deadline. Bounded sends
        # raise on transport failure or timeout; no transfer success is recorded.
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            await self.stream_response(send)
            if self.background is not None:
                await self.background()


def native_zip_router(
    service: NativeZipInstallationService, authentication: McpAuthenticationService
) -> APIRouter:
    slots = threading.BoundedSemaphore(4)

    async def sweep() -> None:
        while True:
            await asyncio.to_thread(service.sweep)
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(service.sweep)
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    router = APIRouter(lifespan=lifespan)

    def principal(request: Request) -> McpPrincipal:
        # Session cookies/read-only Web access are not native-install authority.
        header = request.headers.get("authorization", "")
        identity = (
            authentication.authenticate(header[7:])
            if header.startswith("Bearer ")
            else None
        )
        if identity is None:
            raise HTTPException(401, "native_zip_authentication_required")
        return identity

    def invoke(call: Callable[[], _T]) -> _T:
        try:
            return call()
        except McpAuthorizationError as error:
            raise HTTPException(403, error.code) from error
        except NativeZipError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(503, "native_zip_operation_unavailable") from error

    @router.post(
        "/api/native-zip-installations", response_model=NativeZipStatus, status_code=202
    )
    def request_installation(
        request: Request, payload: NativeZipRequest
    ) -> NativeZipStatus:
        actor = principal(request)
        return invoke(lambda: service.request(actor, payload))

    @router.get(
        "/api/native-zip-installations/{operation_id}", response_model=NativeZipStatus
    )
    def status(request: Request, operation_id: UUID) -> NativeZipStatus:
        actor = principal(request)
        return invoke(lambda: service.get(actor, operation_id))

    @router.post(
        "/api/native-zip-installations/{operation_id}/verify",
        response_model=NativeZipStatus,
    )
    def verify(
        request: Request, operation_id: UUID, payload: _VerifyInput | None = None
    ) -> NativeZipStatus:
        actor = principal(request)
        return invoke(lambda: service.verify(actor, operation_id))

    @router.api_route(
        "/native-zip-packages/{operation_id}/",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    @router.api_route(
        "/native-zip-packages/{operation_id}/{name}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def package(
        request: Request, operation_id: UUID, name: str | None = None
    ) -> Response:
        if (
            request.url.query
            or request.client is None
            # Uvicorn can rewrite scope.client before application admission.
            # Refuse forwarded addresses rather than authorize that rewrite.
            or any(
                header in request.headers
                for header in ("x-forwarded-for", "forwarded", "x-real-ip")
            )
        ):
            raise HTTPException(404)
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "native_zip_delivery_busy")
        handed_off = False
        released = False
        release_lock = threading.Lock()

        def release_slot() -> None:
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    slots.release()

        def prepare() -> tuple[bytes, str, tuple[int, int] | None]:
            # Forwarded addresses were rejected above; hidden peers fail closed.
            assert request.client is not None
            data, filename = service.package(
                operation_id, request.client.host, name=name, mark_delivery=False
            )
            selected = None
            range_headers = request.headers.getlist("range")
            if name is not None:
                try:
                    if len(range_headers) > 1:
                        raise RangeSyntaxError("multiple ranges")
                    if range_headers and not request.headers.get("if-range"):
                        selected = single_byte_range(range_headers[0], len(data))
                except RangeNotSatisfiable as error:
                    raise HTTPException(
                        416, headers={"Content-Range": f"bytes */{len(data)}"}
                    ) from error
                except RangeSyntaxError as error:
                    raise HTTPException(400, "invalid_range") from error
            service.admit_delivery(
                operation_id, record_get=name is not None and request.method == "GET"
            )
            return data, filename, selected

        worker = asyncio.create_task(asyncio.to_thread(prepare))

        def retire(
            future: asyncio.Task[tuple[bytes, str, tuple[int, int] | None]],
        ) -> None:
            # Cancellation cannot terminate a blocking I/O call. Keep its bounded
            # slot until the worker actually exits, then discard its result.
            if not future.cancelled():
                future.exception()
            release_slot()

        try:
            try:
                async with asyncio.timeout(PREPARATION_TIMEOUT_SECONDS):
                    data, filename, selected = await asyncio.shield(worker)
            except TimeoutError as error:
                worker.add_done_callback(retire)
                handed_off = True
                raise HTTPException(504, "native_zip_preparation_timed_out") from error
            except HTTPException:
                raise
            except Exception as error:
                raise HTTPException(404) from error
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

            class Delivery(_TimedDelivery):
                async def __call__(
                    self, scope: Scope, receive: Receive, send: Send
                ) -> None:
                    try:
                        await super().__call__(scope, receive, send)
                    finally:
                        release_slot()

            if name is None:
                content = (
                    '<!doctype html><html><body><a href="'
                    + html.escape(filename, quote=True)
                    + '">'
                    + html.escape(filename)
                    + "</a></body></html>"
                ).encode()
                handed_off = True
                return Delivery(
                    iter((content,)) if request.method == "GET" else iter(()),
                    media_type="text/html",
                    headers={**headers, "Content-Length": str(len(content))},
                )
            start, end = selected if selected is not None else (0, len(data) - 1)
            headers.update(
                {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes"}
            )
            if selected:
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            if request.method == "HEAD":
                handed_off = True
                return Delivery(
                    iter(()),
                    media_type="application/zip",
                    headers=headers,
                    status_code=206 if selected else 200,
                )

            async def chunks() -> AsyncIterator[bytes]:
                for offset in range(start, end + 1, 64 * 1024):
                    yield data[offset : min(offset + 64 * 1024, end + 1)]

            response = Delivery(
                chunks(),
                media_type="application/zip",
                headers=headers,
                status_code=206 if selected else 200,
            )
            handed_off = True
            return response
        finally:
            if not handed_off:
                if worker.done():
                    release_slot()
                else:
                    worker.add_done_callback(retire)

    return router

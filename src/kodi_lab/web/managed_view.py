"""WebSocket transport for the managed broker's RFB lane."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import struct
from collections.abc import Callable
from contextlib import suppress
from uuid import UUID, uuid4

from starlette.websockets import WebSocket, WebSocketDisconnect

from kodi_lab.auth.service import AuthenticatedWebSession
from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.protocol import Refusal

CHUNK = 65536


async def stream_view(
    websocket: WebSocket,
    broker: BrokerClient,
    resolve: Callable[[str | None], AuthenticatedWebSession | None],
    cookie_name: str,
    same_origin: Callable[[WebSocket], bool],
    require_https: bool,
    *,
    generation: str = "omega",
) -> None:
    token = websocket.cookies.get(cookie_name)
    session = await asyncio.to_thread(resolve, token)
    protocols = websocket.scope.get("subprotocols", [])
    interactive = len(protocols) == 2
    if (
        session is None
        or not protocols
        or protocols[0] != "novnc"
        or len(protocols) > 2
        or (
            interactive
            and not hmac.compare_digest(protocols[1], "control." + session.csrf_token)
        )
        or not same_origin(websocket)
        or websocket.scope.get("query_string")
        or (require_https and websocket.url.scheme != "wss")
    ):
        await websocket.close(code=4403)
        return
    attempt = uuid4()
    writer: asyncio.StreamWriter | None = None
    accepted = False
    try:
        async with asyncio.timeout(4):
            value = (
                await asyncio.to_thread(
                    broker.request,
                    "admin",
                    "managed_status",
                    {"generation": generation} if generation != "omega" else {},
                )
            )["result"]
            if not isinstance(value, dict) or value.get("state") != "ready":
                raise ValueError("Kodi is not ready")
            target = UUID(value["view_target"])
            reader, writer = await asyncio.open_unix_connection(
                broker.path.parent / "display.sock"
            )
            challenge = (await reader.readexactly(64)).decode("ascii")
            writer.write(
                (
                    json.dumps(
                        {
                            "instance": str(target),
                            "owner": hashlib.sha256(
                                session.csrf_token.encode()
                            ).hexdigest(),
                            "attempt": str(attempt),
                            "mode": "interactive" if interactive else "view-only",
                            "rate": 10,
                            "challenge": challenge,
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            if await reader.readexactly(3) != b"OK\n":
                raise ValueError("View admission refused")
            await websocket.accept(subprotocol="novnc")
            accepted = True

        pipe = writer

        async def browser_to_broker() -> None:
            while True:
                frame = await websocket.receive_bytes()
                if not frame or len(frame) > CHUNK:
                    return
                pipe.write(struct.pack("!BI", 1, len(frame)) + frame)
                await asyncio.wait_for(pipe.drain(), 1)

        async def broker_to_browser() -> None:
            while data := await reader.read(CHUNK):
                await asyncio.wait_for(websocket.send_bytes(data), 2)

        async def renew() -> None:
            while True:
                current = await asyncio.to_thread(resolve, token)
                if current is None:
                    return
                pipe.write(struct.pack("!BI", 2, 0))
                await asyncio.wait_for(pipe.drain(), 1)
                await asyncio.sleep(0.4)

        tasks = [
            asyncio.create_task(run())
            for run in (
                browser_to_broker,
                broker_to_browser,
                renew,
            )
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (ValueError, Refusal, OSError, EOFError, WebSocketDisconnect, TimeoutError):
        pass
    finally:
        if writer:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1000 if accepted else 4403)

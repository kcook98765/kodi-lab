"""A durable renewal may finish after one second, before broker lease expiry."""

from __future__ import annotations

import asyncio
import struct
import time
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.web.novnc import PrototypeViewer, StartView, relay

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING, OWNER
from .view_attempt_fixture import view_attempt


@pytest.mark.parametrize("commit_seconds,expected_renewal", [(1.1, True), (1.8, False)])
def test_watchdog_waits_for_bounded_durable_commit_before_trusted_renewal(
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    commit_seconds: float,
    expected_renewal: bool,
) -> None:
    ownership = HumanViewerOwnership(database)
    viewer = PrototypeViewer(
        Path("/unused"),
        lambda _: BINDING,
        registry=LiveDisplaySessionRegistry(),
        ownership=ownership,
    )
    item = viewer.start(INSTANCE_ID, OWNER, StartView(attempt=view_attempt()))
    broker = str(uuid4())
    ownership.claim(INSTANCE_ID, item.attempt, OWNER, BINDING, "view-only", broker)
    began_commit, committed = Event(), Event()
    dialect = database.kw["bind"].dialect
    original_commit = dialect.do_commit

    def delayed_commit(connection: Any) -> None:
        if not began_commit.is_set():
            began_commit.set()
            time.sleep(commit_seconds)
            original_commit(connection)
            committed.set()
        else:
            original_commit(connection)

    monkeypatch.setattr(dialect, "do_commit", delayed_commit)

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 64 + b"OK\n")
        renewed = asyncio.Event()

        class Writer:
            closed = False

            def write(self, data: bytes) -> None:
                if data == struct.pack("!BI", 2, 0):
                    # A trusted heartbeat must never precede its durable lease.
                    assert committed.is_set()
                    renewed.set()

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                pass

        class Socket:
            closed = False

            async def accept(self, **kwargs: Any) -> None:
                assert kwargs == {"subprotocol": "novnc"}

            async def receive(self) -> dict[str, Any]:
                await renewed.wait()
                return {"type": "websocket.disconnect", "code": 1000}

            async def close(self, **kwargs: Any) -> None:
                self.closed = True

        writer, socket = Writer(), Socket()

        async def connect(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            return reader, writer

        monkeypatch.setattr(asyncio, "open_unix_connection", connect)
        await asyncio.wait_for(
            relay(cast(WebSocket, socket), viewer, item, lambda: True, lambda: True),
            3,
        )
        assert began_commit.is_set() and committed.is_set()
        assert renewed.is_set() is expected_renewal
        assert socket.closed and writer.closed and item.session.cancelled.is_set()
        assert not viewer.attachments and viewer.registry.active_count() == 0

    try:
        asyncio.run(run())
    finally:
        ownership.finish(INSTANCE_ID, item.attempt, broker, True)
    assert ownership.status(INSTANCE_ID) is None

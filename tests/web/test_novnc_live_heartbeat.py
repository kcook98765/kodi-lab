"""Established viewing uses live heartbeats without durable SQLite renewals."""

from __future__ import annotations

import asyncio
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import novnc_control
from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.storage.models import NovncSessionRow
from kodi_lab.web import novnc

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING, OWNER
from .view_attempt_fixture import view_attempt


@pytest.mark.parametrize("mode", ["view-only", "interactive"])
def test_five_minutes_of_authorized_heartbeats_never_wait_for_slow_commit(
    database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    ownership = HumanViewerOwnership(database)
    # Broker reads use independent sessions, as in the separately running role.
    broker_ownership = HumanViewerOwnership(sessionmaker(bind=database.kw["bind"]))
    viewer = novnc.PrototypeViewer(
        Path("/unused"),
        lambda _: BINDING,
        registry=LiveDisplaySessionRegistry(),
        ownership=ownership,
    )
    item = viewer.start(
        INSTANCE_ID, OWNER, novnc.StartView(attempt=view_attempt()), mode=mode
    )
    broker = str(uuid4())
    ownership.claim(INSTANCE_ID, item.attempt, OWNER, BINDING, mode, broker)
    ownership.started(INSTANCE_ID, item.attempt, broker, "worker")
    with database() as db:
        row = db.get(NovncSessionRow, str(INSTANCE_ID))
        assert row and row.expires_at == row.absolute_expires_at == item.deadline
        durable_before = (row.expires_at, row.absolute_expires_at, row.exec_id)

    ticks = heartbeats = commits = 0
    statements: list[str] = []
    wall_start = datetime.now(UTC)
    monotonic_start = time.monotonic()
    real_sleep = asyncio.sleep
    timestamp = novnc_control.timestamp
    engine = database.kw["bind"]
    original_commit = engine.dialect.do_commit

    def sql(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.split()[0].upper() in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "REPLACE",
            "BEGIN",
            "COMMIT",
            "CREATE",
            "ALTER",
            "DROP",
        }:
            statements.append(statement)
            assert ticks == 1200, "steady-state viewing tried to write ownership"

    def slow_commit(connection: Any) -> None:
        nonlocal commits
        commits += 1
        # A real commit remains slow. Only final revocation may reach it; any
        # old routine renewal would consume the unchanged watchdog budget.
        assert ticks == 1200, "steady-state viewing tried to commit"
        time.sleep(1.5)
        original_commit(connection)

    async def run() -> None:
        nonlocal ticks, heartbeats
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 64 + b"OK\n")
        pulse, ending = asyncio.Event(), asyncio.Event()

        async def tick(seconds: float) -> None:
            nonlocal ticks
            assert seconds == 0.25
            ticks += 1
            if ticks == 1200:
                assert heartbeats == 1200 and commits == 0 and statements == []
                with database() as db:
                    row = db.get(NovncSessionRow, str(INSTANCE_ID))
                    assert row and durable_before == (
                        row.expires_at,
                        row.absolute_expires_at,
                        row.exec_id,
                    )
                ending.set()
                pulse.set()
                await asyncio.Event().wait()
            await real_sleep(0)

        class Writer:
            closed = False

            def write(self, data: bytes) -> None:
                nonlocal heartbeats
                if data == struct.pack("!BI", 2, 0):
                    assert commits == 0
                    assert broker_ownership.valid(INSTANCE_ID, item.attempt, broker)
                    broker_ownership.expire_pending()
                    heartbeats += 1
                    pulse.set()

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
                if mode == "view-only":
                    await ending.wait()
                else:
                    await pulse.wait()
                    pulse.clear()
                    if not ending.is_set():
                        return {"type": "websocket.receive", "text": "control-alive"}
                return {"type": "websocket.disconnect", "code": 1000}

            async def close(self, **kwargs: Any) -> None:
                self.closed = True

        writer, socket = Writer(), Socket()

        async def connect(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            return reader, writer

        # Advance only viewer/ownership clocks. I/O and the real SQLite commit
        # delay keep real deadlines; asyncio's event-loop clock is untouched.
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "open_unix_connection", connect)
            patch.setattr(asyncio, "sleep", tick)
            patch.setattr(
                novnc,
                "time",
                SimpleNamespace(monotonic=lambda: monotonic_start + ticks * 0.25),
            )
            patch.setattr(
                novnc_control,
                "timestamp",
                lambda: timestamp(wall_start + timedelta(seconds=ticks * 0.25)),
            )
            patch.setattr(engine.dialect, "do_commit", slow_commit)
            event.listen(engine, "before_cursor_execute", sql)
            try:
                await asyncio.wait_for(
                    novnc.relay(
                        cast(WebSocket, socket),
                        viewer,
                        item,
                        lambda: not ending.is_set(),
                        lambda: True,
                    ),
                    20,
                )
            finally:
                event.remove(engine, "before_cursor_execute", sql)
        assert ticks == heartbeats == 1200
        assert commits == 1  # final revocation, after the connection is closed
        assert socket.closed and writer.closed and item.session.cancelled.is_set()
        assert not viewer.attachments and viewer.registry.active_count() == 0

    try:
        asyncio.run(run())
        assert ownership.status(INSTANCE_ID).state == "revoking"  # type: ignore[union-attr]
    finally:
        ownership.finish(INSTANCE_ID, item.attempt, broker, True)
    assert ownership.status(INSTANCE_ID) is None

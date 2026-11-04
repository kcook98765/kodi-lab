from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.viewer_diagnostics import LOGGER
from kodi_lab.web.novnc import PrototypeViewer, StartView, relay

from .conftest import INSTANCE_ID
from .view_attempt_fixture import view_attempt


@pytest.mark.parametrize(
    "outcome,category",
    [
        ("stop", "browser_disconnect"),
        ("early_close", "browser_disconnect"),
        ("helper_eof", "broker_eof"),
        ("send_disconnect", "browser_disconnect"),
        ("auth", "authorization_rejected"),
        ("binding", "binding_changed"),
        ("ownership", "ownership_rejected"),
        ("watchdog_timeout", "timeout"),
        ("watchdog_error", "internal_error"),
    ],
)
def test_relay_terminal_evidence_preserves_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    category: str,
) -> None:
    monkeypatch.setattr(logging.getLogger(LOGGER), "propagate", True)
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def run() -> None:
        binding = ViewBinding("a" * 64, "image-secret", "start", (str(uuid4()), 1, 1))
        service = PrototypeViewer(
            Path("/not-used"), lambda _: binding, registry=LiveDisplaySessionRegistry()
        )
        item = service.start(
            INSTANCE_ID, "owner-secret", StartView(attempt=view_attempt())
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 64 + b"OK\n")
        if outcome in {"stop", "send_disconnect"}:
            reader.feed_data(b"RFB-payload-secret")
        elif outcome == "helper_eof":
            reader.feed_eof()
        sent = asyncio.Event()

        class Socket:
            closed = False

            async def accept(self, **kwargs: Any) -> None:
                assert kwargs == {"subprotocol": "novnc"}

            async def receive(self) -> dict[str, Any]:
                if outcome == "stop":
                    await sent.wait()
                elif outcome != "early_close":
                    await asyncio.Event().wait()
                return {
                    "type": "websocket.disconnect",
                    "code": 1000 if outcome == "stop" else 1006,
                    "reason": "reason-secret",
                }

            async def send_bytes(self, data: bytes) -> None:
                assert data == b"RFB-payload-secret"
                if outcome == "send_disconnect":
                    raise WebSocketDisconnect(code=1006, reason="exception-secret")
                sent.set()

            async def close(self, **kwargs: Any) -> None:
                assert kwargs == {"code": 1000}
                self.closed = True

        class Writer:
            closed = False

            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                pass

        writer, socket = Writer(), Socket()

        async def connect(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
            return reader, writer

        monkeypatch.setattr(asyncio, "open_unix_connection", connect)
        if outcome == "binding":
            # Initial check still succeeds; the watchdog observes a replacement.
            checks = 0

            def binding_check(*args: Any) -> ViewBinding:
                nonlocal checks
                checks += 1
                return (
                    binding if checks == 1 else ViewBinding("replacement", "", "", ())
                )

            monkeypatch.setattr(service, "binding", binding_check)
        if outcome == "ownership":
            from types import SimpleNamespace

            service.ownership = cast(Any, SimpleNamespace(renew=lambda *_: False))
            # Cleanup is observed separately from ownership's own tested DB release.
            monkeypatch.setattr(service, "end", lambda _: None)
        if outcome.startswith("watchdog_"):
            original = service.checked
            calls = 0

            async def checked(callback: Any, **kwargs: Any) -> Any:
                nonlocal calls
                calls += 1
                if calls == 2:
                    error = (
                        TimeoutError if outcome == "watchdog_timeout" else ValueError
                    )
                    raise error("exception-secret")
                return await original(callback, **kwargs)

            monkeypatch.setattr(service, "checked", checked)
        await asyncio.wait_for(
            relay(
                cast(WebSocket, socket),
                service,
                item,
                lambda: True,
                lambda: outcome != "auth",
            ),
            2,
        )
        assert socket.closed and writer.closed and item.session.cancelled.is_set()
        if outcome != "ownership":
            assert not service.attachments and service.registry.active_count() == 0
        events = [
            json.loads(record.message)
            for record in caplog.records
            if record.name == LOGGER
        ]
        assert [event["event"] for event in events] == ["web_accepted", "web_ended"]
        terminal = events[-1]
        assert terminal["attempt"] == str(item.attempt)
        assert terminal["instance"] == str(INSTANCE_ID)
        assert terminal["accepted"] is True and terminal["phase"] == "streaming"
        lane = (
            "incoming"
            if outcome in {"stop", "early_close"}
            else "outgoing"
            if outcome in {"helper_eof", "send_disconnect"}
            else "watchdog"
        )
        assert terminal[lane] == category
        if lane == "incoming" or outcome == "send_disconnect":
            assert terminal["ws_code"] == (1000 if outcome == "stop" else 1006)
        assert "secret" not in json.dumps(events)

    asyncio.run(run())

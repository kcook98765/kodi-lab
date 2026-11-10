"""Foreground-only input and verified single-successor view-only handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import WebSocket
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc_service import HumanDisplayService
from kodi_lab.viewer_diagnostics import LOGGER
from kodi_lab.web.novnc import (
    ChangeMode,
    ForegroundControlLease,
    PrototypeViewer,
    StartView,
    relay,
)

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING, OWNER, mutation
from .view_attempt_fixture import view_attempt


def test_visibility_handoff_races_and_input_gates_in_node() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("novnc_tab_handoff_browser.cjs")),
            str(root / "src/kodi_lab/web/static/novnc-phase1.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_foreground_lease_cannot_revive_expired_control_or_extend_view_only() -> None:
    lease = ForegroundControlLease("interactive")
    assert lease.valid() and lease.renew()
    lease.deadline = 0
    assert not lease.valid() and not lease.renew()
    assert lease.deadline == 0
    view = ForegroundControlLease("view-only")
    view.deadline = 0
    assert view.valid() and not view.renew()


@pytest.mark.parametrize("proof", [True, False])
def test_release_waits_for_native_proof_and_preserves_deadline_and_capacity(
    database: sessionmaker[Session],
    tmp_path: Path,
    proof: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = HumanViewerOwnership(database)
    registry = LiveDisplaySessionRegistry()
    binding = BINDING
    viewer = PrototypeViewer(
        tmp_path / "unused.sock",
        lambda _: binding,
        registry=registry,
        ownership=ownership,
    )
    first = viewer.start(INSTANCE_ID, OWNER, StartView(attempt=view_attempt()))
    active = viewer.handoff(
        INSTANCE_ID,
        OWNER,
        ChangeMode(attempt=first.attempt, next_attempt=view_attempt()),
        "interactive",
    )
    ticket = active.ticket
    viewer.consume(INSTANCE_ID, OWNER, ticket)
    broker = str(uuid4())
    ownership.claim(INSTANCE_ID, active.attempt, OWNER, binding, "interactive", broker)
    entered, permit = Event(), Event()
    original = ownership.wait_released

    def wait(instance: Any) -> None:
        entered.set()
        assert permit.wait(2)
        original(instance, timeout=0)

    monkeypatch.setattr(ownership, "wait_released", wait)
    successor = view_attempt()
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(
            viewer.handoff,
            INSTANCE_ID,
            OWNER,
            ChangeMode(attempt=active.attempt, next_attempt=successor),
            "view-only",
        )
        assert entered.wait(2)
        assert active.session.cancelled.is_set()
        assert ownership.status(INSTANCE_ID).state == "revoking"  # type: ignore[union-attr]
        assert registry.active_count() == 1
        with pytest.raises(IntegrityError):
            mutation(database)
        # Broker finish(True) is permitted only after worker death and the
        # fixed native --probe-control neutral-key/button proof. False keeps
        # the durable interactive fence and cannot issue a successor ticket.
        ownership.finish(INSTANCE_ID, active.attempt, broker, proof)
        permit.set()
        if proof:
            result = work.result(timeout=2)
            assert result.mode == "view-only" and result.deadline == first.deadline
            assert registry.active_count() == 1
            assert list(viewer.attachments) == [successor]
            with pytest.raises(ValueError):
                viewer.consume(INSTANCE_ID, OWNER, ticket)
            mutation(database)
            viewer.stop(OWNER, successor)
            assert registry.active_count() == 0
        else:
            with pytest.raises(ValueError, match="unproven"):
                work.result(timeout=2)
            assert successor not in viewer.attachments
            assert ownership.status(INSTANCE_ID).state == "blocked"  # type: ignore[union-attr]
            with pytest.raises(IntegrityError):
                mutation(database)
    viewer.close()


@pytest.mark.parametrize(
    "scenario,terminal",
    [
        ("suspended", "control_lease_expired"),
        ("late-pulse", "control_lease_expired"),
        ("late-input", "control_lease_expired"),
        ("pulse", "browser_disconnect"),
        ("view-pulse", "invalid_browser_message"),
        ("view-hidden", "browser_disconnect"),
        ("view-expired", "session_ended"),
    ],
)
def test_relay_foreground_lease_is_independent_of_visibility_handlers(
    scenario: str,
    terminal: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    monkeypatch.setattr(logging.getLogger(LOGGER), "propagate", True)
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def run() -> None:
        binding = BINDING
        viewer = PrototypeViewer(
            Path("/unused"), lambda _: binding, registry=LiveDisplaySessionRegistry()
        )
        item = viewer.start(INSTANCE_ID, OWNER, StartView(attempt=view_attempt()))
        item.mode = "view-only" if scenario.startswith("view-") else "interactive"
        monkeypatch.setattr(viewer, "binding", lambda *_: binding)
        lease = ForegroundControlLease(item.mode)
        monkeypatch.setattr(
            "kodi_lab.web.novnc.ForegroundControlLease", lambda _: lease
        )
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 64 + b"OK\n")
        writes: list[bytes] = []
        session_current = True

        class Writer:
            def write(self, data: bytes) -> None:
                writes.append(data)

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        class Socket:
            async def accept(self, **_: Any) -> None:
                pass

            async def receive(self) -> dict[str, Any]:
                nonlocal session_current
                if scenario == "view-expired":
                    session_current = False
                    await asyncio.Event().wait()
                if scenario in {"suspended", "late-pulse", "late-input", "view-hidden"}:
                    lease.deadline = 0
                if scenario == "suspended":
                    await asyncio.Event().wait()
                if scenario == "late-input":
                    return {"type": "websocket.receive", "bytes": b"key-down-secret"}
                if scenario != "view-hidden" and not hasattr(self, "sent"):
                    self.sent = True
                    return {"type": "websocket.receive", "text": "control-alive"}
                return {"type": "websocket.disconnect", "code": 1000}

            async def send_bytes(self, _: bytes) -> None:
                pass

            async def close(self, **_: Any) -> None:
                pass

        async def connect(*_: Any, **__: Any) -> tuple[Any, Any]:
            return reader, Writer()

        monkeypatch.setattr(asyncio, "open_unix_connection", connect)
        await asyncio.wait_for(
            relay(
                cast(WebSocket, Socket()),
                viewer,
                item,
                lambda: session_current,
                lambda: True,
            ),
            timeout=2,
        )
        assert not viewer.attachments and viewer.registry.active_count() == 0
        assert item.session.cancelled.is_set()
        assert not any(b"key-down-secret" in data for data in writes)
        assert not any(b"control-alive" in data for data in writes)
        assert all(data == struct.pack("!BI", 2, 0) for data in writes[1:]), (
            "only the trusted watchdog writes broker lease envelopes"
        )
        terminal_event = json.loads(caplog.records[-1].message)
        assert terminal in (terminal_event["incoming"], terminal_event["watchdog"])

    asyncio.run(run())


@pytest.mark.parametrize(
    "worker_running,neutral_code", [(True, 0), (False, 1), (False, 0)]
)
def test_existing_broker_proof_requires_worker_death_and_native_neutral_probe(
    worker_running: bool,
    neutral_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[Any] = []

    class Docker:
        def inspect_container(self, container: str) -> dict[str, Any]:
            assert container == BINDING.container
            return {"Id": container, "State": {"Running": True, "Paused": False}}

        def exec_inspect(self, exec_id: str) -> dict[str, Any]:
            return {
                "Running": worker_running if exec_id == "worker" else False,
                "ExitCode": neutral_code,
            }

        def exec_create(self, container: str, **kwargs: Any) -> dict[str, str]:
            assert container == BINDING.container
            commands.append(kwargs["cmd"])
            return {"Id": "probe"}

        def exec_start(self, exec_id: str, **_: Any) -> None:
            assert exec_id == "probe"

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "kodi_lab.broker.novnc_service.docker.APIClient", lambda **_: Docker()
    )
    service = HumanDisplayService.__new__(HumanDisplayService)
    assert service.proof(BINDING.container, "worker", True) is (
        not worker_running and neutral_code == 0
    )
    assert commands == (
        []
        if worker_running
        else [
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                "/usr/local/lib/kodi-lab-novnc-view.py",
                "--probe-control",
            ]
        ]
    )


@pytest.mark.skipif(not os.getenv("KODI_LAB_BROWSER_TOOLS"), reason="Chromium opt-in")
def test_tab_handoff_in_chromium() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("novnc_tab_handoff_chromium.cjs")),
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHROMIUM PASS" in result.stdout

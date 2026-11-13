"""Real socket readiness with deterministic elapsed time, without native Kodi."""

from __future__ import annotations

import selectors
import socket
import struct
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from kodi_lab.broker import novnc as transport

from .test_novnc_transport import DockerDouble, make_tunnel


@pytest.mark.parametrize(
    "case",
    [
        "progress",
        "silent",
        "delayed_input",
        "unrelated_read",
        "fresh_heartbeat",
        "split_heartbeat",
        "disconnect",
        "cancel",
        "backlog",
    ],
)
def test_buffered_heartbeats_use_receive_time_and_make_bounded_progress(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    stopped = Event()
    client = DockerDouble()
    web, broker = socket.socketpair()
    heartbeat = struct.pack("!BI", 2, 0)
    mixed = case in {"delayed_input", "unrelated_read", "fresh_heartbeat"}
    burst = b"".join(
        (struct.pack("!BI", 1, 1) + bytes([n]) if mixed else b"") + heartbeat
        for n in range(8)
    )
    if case == "backlog":
        burst *= 2
    web.sendall(heartbeat[:4] if case == "split_heartbeat" else burst)
    if case == "disconnect":
        web.shutdown(socket.SHUT_WR)
    if case == "backlog":
        monkeypatch.setattr(transport, "BACKLOG", len(burst) - 1)
    challenge = b"KLVNCARM" + b"n" * 32
    client.helper.sendall(struct.pack("!B3sI", 1, b"\0\0\0", 40) + challenge)
    real_selector = selectors.DefaultSelector()
    reads: list[float] = []
    turns: list[tuple[float, float | None]] = []
    injected = False

    class Selector:
        def register(self, *args: Any, **kwargs: Any) -> selectors.SelectorKey:
            return real_selector.register(*args, **kwargs)

        def modify(self, *args: Any, **kwargs: Any) -> selectors.SelectorKey:
            return real_selector.modify(*args, **kwargs)

        def select(
            self, timeout: float | None = None
        ) -> list[tuple[selectors.SelectorKey, int]]:
            nonlocal injected
            # Read/write readiness is real; only an idle wait's elapsed time is
            # simulated. This detects busy spins as well as artificial waits.
            assert len(turns) < 500, "buffered/partial input stopped making progress"
            if not injected and clock.now >= 100.5:
                extra = {
                    "unrelated_read": b"\x01",  # Incomplete input, not a heartbeat.
                    "fresh_heartbeat": heartbeat,
                    "split_heartbeat": heartbeat[4:],
                }.get(case)
                if extra is not None:
                    web.sendall(extra)
                    injected = True
            if reads:
                turns.append((clock.now, timeout))
            ready = real_selector.select(0)
            if any(
                key.fileobj is broker and mask & selectors.EVENT_READ
                for key, mask in ready
            ):
                reads.append(clock.now)
                if case == "cancel":
                    stopped.set()
            if not ready:
                assert timeout is not None and timeout >= 0
                clock.sleep(timeout)
            return ready

        def close(self) -> None:
            real_selector.close()

    def valid() -> bool:
        if mixed and reads:
            # Each check remains individually well below the two-second lease.
            clock.sleep(0.15)
        return True

    monkeypatch.setattr(transport, "time", clock)
    monkeypatch.setattr(
        transport,
        "selectors",
        SimpleNamespace(
            DefaultSelector=Selector,
            EVENT_READ=selectors.EVENT_READ,
            EVENT_WRITE=selectors.EVENT_WRITE,
        ),
    )
    tunnel = make_tunnel(client, valid)
    tunnel.request = replace(tunnel.request, mode="interactive")
    try:
        if case == "backlog":
            with pytest.raises(ValueError, match="display backlog exceeded"):
                tunnel.run(broker, stopped)
        else:
            tunnel.run(broker, stopped)
    finally:
        web.close()
        broker.close()
        real_selector.close()
    assert tunnel.result.terminated and tunnel.result.done.is_set()
    if case in {"disconnect", "cancel", "backlog"}:
        assert (
            tunnel.result.reason
            == {
                "disconnect": "disconnect",
                "cancel": "cancelled",
                "backlog": "backlog_exceeded",
            }[case]
        )
        assert clock.now - reads[0] < 0.1
        return
    assert tunnel.result.reason == "web_lease_expired"
    if case == "progress":
        # Eight heartbeats already in userspace need no new socket event. They
        # must not accumulate eight blocking selector waits on quiet sockets.
        assert len(turns) >= 8
        assert turns[7][0] - reads[0] < 0.05
    else:
        if case == "fresh_heartbeat":
            assert injected and len(reads) == 2
            last_heartbeat = reads[1]
        else:
            last_heartbeat = reads[0]
        if case in {"unrelated_read", "split_heartbeat"}:
            assert injected and len(reads) == 2
        # A genuinely newer heartbeat receives its own lease even if older input
        # remains buffered. Neither unrelated reads nor a late suffix can refresh
        # earlier heartbeat bytes. One idle tick is the existing detection margin.
        assert last_heartbeat + transport.WEB_LEASE_SECONDS <= clock.now
        assert clock.now <= last_heartbeat + transport.WEB_LEASE_SECONDS + 0.051

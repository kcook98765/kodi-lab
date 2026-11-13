"""Coalesced interactive input must yield to helper I/O and cancellation."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import replace
from threading import Event, Thread

import pytest

from .test_novnc_transport import DockerDouble, make_tunnel


@pytest.mark.parametrize("ending", ["drain", "cancel", "revoke"])
def test_coalesced_input_yields_between_fresh_authority_checks(ending: str) -> None:
    client = DockerDouble()
    stopped, checking, proceed = Event(), Event(), Event()
    armed = Event()
    checks = 0

    def valid() -> bool:
        nonlocal checks
        if armed.is_set():
            checks += 1
            if checks == 1:
                checking.set()
                assert proceed.wait(2)
            time.sleep(0.03)
            return ending != "revoke" or checks < 3
        return True

    tunnel = make_tunnel(client, valid)
    tunnel.request = replace(tunnel.request, mode="interactive")
    web, broker = socket.socketpair()
    failures: list[Exception] = []

    def run() -> None:
        try:
            tunnel.run(broker, stopped)
        except Exception as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        client.helper.settimeout(1)
        challenge = b"KLVNCARM" + b"n" * 32
        client.helper.sendall(struct.pack("!B3sI", 1, b"\0\0\0", 40) + challenge)
        assert client.helper.recv(37) == struct.pack("!BI", 4, 32) + b"n" * 32
        armed.set()
        # One write/read contains forty separately authorized input envelopes.
        frames = [struct.pack("!BI", 1, 1) + bytes([n]) for n in range(40)]
        web.sendall(b"".join(frames))
        assert checking.wait(1)
        if ending == "cancel":
            stopped.set()
        proceed.set()
        if ending == "drain":
            # The old loop withholds every byte until all 40 checks finish.
            client.helper.settimeout(0.6)
            received: list[bytes] = []
            renewals = 0

            def read_exact(size: int) -> bytes:
                data = bytearray()
                while len(data) < size:
                    part = client.helper.recv(size - len(data))
                    assert part
                    data.extend(part)
                return bytes(data)

            while len(received) < len(frames):
                kind, size = struct.unpack("!BI", read_exact(5))
                if kind == 2:
                    assert size == 0
                    renewals += 1
                else:
                    assert kind == 1 and size == 1
                    received.append(struct.pack("!BI", kind, size) + read_exact(size))
            assert received == frames
            assert renewals >= 1
        else:
            worker.join(0.6)
            assert not worker.is_alive()
            assert checks == (1 if ending == "cancel" else 3)
            assert tunnel.result.reason == (
                "cancelled" if ending == "cancel" else "input_revoked"
            )
            assert tunnel.result.bytes_up == (1 if ending == "cancel" else 2)
    finally:
        proceed.set()
        stopped.set()
        worker.join(3)
        web.close()
        broker.close()
    assert not worker.is_alive() and not failures
    assert tunnel.result.terminated and tunnel.result.done.is_set()

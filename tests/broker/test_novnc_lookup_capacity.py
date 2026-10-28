"""Native display traffic must leave bounded capacity for fresh authority reads."""

from __future__ import annotations

import json
import os
import socket
from threading import Event, Lock, Thread
from typing import cast
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.novnc import (
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
    ViewResult,
)
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.web.novnc_optin import resolve_binding


@pytest.mark.parametrize("displays", [1, 2])
def test_status_and_watchdog_resolve_while_display_workers_are_active(
    socket_config: SocketPathConfig, displays: int
) -> None:
    binding = ViewBinding("a" * 64, "sha256:" + "b" * 64, "start", ("revision", 1, 2))
    started = [Event() for _ in range(displays)]
    both_lookups = Event()
    release_lookups = Event()
    lock = Lock()
    admitted: list[UUID] = []
    lookups = 0
    replies: list[ViewBinding] = []
    errors: list[Exception] = []

    class Tunnel:
        result = ViewResult(exec_id="fixture", terminated=True)
        finished = None

        def __init__(self, index: int) -> None:
            self.index = index

        def run(self, peer: socket.socket, stopped: Event) -> None:
            started[self.index].set()
            assert stopped.wait(5)

    def admit(request: ViewRequest) -> DockerViewTunnel:
        admitted.append(request.instance)
        return cast(DockerViewTunnel, Tunnel(len(admitted) - 1))

    def resolve(_instance: UUID) -> ViewBinding:
        nonlocal lookups
        with lock:
            lookups += 1
            if lookups == 2:
                both_lookups.set()
        assert release_lookups.wait(3)
        return binding

    lane = DisplayLane(socket_config, os.getuid(), admit, resolve=resolve)
    lane.start()
    peers: list[socket.socket] = []
    threads: list[Thread] = []

    def read_authority() -> None:
        try:
            replies.append(resolve_binding(socket_config.socket_path, uuid4()))
        except Exception as error:
            errors.append(error)

    try:
        for index in range(displays):
            peer = socket.socket(socket.AF_UNIX)
            peer.settimeout(1)
            peers.append(peer)
            peer.connect(str(socket_config.socket_path))
            challenge = peer.recv(64).decode()
            peer.sendall(
                json.dumps(
                    {
                        "instance": str(uuid4()),
                        "owner": "c" * 64,
                        "rate": 2,
                        "challenge": challenge,
                    }
                ).encode()
                + b"\n"
            )
            assert peer.recv(3) == b"OK\n"
            assert started[index].wait(1)
        for _ in range(2):
            thread = Thread(target=read_authority)
            threads.append(thread)
            thread.start()
        assert both_lookups.wait(1), (
            "status and watchdog authority reads competed with established displays"
        )
        # Two worker watchdogs may overlap a page's status poll. The third read
        # waits in the bounded socket backlog rather than losing authority.
        queued = socket.socket(socket.AF_UNIX)
        queued.settimeout(0.1)
        peers.append(queued)
        queued.connect(str(socket_config.socket_path))
        with pytest.raises(TimeoutError):
            queued.recv(64)
        release_lookups.set()
        queued.settimeout(1)
        challenge = queued.recv(64).decode()
        queued.sendall(
            json.dumps({"resolve": str(uuid4()), "challenge": challenge}).encode()
            + b"\n"
        )
        with queued.makefile("rb") as response:
            value = json.loads(response.readline())
        value["identity"] = tuple(value["identity"])
        replies.append(ViewBinding(**value))
        for thread in threads:
            thread.join(2)
        assert not errors and replies == [binding, binding, binding]
        assert len(admitted) == displays
        if displays == 2:
            # More read capacity must never admit a third native display worker.
            peer = socket.socket(socket.AF_UNIX)
            peer.settimeout(1)
            peers.append(peer)
            peer.connect(str(socket_config.socket_path))
            challenge = peer.recv(64).decode()
            peer.sendall(
                json.dumps(
                    {
                        "instance": str(uuid4()),
                        "owner": "c" * 64,
                        "rate": 2,
                        "challenge": challenge,
                    }
                ).encode()
                + b"\n"
            )
            assert peer.recv(3) == b""
            assert len(admitted) == 2
    finally:
        release_lookups.set()
        for thread in threads:
            thread.join(2)
        for peer in peers:
            peer.close()
        lane.close()
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("failure", ["eof", "invalid", "timeout", "challenge"])
def test_stalled_handshakes_remain_bounded_and_release_capacity(
    socket_config: SocketPathConfig, failure: str
) -> None:
    binding = ViewBinding("a" * 64, "sha256:" + "b" * 64, "start", ("revision", 1, 2))
    lane = DisplayLane(
        socket_config,
        os.getuid(),
        lambda _: pytest.fail("No display requested"),
        resolve=lambda _: binding,
    )
    lane.start()
    peers: list[socket.socket] = []
    try:
        for _ in range(2):
            peer = socket.socket(socket.AF_UNIX)
            peer.settimeout(1)
            peers.append(peer)
            peer.connect(str(socket_config.socket_path))
            assert len(peer.recv(64)) == 64
        extra = socket.socket(socket.AF_UNIX)
        extra.settimeout(0.1)
        peers.append(extra)
        extra.connect(str(socket_config.socket_path))
        with pytest.raises(TimeoutError):
            extra.recv(64)
        extra.close()
        for peer in peers[:2]:
            if failure == "invalid":
                peer.sendall(b"{}\n")
                assert peer.recv(1) == b""
            elif failure == "challenge":
                peer.sendall(
                    json.dumps({"resolve": str(uuid4()), "challenge": "wrong"}).encode()
                    + b"\n"
                )
                assert peer.recv(1) == b""
            elif failure == "timeout":
                peer.settimeout(2)
                assert peer.recv(1) == b""
            peer.close()
        # Wait for the exact finished handshakes, then require normal recovery.
        for thread in list(lane.threads):
            thread.join(2)
        assert resolve_binding(socket_config.socket_path, uuid4()) == binding
    finally:
        for peer in peers:
            peer.close()
        lane.close()


def test_shutdown_reaps_pending_handshakes(socket_config: SocketPathConfig) -> None:
    lane = DisplayLane(
        socket_config, os.getuid(), lambda _: pytest.fail("No display requested")
    )
    lane.start()
    peers: list[socket.socket] = []
    try:
        for _ in range(2):
            peer = socket.socket(socket.AF_UNIX)
            peer.settimeout(2)
            peers.append(peer)
            peer.connect(str(socket_config.socket_path))
            assert len(peer.recv(64)) == 64
        lane.close()
        assert all(not thread.is_alive() for thread in lane.threads)
        assert lane.accept_thread is not None and not lane.accept_thread.is_alive()
        assert lane.reaper is not None and not lane.reaper.is_alive()
        assert not lane.active and not lane.retained
        assert not socket_config.socket_path.exists()
        assert all(peer.recv(1) == b"" for peer in peers)
    finally:
        for peer in peers:
            peer.close()
        lane.close()

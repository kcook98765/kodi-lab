from __future__ import annotations

import json
import os
import socket
import struct
import time
from dataclasses import replace
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.broker import novnc as transport
from kodi_lab.broker.authorization import ExactUidAuthorizationPolicy
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.novnc import (
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
)
from kodi_lab.broker.server import BrokerServer
from kodi_lab.broker.socket_path import SocketPathConfig


class Attached:
    def __init__(self, wire: socket.socket) -> None:
        self._sock = wire

    def close(self) -> None:
        self._sock.close()


class DockerDouble:
    def __init__(self) -> None:
        self.wire, self.helper = socket.socketpair()
        self.calls: dict[str, Any] = {}

    def exec_create(self, container: str, **kwargs: Any) -> dict[str, str]:
        self.calls = {"container": container, **kwargs}
        return {"Id": "exec-test"}

    def exec_start(self, *args: Any, **kwargs: Any) -> Attached:
        assert kwargs == {"socket": True, "tty": False}
        return Attached(self.wire)

    def exec_inspect(self, _: str) -> dict[str, bool]:
        return {"Running": False}

    def close(self) -> None:
        self.helper.close()


def make_tunnel(client: DockerDouble, valid: Any = lambda: True) -> DockerViewTunnel:
    return DockerViewTunnel(
        ViewBinding("a" * 64, "sha256:" + "b" * 64, "start", ()),
        ViewRequest(uuid4(), "c" * 64, 10),
        valid,
        client=client,
    )


def test_fragmented_docker_headers_and_trusted_lease_separation() -> None:
    client = DockerDouble()
    tunnel = make_tunnel(client)
    web, broker = socket.socketpair()
    thread = Thread(target=tunnel.run, args=(broker, Event()))
    thread.start()
    try:
        client.helper.settimeout(1)
        web.settimeout(1)
        assert client.helper.recv(5) == struct.pack("!BI", 2, 0)
        # Browser bytes that look like renewal are still enclosed as RFB data.
        forged = struct.pack("!BI", 2, 0)
        web.sendall(struct.pack("!BI", 1, len(forged)) + forged)
        assert client.helper.recv(10) == struct.pack("!BI", 1, 5) + forged
        framed = struct.pack("!B3sI", 1, b"\0\0\0", 12) + b"RFB 003.008\n"
        for part in (framed[:3], framed[3:9], framed[9:]):
            client.helper.sendall(part)
        assert web.recv(12) == b"RFB 003.008\n"
        assert client.calls["user"] == "1000:1000"
        assert client.calls["privileged"] is False
        assert client.calls["tty"] is False
        assert client.calls["cmd"][:3] == ["/usr/bin/python3", "-I", "-B"]
    finally:
        web.close()
        thread.join(2)
        broker.close()
    assert not thread.is_alive() and tunnel.result.terminated


@pytest.mark.parametrize("stream,size", [(2, 1), (3, 1), (1, 1048577)])
def test_stderr_unknown_stream_and_oversized_docker_chunks_fail_closed(
    stream: int,
    size: int,
) -> None:
    client = DockerDouble()
    tunnel = make_tunnel(client)
    web, broker = socket.socketpair()
    failures: list[Exception] = []

    def run() -> None:
        try:
            tunnel.run(broker, Event())
        except Exception as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    client.helper.sendall(struct.pack("!B3sI", stream, b"\0\0\0", size) + b"x")
    thread.join(2)
    web.close()
    broker.close()
    assert not thread.is_alive() and len(failures) == 1
    assert tunnel.result.terminated


def test_sequential_broker_remains_available_during_stalled_admission(
    socket_config: SocketPathConfig,
) -> None:
    lane_config = replace(
        socket_config, socket_path=socket_config.runtime_directory / "view.sock"
    )
    lane = DisplayLane(lane_config, os.getuid(), lambda _: pytest.fail("not admitted"))
    server = BrokerServer(
        socket_config=socket_config,
        authorization=ExactUidAuthorizationPolicy(os.getuid()),
        display_lane=lane,
    )
    stop = Event()
    server.open()
    thread = Thread(target=server.serve_forever, args=(stop,))
    thread.start()
    peers = []
    try:
        deadline = time.monotonic() + 1
        while not lane_config.socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        for _ in range(2):
            peer = socket.socket(socket.AF_UNIX)
            peer.connect(str(lane_config.socket_path))
            assert len(peer.recv(64)) == 64
            peers.append(peer)
        began = time.monotonic()
        for _ in range(20):
            assert BrokerClient(socket_config.socket_path).ping().status == "healthy"
        assert time.monotonic() - began < 0.5
    finally:
        for peer in peers:
            peer.close()
        stop.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not lane_config.socket_path.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"challenge": "wrong"},
        {"rate": True},
        {"rate": 30},
        {"command": "shell"},
        {"owner": "bad"},
    ],
)
def test_closed_admission(change: dict[str, Any]) -> None:
    value = {
        "challenge": "ticket",
        "rate": 10,
        "instance": str(uuid4()),
        "owner": "a" * 64,
    }
    value.update(change)
    with pytest.raises(ValueError):
        ViewRequest.decode(json.dumps(value).encode(), "ticket")


def test_browser_data_cannot_renew_a_stalled_web_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport, "WEB_LEASE_SECONDS", 0.2)
    client = DockerDouble()
    tunnel = make_tunnel(client)
    web, broker = socket.socketpair()
    thread = Thread(target=tunnel.run, args=(broker, Event()))
    thread.start()
    try:
        began = time.monotonic()
        while time.monotonic() - began < 0.35:
            web.sendall(struct.pack("!BI", 1, 1) + b"x")
            time.sleep(0.03)
        thread.join(1)
        assert not thread.is_alive()
        assert tunnel.result.terminated
        assert tunnel.result.reason == "web_lease_expired"
    finally:
        web.close()
        broker.close()
        thread.join(1)

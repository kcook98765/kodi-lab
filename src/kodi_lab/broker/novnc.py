"""Opt-in Phase 1 display data lane, independent of sequential command dispatch.

The broker forwards opaque RFB bytes. Only it generates helper renew envelopes.
No caller-selected command, container, path, environment, or server flags.
"""

from __future__ import annotations

import json
import secrets
import selectors
import socket
import struct
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]

from kodi_lab.broker.authorization import extract_peer_credentials
from kodi_lab.broker.socket_path import SocketPathConfig, prepare_unix_listener

CHUNK = 65536
BACKLOG = 1024 * 1024
MAX_SESSION = 900.0


@dataclass(frozen=True)
class ViewRequest:
    instance: UUID
    owner: str
    rate: int

    @classmethod
    def decode(cls, raw: bytes, challenge: str) -> ViewRequest:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"instance", "owner", "rate", "challenge"}
            or value["challenge"] != challenge
            or type(value["rate"]) is not int
            or value["rate"] not in (10, 15)
            or not isinstance(value["owner"], str)
            or len(value["owner"]) != 64
            or any(c not in "0123456789abcdef" for c in value["owner"])
        ):
            raise ValueError("view admission denied")
        return cls(UUID(value["instance"]), value["owner"], value["rate"])


@dataclass(frozen=True)
class ViewBinding:
    """Server-resolved immutable container/image/start and catalog identity."""

    container: str
    image: str
    started: str
    identity: tuple[object, ...]


@dataclass
class ViewResult:
    exec_id: str | None = None
    bytes_up: int = 0
    bytes_down: int = 0
    startup_ms: float = 0
    teardown_ms: float = 0
    terminated: bool = False
    reason: str = "failed"
    done: Event = field(default_factory=Event)


class DockerViewTunnel:
    """One worker owns one client, attached exec, independent validation/lease."""

    def __init__(
        self,
        binding: ViewBinding,
        request: ViewRequest,
        valid: Callable[[], bool],
        *,
        client: Any = None,
    ) -> None:
        self.binding = binding
        self.request = request
        self.valid = valid
        self.client = client or docker.APIClient(version="auto", timeout=1)
        self.result = ViewResult()

    def run(self, web: socket.socket, stopped: Event) -> None:
        attached: Any = None
        wire: socket.socket | None = None
        selector = selectors.DefaultSelector()
        start = time.monotonic()
        output = bytearray()
        from_exec = bytearray()
        to_exec = bytearray()
        renewed = 0.0
        try:
            if not self.valid():
                raise ValueError("view identity denied")
            created = self.client.exec_create(
                self.binding.container,
                cmd=[
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "/usr/local/lib/kodi-lab-novnc-view.py",
                ],
                user="1000:1000",
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                privileged=False,
                environment={"KODI_LAB_VIEW_RATE": str(self.request.rate)},
            )
            self.result.exec_id = created["Id"]
            attached = self.client.exec_start(created["Id"], socket=True, tty=False)
            wire = attached._sock
            assert wire is not None
            wire.setblocking(False)
            web.setblocking(False)
            selector.register(web, selectors.EVENT_READ)
            selector.register(wire, selectors.EVENT_READ)
            self.result.startup_ms = (time.monotonic() - start) * 1000
            last_write = time.monotonic()
            while not stopped.is_set() and time.monotonic() - start < MAX_SESSION:
                now = time.monotonic()
                if now - renewed >= 1:
                    # A fresh authority check, not browser traffic, renews the helper.
                    if not self.valid():
                        self.result.reason = "revoked"
                        break
                    to_exec.extend(struct.pack("!BI", 2, 0))
                    renewed = time.monotonic()
                selector.modify(
                    web, selectors.EVENT_READ | (selectors.EVENT_WRITE if output else 0)
                )
                selector.modify(
                    wire,
                    selectors.EVENT_READ | (selectors.EVENT_WRITE if to_exec else 0),
                )
                for key, mask in selector.select(0.05):
                    if key.fileobj is web:
                        if mask & selectors.EVENT_READ:
                            payload = web.recv(CHUNK)
                            if not payload:
                                self.result.reason = "disconnect"
                                return
                            self.result.bytes_up += len(payload)
                            to_exec.extend(struct.pack("!BI", 1, len(payload)))
                            to_exec.extend(payload)
                        if mask & selectors.EVENT_WRITE and output:
                            size = web.send(output[:CHUNK])
                            del output[:size]
                            self.result.bytes_down += size
                            last_write = time.monotonic()
                    else:
                        if mask & selectors.EVENT_READ:
                            payload = wire.recv(CHUNK)
                            if not payload:
                                self.result.reason = "exec_eof"
                                return
                            from_exec.extend(payload)
                            while len(from_exec) >= 8:
                                stream, reserved, size = struct.unpack(
                                    "!B3sI", from_exec[:8]
                                )
                                if (
                                    stream not in (1, 2)
                                    or reserved != b"\0\0\0"
                                    or size > BACKLOG
                                ):
                                    raise ValueError("invalid Docker stream")
                                if len(from_exec) < 8 + size:
                                    break
                                if stream == 2 and size:
                                    # Never mix/persist stderr with pixels.
                                    raise ValueError("display helper failed")
                                if not output:
                                    last_write = time.monotonic()
                                output.extend(from_exec[8 : 8 + size])
                                del from_exec[: 8 + size]
                        if mask & selectors.EVENT_WRITE and to_exec:
                            size = wire.send(to_exec[:CHUNK])
                            del to_exec[:size]
                if max(len(output), len(from_exec), len(to_exec)) > BACKLOG:
                    raise ValueError("display backlog exceeded")
                if output and time.monotonic() - last_write > 2:
                    raise TimeoutError("display consumer stalled")
            self.result.reason = "cancelled"
        finally:
            ending = time.monotonic()
            selector.close()
            if wire is not None:
                # Best effort close; independent helper timer is mandatory fallback.
                with suppress(OSError):
                    wire.send(struct.pack("!BI", 3, 0))
                with suppress(OSError):
                    wire.shutdown(socket.SHUT_RDWR)
            if attached is not None:
                attached.close()
            while self.result.exec_id and time.monotonic() - ending < 4.5:
                try:
                    state = self.client.exec_inspect(self.result.exec_id)
                except Exception:
                    break
                if not state["Running"]:
                    self.result.terminated = True
                    break
                time.sleep(0.05)
            if self.result.exec_id is None:
                self.result.terminated = True
            self.result.teardown_ms = (time.monotonic() - ending) * 1000
            self.client.close()
            self.result.done.set()


class DisplayLane:
    """Bounded separate Unix listener; no worker runs in broker dispatch.

    Failed/unproven teardown retains its capacity reservation until service
    restart. The container helper's flock also fences a surviving old worker.
    """

    def __init__(
        self,
        config: SocketPathConfig,
        web_uid: int,
        admit: Callable[[ViewRequest], DockerViewTunnel],
    ) -> None:
        self.config = config
        self.web_uid = web_uid
        self.admit = admit
        self.stopped = Event()
        self.lock = Lock()
        self.active: set[UUID] = set()
        self.threads: list[Thread] = []
        self.results: list[ViewResult] = []
        self.listener: Any = None
        self.accept_thread: Thread | None = None
        self.control_stopped: Event | None = None

    def start(self, control_stopped: Event | None = None) -> None:
        self.control_stopped = control_stopped
        self.listener = prepare_unix_listener(self.config)
        self.listener.socket.settimeout(0.1)
        self.accept_thread = Thread(
            target=self._accept, name="display-admission", daemon=True
        )
        self.accept_thread.start()

    def _accept(self) -> None:
        while not self.stopped.is_set():
            if self.control_stopped is not None and self.control_stopped.is_set():
                self.stopped.set()
                break
            try:
                peer, _ = self.listener.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            # At most two concurrent handshakes OR established workers globally.
            with self.lock:
                self.threads = [t for t in self.threads if t.is_alive()]
                if len(self.threads) >= 2:
                    peer.close()
                    continue
                thread = Thread(
                    target=self._worker,
                    args=(peer,),
                    name="display-worker",
                    daemon=True,
                )
                self.threads.append(thread)
                thread.start()

    def _worker(self, peer: socket.socket) -> None:
        instance: UUID | None = None
        tunnel: DockerViewTunnel | None = None
        try:
            with peer:
                if extract_peer_credentials(peer).uid != self.web_uid:
                    return
                peer.settimeout(1)
                challenge = secrets.token_hex(32)
                peer.sendall(challenge.encode("ascii"))
                raw = bytearray()
                deadline = time.monotonic() + 1
                while not raw.endswith(b"\n"):
                    peer.settimeout(max(0.001, deadline - time.monotonic()))
                    if time.monotonic() >= deadline:
                        return
                    value = peer.recv(1)
                    if not value or len(raw) >= 1024:
                        return
                    raw.extend(value)
                request = ViewRequest.decode(bytes(raw), challenge)
                with self.lock:
                    if request.instance in self.active or len(self.active) >= 2:
                        return
                    self.active.add(request.instance)
                    instance = request.instance
                tunnel = self.admit(request)
                with self.lock:
                    self.results.append(tunnel.result)
                    self.results = self.results[-64:]
                peer.sendall(b"OK\n")
                tunnel.run(peer, self.stopped)
        except Exception:
            pass  # Closed failure, no credentials/RFB/exception text in audit.
        finally:
            peer.close()
            if tunnel is not None and tunnel.result.exec_id is None:
                tunnel.client.close()
                tunnel.result.terminated = True
            if instance is not None and (tunnel is None or tunnel.result.terminated):
                with self.lock:
                    self.active.discard(instance)

    def close(self) -> None:
        self.stopped.set()
        if self.listener is not None:
            self.listener.close()
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=1)
        deadline = time.monotonic() + 5
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

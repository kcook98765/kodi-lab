"""Restricted Unix-socket transport for the managed Kodi controller."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import socketserver
import stat
import struct
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from typing import Protocol, cast

from . import REVIEWED_IMAGE_FILE
from .controller import Controller
from .packages import HEX, MAX_ZIP
from .prepare import PREPARE_CLEANUP_INTERVAL
from .protocol import OperationReply, Refusal, actor_key, validate
from .viewer import display_lane

MAX_REQUEST = 810_000
MAX_REPLY = 2_000_000


class BinaryLineReader(Protocol):
    def readline(self, size: int = -1, /) -> bytes: ...


def receive(stream: BinaryLineReader, limit: int) -> object:
    line = stream.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("invalid broker frame")
    return cast(object, json.loads(line))


def frame(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class BrokerClient:
    def __init__(self, path: Path) -> None:
        self.path = path

    def request(self, actor: str, name: str, args: Mapping[str, str]) -> OperationReply:
        return self._exchange(actor, name, args)

    def upload(self, actor: str, data: bytes, digest: str) -> OperationReply:
        return self._exchange(
            actor, "package_upload", {"size": len(data), "digest": digest}, data
        )

    def _exchange(
        self,
        actor: str,
        name: str,
        args: Mapping[str, str | int],
        data: bytes = b"",
    ) -> OperationReply:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(155)
                connection.connect(str(self.path))
                connection.sendall(
                    frame({"actor": actor, "name": name, "args": dict(args)})
                )
                if data:
                    connection.sendall(data)
                with connection.makefile("rb") as response:
                    reply = receive(response, MAX_REPLY)
        except (OSError, ValueError) as exc:
            raise Refusal("operation_outcome_unconfirmed") from exc
        if not isinstance(reply, dict):
            raise Refusal("managed_broker_reply_invalid")
        if set(reply) == {"error"} and isinstance(reply["error"], str):
            raise Refusal(reply["error"])
        value = reply.get("value")
        if not isinstance(value, dict) or set(value) != {"result", "elapsed_seconds"}:
            raise Refusal("managed_broker_reply_invalid")
        return cast(OperationReply, value)


class Broker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, controller: Controller, frontend_uid: int) -> None:
        self.controller = controller
        self.frontend_uid = frontend_uid
        super().__init__(str(path), Handler)

    def verify_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: object,
    ) -> bool:
        if not isinstance(request, socket.socket):
            return False
        identity = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", identity)
        return int(uid) == self.frontend_uid

    def dispatch(
        self, request: object, reader: Callable[[int], bytes] | None = None
    ) -> OperationReply:
        if not isinstance(request, dict) or set(request) != {"actor", "name", "args"}:
            raise Refusal("invalid_broker_request")
        # Only the exact application service UID can assert an actor. Web cookies
        # and agent bearer credentials never cross the broker socket.
        actor = actor_key(request["actor"])
        if request["name"] == "package_upload":
            args = request["args"]
            if (
                reader is None
                or actor == "admin"
                or not isinstance(args, dict)
                or set(args) != {"size", "digest"}
                or type(args["size"]) is not int
                or not 0 < args["size"] <= MAX_ZIP
                or not isinstance(args["digest"], str)
                or not HEX.fullmatch(args["digest"])
            ):
                raise Refusal("invalid_broker_request")
            data = reader(args["size"])
            if len(data) != args["size"]:
                raise Refusal("incomplete_package_upload")
            return self.controller.upload_package(data, args["digest"], actor=actor)
        name, args = validate(request["name"], request["args"])
        return self.controller.perform(name, args, actor=actor)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(5)
        reply: dict[str, object]
        try:
            request = receive(self.rfile, MAX_REQUEST)
        except (OSError, ValueError):
            reply = {"error": "invalid_broker_request"}
        else:
            try:
                assert isinstance(self.server, Broker)
                reply = {"value": self.server.dispatch(request, self.rfile.read)}
            except Refusal as exc:
                reply = {"error": str(exc)}
            except Exception:
                logging.exception("Managed broker operation outcome unconfirmed")
                reply = {"error": "operation_outcome_unconfirmed"}
        with suppress(OSError):
            self.wfile.write(frame(reply))


def maintain_resources(controller: Controller, stopped: Event) -> None:
    while not stopped.is_set():
        controller.resume_prepare_cleanup()
        if not stopped.is_set():
            controller.resume_feed_cleanup()
        stopped.wait(PREPARE_CLEANUP_INTERVAL)


def serve(
    root: Path, path: Path, frontend_uid: int, frontend_gid: int, daemon_root: Path
) -> None:
    if frontend_uid in {0, os.getuid()}:
        raise Refusal("separate_frontend_uid_required")
    controller = Controller(
        root,
        daemon_root=daemon_root,
        reviewed_image=path.parent.parent / REVIEWED_IMAGE_FILE,
        handoff_root=path.parent.parent,
    )
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise Refusal("broker_socket_parent_not_owned")
    with (root / "broker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("broker_already_running") from exc
        if path.exists() or path.is_symlink():
            entry = path.lstat()
            if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
                raise Refusal("broker_socket_not_owned")
            path.unlink()
        with Broker(path, controller, frontend_uid) as broker:
            try:
                os.chown(path, -1, frontend_gid)
                path.chmod(0o660)
                stopped = Event()
                signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                lane = display_lane(
                    controller, path.parent / "display.sock", frontend_uid, frontend_gid
                )
                lane.start(stopped)
                try:
                    # Active recovery may need the ordinary lifecycle timeouts.
                    # Serve the socket while it reconciles under the site lock.
                    Thread(
                        target=controller.resume_interrupted_prepare,
                        name="managed-prepare-recovery",
                        daemon=True,
                    ).start()
                    Thread(
                        target=maintain_resources,
                        args=(controller, stopped),
                        name="managed-resource-cleanup",
                        daemon=True,
                    ).start()
                    broker.timeout = 0.2
                    while not stopped.is_set():
                        broker.handle_request()
                finally:
                    lane.close()
            finally:
                path.unlink(missing_ok=True)

"""Restricted Unix-socket transport for the managed Kodi controller."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import socket
import socketserver
import stat
import struct
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol, cast

from .controller import Controller
from .protocol import OperationReply, Refusal, actor_key, validate

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
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(155)
                connection.connect(str(self.path))
                connection.sendall(
                    frame({"actor": actor, "name": name, "args": dict(args)})
                )
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

    def dispatch(self, request: object) -> OperationReply:
        if not isinstance(request, dict) or set(request) != {"actor", "name", "args"}:
            raise Refusal("invalid_broker_request")
        # Only the exact application service UID can assert an actor. Web cookies
        # and agent bearer credentials never cross the broker socket.
        actor = actor_key(request["actor"])
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
                reply = {"value": self.server.dispatch(request)}
            except Refusal as exc:
                reply = {"error": str(exc)}
            except Exception:
                logging.exception("Managed broker operation outcome unconfirmed")
                reply = {"error": "operation_outcome_unconfirmed"}
        with suppress(OSError):
            self.wfile.write(frame(reply))


def serve(root: Path, path: Path, frontend_uid: int, frontend_gid: int) -> None:
    if frontend_uid in {0, os.getuid()}:
        raise Refusal("separate_frontend_uid_required")
    controller = Controller(root)
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
                broker.serve_forever()
            finally:
                path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--frontend-uid", type=int, required=True)
    parser.add_argument("--frontend-gid", type=int, required=True)
    args = parser.parse_args()
    serve(args.root, args.socket, args.frontend_uid, args.frontend_gid)


if __name__ == "__main__":
    main()

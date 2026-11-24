"""Local restricted broker for one fresh-install managed runtime."""

import argparse
import fcntl
import hmac
import json
import logging
import os
import secrets
import socket
import socketserver
import stat
import struct
from contextlib import suppress
from pathlib import Path

from .protocol import Refusal, validate

MAX_REQUEST = 810_000
MAX_REPLY = 2_000_000


def receive(stream, limit):
    line = stream.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("invalid broker frame")
    return json.loads(line)


def frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class BrokerClient:
    def __init__(self, path):
        self.path = path

    def request(self, token, name, args):
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(155)
                connection.connect(str(self.path))
                connection.sendall(frame({"token": token, "name": name, "args": args}))
                with connection.makefile("rb") as response:
                    reply = receive(response, MAX_REPLY)
        except (OSError, ValueError) as exc:
            raise Refusal("operation_outcome_unconfirmed") from exc
        if "error" in reply:
            raise Refusal(reply["error"])
        return reply["value"]


class Broker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, controller, frontend_uid, credentials):
        self.controller = controller
        self.frontend_uid = frontend_uid
        self.credentials = credentials
        super().__init__(str(path), Handler)

    def verify_request(self, request, _address):
        identity = request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", identity)
        return uid == self.frontend_uid

    def dispatch(self, request):
        if not isinstance(request, dict) or set(request) != {"token", "name", "args"}:
            raise Refusal("invalid_broker_request")
        token = request["token"]
        if not isinstance(token, str) or not token.isascii() or len(token) > 128:
            raise Refusal("broker_access_denied")
        owner, observer = self.credentials
        write = hmac.compare_digest(token, owner)
        if not write and not hmac.compare_digest(token, observer):
            raise Refusal("broker_access_denied")
        name, args = request["name"], request["args"]
        if name == "authorize" and args == {}:
            return {"write": write}
        validate(name, args)
        return self.controller.perform(name, args, write=write)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            request = receive(self.rfile, MAX_REQUEST)
        except (OSError, ValueError):
            reply = {"error": "invalid_broker_request"}
        else:
            try:
                reply = {"value": self.server.dispatch(request)}
            except Refusal as exc:
                reply = {"error": str(exc)}
            except Exception:
                logging.exception("Managed broker operation outcome unconfirmed")
                reply = {"error": "operation_outcome_unconfirmed"}
        # The authoritative operation record survives a lost reply.
        with suppress(OSError):
            self.wfile.write(frame(reply))


def serve(root, path, frontend_uid, frontend_gid, *, interrupt=None):
    from .controller import Controller
    from .runtime import atomic

    controller = Controller(root, interrupt=interrupt)
    with controller.runtime.exclusive():
        tokens = []
        for name in ("owner", "observer"):
            credential = root / (name + ".token")
            if not credential.exists():
                atomic(credential, secrets.token_urlsafe(32).encode())
            tokens.append(credential.read_text())
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise Refusal("broker_socket_parent_not_owned")
    # A live broker may be between accepting a request and taking the mutation lock.
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
        with Broker(path, controller, frontend_uid, tokens) as broker:
            try:
                os.chown(path, -1, frontend_gid)
                path.chmod(0o660)
                broker.serve_forever()
            finally:
                path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("socket", type=Path)
    parser.add_argument("--frontend-uid", type=int, required=True)
    parser.add_argument("--frontend-gid", type=int, required=True)
    args = parser.parse_args()
    serve(args.root, args.socket, args.frontend_uid, args.frontend_gid)


if __name__ == "__main__":
    main()

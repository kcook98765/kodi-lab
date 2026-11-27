"""Restricted Unix-socket transport for the managed Kodi controller."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
import socketserver
import stat
import struct
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from typing import Protocol, cast

from . import REVIEWED_IMAGE_FILE
from .controller import Controller
from .packages import HEX, MAX_ZIP
from .prepare import PREPARE_CLEANUP_INTERVAL
from .protocol import (
    RECORDING_RATES,
    RECORDING_TOOLS,
    OperationReply,
    Refusal,
    actor_key,
    validate,
)
from .recording import MAX_BYTES, RECORDING_ID, CaptureBinding, Recordings
from .runtime_retirement import OPERATIONS as RETIREMENT_OPERATIONS
from .storage_maintenance import OPERATIONS as STORAGE_OPERATIONS
from .viewer import display_lane
from .x11_recording import MAX_SECONDS

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

    @asynccontextmanager
    async def download(
        self, actor: str, credential: str, recording: str
    ) -> AsyncIterator[tuple[asyncio.StreamReader, int]]:
        writer: asyncio.StreamWriter | None = None
        try:
            try:
                async with asyncio.timeout(5):
                    reader, writer = await asyncio.open_unix_connection(self.path)
                    writer.write(
                        frame(
                            {
                                "actor": actor,
                                "name": "recording_download",
                                "args": {
                                    "credential": credential,
                                    "recording": recording,
                                },
                            }
                        )
                    )
                    await writer.drain()
                    reply = json.loads(await reader.readline())
            except (OSError, ValueError) as error:
                raise Refusal("operation_outcome_unconfirmed") from error
            if not isinstance(reply, dict):
                raise Refusal("managed_broker_reply_invalid")
            if set(reply) == {"error"} and isinstance(reply["error"], str):
                raise Refusal(reply["error"])
            size = reply.get("bytes")
            if type(size) is not int or not 0 < size <= MAX_BYTES:
                raise Refusal("managed_broker_reply_invalid")
            yield reader, size
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    def _exchange(
        self,
        actor: str,
        name: str,
        args: Mapping[str, str | int],
        data: bytes = b"",
    ) -> OperationReply:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(2 if name == "recording_renew" else 155)
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

    def __init__(
        self,
        path: Path,
        controller: Controller,
        frontend_uid: int,
        recordings: Recordings | None = None,
    ) -> None:
        self.controller = controller
        self.frontend_uid = frontend_uid
        self.recordings = recordings
        self.download_slot = BoundedSemaphore()
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
        operation = request["name"]
        if isinstance(operation, str) and operation in (
            STORAGE_OPERATIONS | RETIREMENT_OPERATIONS
        ):
            if actor != "admin":
                raise Refusal("storage_maintenance_admin_required")
            started = time.monotonic()
            handler = (
                self.controller.runtime_retirement
                if operation in RETIREMENT_OPERATIONS
                else self.controller.storage_maintenance
            )
            return {
                "result": handler.request(operation, request["args"]),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        if isinstance(operation, str) and operation in RECORDING_TOOLS | {
            "recording_renew"
        }:
            return self.recording_request(actor, operation, request["args"])
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

    def recording_request(
        self,
        actor: str,
        operation: str,
        args: object,
    ) -> OperationReply:
        started = time.monotonic()
        identity, arguments = recording_identity(actor, args)
        if self.recordings is None:
            raise Refusal("managed_recording_unavailable")
        if operation == "managed_recording_start":
            _, settings = validate(operation, arguments)
            rate = settings.get("fps", "2")
            seconds = settings.get("seconds", "300")
            if (
                rate not in RECORDING_RATES
                or not seconds.isascii()
                or not seconds.isdecimal()
                or not 1 <= int(seconds) <= MAX_SECONDS
            ):
                raise Refusal("invalid_arguments")
            generation = settings.get("generation", "omega")
            result = self.recordings.start(
                identity,
                int(rate),
                int(seconds),
                lambda: self.recording_binding(generation, actor),
            )
        else:
            if operation != "recording_renew":
                validate(operation, arguments)
            recording = (
                None
                if operation == "managed_recording_status" and not arguments
                else recording_argument(arguments)
            )
            result = self.recordings.request(
                identity, recording, operation.rsplit("_", 1)[1]
            )
        return {
            "result": result,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    def recording_binding(self, generation: str, actor: str) -> CaptureBinding:
        runtime = self.controller.runtimes.get(generation)
        if runtime is None:
            raise Refusal("managed_generation_unavailable")
        with self.controller.omega.exclusive():
            # Use the same read authority as managed GUI observation. Recording
            # never claims or renews the managed input lease.
            self.controller.authorize_profile("managed_gui", actor, None)
            obj = runtime.inspect()
            if not obj or not obj["State"]["Running"]:
                raise Refusal("kodi_stopped")
            runtime.check_live_profile(obj)
            if runtime.public.session.exists():
                raise Refusal("managed_recording_public_profile")
            return CaptureBinding(
                generation,
                obj["Id"],
                obj["State"]["StartedAt"],
                runtime.image,
                runtime.read_pointer(),
                runtime.read_pointer,
            )


def recording_identity(
    actor: str, args: object
) -> tuple[tuple[str, str], dict[str, object]]:
    if actor == "admin" or not isinstance(args, dict):
        raise Refusal("permission_denied")
    arguments = dict(args)
    credential = actor_key(arguments.pop("credential", None))
    if credential == "admin":
        raise Refusal("permission_denied")
    return (actor, credential), arguments


def recording_argument(args: Mapping[str, object]) -> str:
    recording = args.get("recording")
    if (
        set(args) != {"recording"}
        or not isinstance(recording, str)
        or not RECORDING_ID.fullmatch(recording)
    ):
        raise Refusal("invalid_arguments")
    return recording


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
                if (
                    isinstance(request, dict)
                    and request.get("name") == "recording_download"
                ):
                    self.download(request)
                    return
                reply = {"value": self.server.dispatch(request, self.rfile.read)}
            except Refusal as exc:
                reply = {"error": str(exc)}
            except Exception:
                logging.exception("Managed broker operation outcome unconfirmed")
                reply = {"error": "operation_outcome_unconfirmed"}
        with suppress(OSError):
            self.wfile.write(frame(reply))

    def download(self, request: dict[str, object]) -> None:
        assert isinstance(self.server, Broker)
        if set(request) != {"actor", "name", "args"}:
            raise Refusal("invalid_broker_request")
        identity, args = recording_identity(
            actor_key(request["actor"]), request["args"]
        )
        recording = recording_argument(args)
        if self.server.recordings is None:
            raise Refusal("managed_recording_unavailable")
        if not self.server.download_slot.acquire(blocking=False):
            raise Refusal("managed_recording_download_busy")
        try:
            file, result = self.server.recordings.open_artifact(identity, recording)
            with file, suppress(OSError):
                self.wfile.write(frame({"bytes": result["bytes"]}))
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and (chunk := file.read(65536)):
                    self.wfile.write(chunk)
        finally:
            self.server.download_slot.release()


def maintain_resources(
    controller: Controller,
    recordings: Recordings,
    stopped: Event,
) -> None:
    while not stopped.is_set():
        controller.resume_prepare_cleanup()
        if not stopped.is_set():
            controller.resume_feed_cleanup()
            if recordings is not None:
                with suppress(OSError):
                    recordings.cleanup()
        stopped.wait(PREPARE_CLEANUP_INTERVAL)


def serve(
    root: Path,
    path: Path,
    frontend_uid: int,
    frontend_gid: int,
    daemon_root: Path,
    worker_image: str | None = None,
) -> None:
    if frontend_uid in {0, os.getuid()}:
        raise Refusal("separate_frontend_uid_required")
    controller = Controller(
        root,
        daemon_root=daemon_root,
        reviewed_image=path.parent.parent / REVIEWED_IMAGE_FILE,
        handoff_root=path.parent.parent,
        audio_image=worker_image,
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
        try:
            recordings: Recordings | None = Recordings(
                root / "recordings",
                daemon_root=daemon_root / "recordings",
                encoder_image=worker_image,
            )
        except (OSError, Refusal):
            logging.warning("Managed recording storage unavailable")
            recordings = None
        with Broker(path, controller, frontend_uid, recordings) as broker:
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
                        args=(controller, recordings, stopped),
                        name="managed-resource-cleanup",
                        daemon=True,
                    ).start()
                    broker.timeout = 0.2
                    while not stopped.is_set():
                        broker.handle_request()
                finally:
                    try:
                        lane.close()
                    finally:
                        if recordings is not None:
                            recordings.close()
            finally:
                path.unlink(missing_ok=True)

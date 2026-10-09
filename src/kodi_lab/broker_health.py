"""Small root-only readiness probe; deliberately independent of broker imports."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
from uuid import uuid4

SOCKET_PATH = Path("/run/kodi-lab/broker.sock")
RESPONSE_DEADLINE_SECONDS = 2.0
MAX_FRAME_PAYLOAD = 65_536


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("broker response deadline exceeded")
    return remaining


def _receive_exact(connection: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        connection.settimeout(_remaining(deadline))
        part = connection.recv(size - len(data))
        if not part:
            raise ValueError("truncated broker response")
        data.extend(part)
    return bytes(data)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _validate_denial(payload: bytes, request_id: str) -> None:
    document = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(document, dict) or set(document) != {
        "version",
        "request_id",
        "ok",
        "error",
    }:
        raise ValueError("invalid broker response envelope")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("incorrect broker protocol version")
    if document["ok"] is not False:
        # Accepting a root ping would indicate identity-policy drift.
        raise ValueError("broker did not reject the root peer")
    if document["request_id"] is not None and document["request_id"] != request_id:
        raise ValueError("incorrect broker response identity")
    error = document["error"]
    if not isinstance(error, dict) or set(error) != {"code", "message"}:
        raise ValueError("invalid broker error envelope")
    if error["code"] != "unauthorized_peer":
        raise ValueError("unexpected broker denial")
    message = error["message"]
    if not isinstance(message, str) or not 1 <= len(message) <= 256:
        raise ValueError("invalid broker error message")


def _probe(socket_path: Path) -> None:
    deadline = time.monotonic() + RESPONSE_DEADLINE_SECONDS
    request_id = str(uuid4())
    payload = json.dumps(
        {"version": 1, "request_id": request_id, "operation": "ping", "params": {}},
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(_remaining(deadline))
        connection.connect(str(socket_path))
        connection.settimeout(_remaining(deadline))
        try:
            connection.sendall(struct.pack("!I", len(payload)) + payload)
            connection.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            # Authorization precedes reads; a complete denial can still be waiting.
            pass
        (size,) = struct.unpack("!I", _receive_exact(connection, 4, deadline))
        if size > MAX_FRAME_PAYLOAD:
            raise ValueError("oversized broker response")
        response = _receive_exact(connection, size, deadline)
        _remaining(deadline)
        connection.setblocking(False)
        try:
            trailing = connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except (BlockingIOError, ConnectionResetError):
            trailing = b""
        if trailing:
            raise ValueError("trailing broker response data")
        _validate_denial(response, request_id)
        _remaining(deadline)


def main() -> int:
    """Succeed only when the real listener gives root its expected strict denial."""

    if sys.argv[1:]:
        print("usage: kodi-lab-broker-healthcheck", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("broker health check requires the root peer", file=sys.stderr)
        return 1
    try:
        _probe(SOCKET_PATH)
    except TimeoutError:
        print("broker health check response timed out", file=sys.stderr)
        return 1
    except OSError:
        print("broker health check connection failed", file=sys.stderr)
        return 1
    except (ValueError, RecursionError):
        print("broker health check response rejected", file=sys.stderr)
        return 1
    return 0

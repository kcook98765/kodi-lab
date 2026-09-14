"""Bounded length-prefixed Unix-stream framing."""

from __future__ import annotations

import socket
import struct

from kodi_lab.broker.errors import (
    FrameTooLargeError,
    TrailingFrameDataError,
    TruncatedFrameError,
)

FRAME_HEADER_SIZE = 4
MAX_FRAME_PAYLOAD = 65_536


def receive_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly ``size`` bytes or fail on early EOF."""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise TruncatedFrameError("connection closed before frame completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_frame(
    sock: socket.socket, *, max_payload: int = MAX_FRAME_PAYLOAD
) -> bytes:
    """Receive one bounded unsigned-big-endian length-prefixed frame."""

    header = receive_exact(sock, FRAME_HEADER_SIZE)
    (length,) = struct.unpack("!I", header)
    if length > max_payload:
        raise FrameTooLargeError(
            f"announced frame length exceeds {max_payload} byte limit"
        )
    return receive_exact(sock, length)


def send_frame(
    sock: socket.socket,
    payload: bytes,
    *,
    max_payload: int = MAX_FRAME_PAYLOAD,
) -> None:
    """Send one bounded unsigned-big-endian length-prefixed frame."""

    if len(payload) > max_payload:
        raise FrameTooLargeError(f"frame exceeds {max_payload} byte limit")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def reject_buffered_trailing_data(sock: socket.socket) -> None:
    """Reject bytes already sent after the protocol's single frame."""

    try:
        trailing = sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except (BlockingIOError, ConnectionResetError):
        return
    if trailing:
        raise TrailingFrameDataError("connection contains data after its single frame")

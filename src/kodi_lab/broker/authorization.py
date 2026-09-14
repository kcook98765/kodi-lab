"""Kernel peer-credential authentication and exact-UID authorization."""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PeerCredentials:
    """Linux credentials attached by the kernel to a Unix peer."""

    pid: int
    uid: int
    gid: int


def extract_peer_credentials(connection: socket.socket) -> PeerCredentials:
    """Read Linux ``SO_PEERCRED`` from an accepted Unix connection."""

    credential_size = struct.calcsize("3i")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, credential_size)
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


type PeerCredentialExtractor = Callable[[socket.socket], PeerCredentials]


@dataclass(frozen=True, slots=True)
class ExactUidAuthorizationPolicy:
    """Authorize only the one configured effective peer UID."""

    expected_uid: int

    def is_authorized(self, peer: PeerCredentials) -> bool:
        """Ignore PID/GID and compare only the kernel-provided UID."""

        return peer.uid == self.expected_uid

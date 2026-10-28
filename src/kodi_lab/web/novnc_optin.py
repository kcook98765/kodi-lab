"""Human Web-only binding lookup over the bounded private display lane."""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.config import Settings
from kodi_lab.web.novnc import PrototypeViewer


def resolve_binding(path: Path, instance: UUID) -> ViewBinding:
    """Lookup confers no worker/input grant; admission still claims exact identity."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(1)
        peer.connect(str(path))
        challenge = bytearray()
        while len(challenge) < 64:
            data = peer.recv(64 - len(challenge))
            if not data:
                raise ValueError("display binding unavailable")
            challenge.extend(data)
        if re.fullmatch(b"[0-9a-f]{64}", challenge) is None:
            raise ValueError("display challenge invalid")
        peer.sendall(
            json.dumps(
                {"resolve": str(instance), "challenge": challenge.decode()}
            ).encode()
            + b"\n"
        )
        raw = bytearray()
        deadline = time.monotonic() + 2
        while not raw.endswith(b"\n"):
            peer.settimeout(max(0.001, deadline - time.monotonic()))
            if time.monotonic() >= deadline or len(raw) > 4096:
                raise ValueError("display binding exceeded bounds")
            data = peer.recv(1)
            if not data:
                raise ValueError("display binding denied")
            raw.extend(data)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"container", "image", "started", "identity"}
            or not isinstance(value["container"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["container"]) is None
            or not isinstance(value["image"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"]) is None
            or not isinstance(value["started"], str)
            or not 1 <= len(value["started"]) <= 64
            or not isinstance(value["identity"], list)
            or not 3 <= len(value["identity"]) <= 7
            or any(
                x is not None and type(x) not in (str, int) for x in value["identity"]
            )
        ):
            raise ValueError("display binding invalid")
        return ViewBinding(
            value["container"],
            value["image"],
            value["started"],
            tuple(value["identity"]),
        )


def build_viewer(
    settings: Settings, sessions: sessionmaker[Session]
) -> PrototypeViewer:
    path = settings.runtime_root / "display.sock"
    allowed = frozenset(settings.experimental_novnc_instances)

    def authority(instance: UUID) -> ViewBinding:
        if instance not in allowed:
            raise ValueError("experimental viewer is not enabled")
        binding = resolve_binding(path, instance)
        # Resolve again on the unprivileged side to fence concurrent catalog writes.
        return CatalogViewAuthority(sessions, {instance: binding}).resolve(instance)

    return PrototypeViewer(
        path,
        authority,
        ownership=HumanViewerOwnership(sessions),
        enabled_instances=allowed,
        require_https=True,
    )

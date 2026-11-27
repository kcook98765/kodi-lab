"""Read a stable player sample without returning Kodi's raw media paths."""

from __future__ import annotations

import hashlib
import hmac
from subprocess import SubprocessError
from typing import TYPE_CHECKING

from .protocol import Document, Refusal

if TYPE_CHECKING:
    from .runtime import Runtime

OBSERVATION_ERRORS = (
    Refusal,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    SubprocessError,
)


def incarnation(runtime: Runtime) -> tuple[str, str] | None:
    container = runtime.inspect()
    if not container or not container["State"]["Running"]:
        return None
    return container["Id"], container["State"]["StartedAt"]


def observe(
    runtime: Runtime,
    binding: tuple[str, str],
    expected_local_media_sha256: str | None,
) -> Document:
    def identity() -> object:
        return runtime.rpc("Player.GetActivePlayers")

    def sample(player_id: int) -> tuple[object, object]:
        properties = runtime.rpc(
            "Player.GetProperties", playerid=player_id, properties=["speed"]
        )
        item = runtime.rpc("Player.GetItem", playerid=player_id, properties=["file"])
        return properties["speed"], item["item"].get("file")

    unavailable = {"state": "unavailable"}
    uncertain = {"state": "uncertain"}
    try:
        active = identity()
        if active == []:
            result: Document = {"state": "idle"} if identity() == [] else uncertain
        elif (
            isinstance(active, list)
            and len(active) == 1
            and isinstance(active[0], dict)
            and type(player_id := active[0].get("playerid")) is int
            and 0 <= player_id <= 2**31 - 1
            and (kind := active[0].get("type")) in ("audio", "video", "picture")
        ):
            speed, file = observed = sample(player_id)
            repeated = sample(player_id)
            if repeated != observed or identity() != active:
                result = uncertain
            else:
                result = {
                    "state": "uncertain",
                    "player_id": player_id,
                    "media_type": kind,
                }
                if type(speed) is int:
                    result["state"] = "paused" if speed == 0 else "playing"
                if isinstance(file, str) and file:
                    # The per-start credential prevents URL or filename guessing
                    # against returned fingerprints. It never crosses this boundary.
                    key = (runtime.credentials / "rpc").read_bytes()
                    result["media_key"] = hmac.digest(
                        key,
                        (
                            "managed player media\x00" + binding[1] + "\x00" + file
                        ).encode(),
                        "sha256",
                    ).hex()
                    if expected_local_media_sha256 and (
                        file.startswith("/")
                        and not file.startswith("//")
                        and "|" not in file
                    ):
                        result["matches_expected_local_media"] = hmac.compare_digest(
                            hashlib.sha256(file.encode()).hexdigest(),
                            expected_local_media_sha256,
                        )
        else:
            result = uncertain
        if incarnation(runtime) != binding:
            return unavailable
        result["incarnation"] = hashlib.sha256(":".join(binding).encode()).hexdigest()
        return result
    except OBSERVATION_ERRORS:
        return unavailable

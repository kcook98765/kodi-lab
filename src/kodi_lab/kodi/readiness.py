"""Kodi application identity returned by native JSON-RPC."""

from __future__ import annotations

from dataclasses import dataclass

from kodi_lab.domain import (
    KodiReleaseChannel,
    KodiReleaseIdentity,
)

MAX_JSONRPC_RESPONSE_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class KodiApplicationIdentity:
    name: str
    major: int
    minor: int
    tag: str
    tag_version: int | None = None

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        # Keep the observed upstream tag while normalizing its canonical channel.
        channel = (
            KodiReleaseChannel.RC
            if self.tag == "releasecandidate"
            else KodiReleaseChannel(self.tag)
        )
        return KodiReleaseIdentity(
            self.major,
            self.minor,
            channel,
            self.tag_version,
        )

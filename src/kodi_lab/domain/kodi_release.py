"""Exact Kodi release identity and product support policy types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_RELEASE_PATTERN = re.compile(
    r"^(?P<major>[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)"
    r"(?:-(?P<channel>alpha|beta|rc)\.(?P<ordinal>[1-9][0-9]*))?$"
)


class KodiReleaseChannel(StrEnum):
    """Release channels reported by Kodi's application identity."""

    STABLE = "stable"
    ALPHA = "alpha"
    BETA = "beta"
    RC = "rc"


class RuntimeSupportTier(StrEnum):
    """Product support policy for one exact Kodi release definition."""

    CURRENT = "current"
    LEGACY = "legacy"
    PREVIEW = "preview"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True, order=True)
class KodiReleaseIdentity:
    """Small typed identity for the exact Kodi releases supported by the lab."""

    major: int
    minor: int
    channel: KodiReleaseChannel = KodiReleaseChannel.STABLE
    prerelease_ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.major <= 0:
            raise ValueError("Kodi release major must be positive")
        if self.minor < 0:
            raise ValueError("Kodi release minor must not be negative")
        if self.channel is KodiReleaseChannel.STABLE:
            if self.prerelease_ordinal is not None:
                raise ValueError("stable Kodi release cannot have prerelease ordinal")
        elif self.prerelease_ordinal is None or self.prerelease_ordinal <= 0:
            raise ValueError("prerelease Kodi release requires a positive ordinal")

    @classmethod
    def parse(cls, value: str) -> KodiReleaseIdentity:
        """Parse the bounded canonical catalog representation."""

        candidate = value.strip()
        match = _RELEASE_PATTERN.fullmatch(candidate)
        if match is None:
            raise ValueError(
                "Kodi release must be major.minor or major.minor-channel.N"
            )
        channel_text = match.group("channel")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            channel=(
                KodiReleaseChannel.STABLE
                if channel_text is None
                else KodiReleaseChannel(channel_text)
            ),
            prerelease_ordinal=(
                None if match.group("ordinal") is None else int(match.group("ordinal"))
            ),
        )

    @property
    def canonical(self) -> str:
        base = f"{self.major}.{self.minor}"
        if self.channel is KodiReleaseChannel.STABLE:
            return base
        return f"{base}-{self.channel.value}.{self.prerelease_ordinal}"

    @property
    def display(self) -> str:
        if self.channel is KodiReleaseChannel.STABLE:
            return f"{self.major}.{self.minor} stable"
        label = (
            "RC"
            if self.channel is KodiReleaseChannel.RC
            else self.channel.value.title()
        )
        return f"{self.major}.{self.minor} {label} {self.prerelease_ordinal}"

    def __str__(self) -> str:
        return self.canonical


def validate_support_tier_for_release(
    release: KodiReleaseIdentity, tier: RuntimeSupportTier
) -> None:
    """Reject channel/tier combinations that confuse immutable identity and policy."""

    if release.channel is KodiReleaseChannel.STABLE:
        if tier is RuntimeSupportTier.PREVIEW:
            raise ValueError("stable Kodi release cannot use preview support tier")
    elif tier not in {RuntimeSupportTier.PREVIEW, RuntimeSupportTier.RETIRED}:
        raise ValueError(
            "prerelease Kodi release must use preview or retired support tier"
        )


def validate_support_transition(
    release: KodiReleaseIdentity,
    current: RuntimeSupportTier,
    desired: RuntimeSupportTier,
) -> None:
    """Validate the initial monotonic support lifecycle."""

    validate_support_tier_for_release(release, desired)
    if current is desired:
        return
    permitted = {
        RuntimeSupportTier.CURRENT: {
            RuntimeSupportTier.LEGACY,
            RuntimeSupportTier.RETIRED,
        },
        RuntimeSupportTier.LEGACY: {RuntimeSupportTier.RETIRED},
        RuntimeSupportTier.PREVIEW: {RuntimeSupportTier.RETIRED},
        RuntimeSupportTier.RETIRED: set(),
    }
    if desired not in permitted[current]:
        raise ValueError(
            f"support transition {current.value} to {desired.value} is not permitted"
        )

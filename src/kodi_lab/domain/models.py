"""Domain representations for registered external Kodi targets."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.kodi_release import KodiReleaseIdentity


class TargetType(StrEnum):
    """Kinds of targets implemented by the current domain slice."""

    EXTERNAL_DEVELOPMENT = "external_development"
    EXTERNAL_PRODUCTION = "external_production"


class TargetAvailability(StrEnum):
    """Whether a target is presently available for use."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class HealthStatus(StrEnum):
    """Canonical aggregate health states."""

    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR = "error"


class ConnectionState(StrEnum):
    """Observed target connection state."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class Target:
    """A normalized Kodi target, independent of transport/runtime adapters."""

    id: UUID
    name: str
    target_type: TargetType
    kodi_version: str | None
    availability: TargetAvailability
    health: HealthStatus
    connection_state: ConnectionState

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        if self.kodi_version is not None:
            _require_text(self.kodi_version, "kodi_version")
            release = KodiReleaseIdentity.parse(self.kodi_version)
            object.__setattr__(self, "kodi_version", release.canonical)

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        """Return the exact typed release identity declared by this target."""

        if self.kodi_version is None:
            raise ValueError("target has no verified Kodi release")
        return KodiReleaseIdentity.parse(self.kodi_version)

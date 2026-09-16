"""Domain representations for targets and managed instances."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.kodi_release import KodiReleaseIdentity

MAX_MANAGED_INSTANCE_NOTES_LENGTH = 4096


class TargetType(StrEnum):
    """Kinds of targets implemented by the current domain slice."""

    MANAGED = "managed"


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


class InstanceType(StrEnum):
    """Canonical managed-instance persistence modes."""

    DISPOSABLE = "disposable"
    PERSISTENT = "persistent"


class InstanceRuntimeState(StrEnum):
    """Durable managed-instance runtime states defined for this slice."""

    RUNNING = "running"
    STOPPED = "stopped"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class Target:
    """A normalized Kodi target, independent of transport/runtime adapters."""

    id: UUID
    name: str
    target_type: TargetType
    kodi_version: str
    availability: TargetAvailability
    health: HealthStatus
    connection_state: ConnectionState

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.kodi_version, "kodi_version")
        release = KodiReleaseIdentity.parse(self.kodi_version)
        object.__setattr__(self, "kodi_version", release.canonical)

    @property
    def kodi_release(self) -> KodiReleaseIdentity:
        """Return the exact typed release identity declared by this target."""

        return KodiReleaseIdentity.parse(self.kodi_version)


@dataclass(frozen=True, slots=True)
class ManagedInstance:
    """Managed-instance data whose identity is its owning managed target."""

    target_id: UUID
    instance_type: InstanceType
    runtime_revision_id: UUID
    runtime_state: InstanceRuntimeState
    start_on_host_boot: bool = False
    notes: str | None = None
    fixture_definition_id: UUID | None = None

    def __post_init__(self) -> None:
        if (
            self.notes is not None
            and len(self.notes) > MAX_MANAGED_INSTANCE_NOTES_LENGTH
        ):
            raise ValueError(
                "managed instance notes must contain at most 4096 Unicode code points"
            )

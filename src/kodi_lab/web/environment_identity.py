"""Environment identity from stored target type, independent of operational state."""

from dataclasses import dataclass
from types import MappingProxyType

from kodi_lab.domain import TargetType


@dataclass(frozen=True)
class EnvironmentIdentity:
    display_name: str
    ownership: str
    icon_path: str


_IDENTITIES = MappingProxyType(
    {
        TargetType.EXTERNAL_PRODUCTION: EnvironmentIdentity(
            display_name="External Production Kodi",
            ownership=(
                "Your household Kodi runs on another device, outside Kodi Lab. "
                "Kodi Lab connects to it with explicitly authorized access."
            ),
            icon_path="M3 11l9-8 9 8 M5 9v12h14V9 M9 21v-7h6v7",
        ),
        TargetType.EXTERNAL_DEVELOPMENT: EnvironmentIdentity(
            display_name="External Development Kodi",
            ownership=(
                "Your test or development Kodi runs on another device, outside "
                "Kodi Lab. Kodi Lab connects to it with explicitly authorized access."
            ),
            icon_path="M9 3h6 M10 3v7L4 20h16l-6-10V3 M7 15h10",
        ),
    }
)


def environment_identity(target_type: TargetType | str) -> EnvironmentIdentity:
    """Never derive category or trust from availability, capabilities or grants."""
    return _IDENTITIES[TargetType(target_type)]

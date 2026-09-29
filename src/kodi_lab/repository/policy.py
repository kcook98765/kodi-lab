"""One reviewed infrastructure namespace policy for repository admission."""

from __future__ import annotations

from kodi_lab.repository.stable import BRIDGE_ID, REPOSITORY_ID

DEVELOPMENT_REPOSITORY_ID = "repository.kodi-lab-dev"
RESERVED_ADDON_IDS = frozenset({REPOSITORY_ID, DEVELOPMENT_REPOSITORY_ID, BRIDGE_ID})


def require_development_addon_id(addon_id: str) -> None:
    if addon_id in RESERVED_ADDON_IDS or addon_id.startswith(
        ("repository.", "service.kodi_lab", "script.kodi_lab")
    ):
        raise ValueError("Kodi Lab infrastructure identity is reserved")
    if not addon_id.startswith("plugin.video."):
        raise ValueError("only plugin.video add-ons may enter the development feed")

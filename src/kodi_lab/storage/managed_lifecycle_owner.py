"""Separate server administrator receipt namespace; never an MCP client/credential."""

from uuid import NAMESPACE_URL, UUID, uuid5

WEB_ADMIN_OWNER = uuid5(
    NAMESPACE_URL, "kodi-lab/web-administrator/managed-lifecycle/v1"
)


def lifecycle_actor(owner: str | UUID) -> str:
    return "web" if str(owner) == str(WEB_ADMIN_OWNER) else f"mcp:{owner}"

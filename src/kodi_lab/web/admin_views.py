"""Compatibility for retired administrator views; never authorization state."""

from urllib.parse import urlsplit

from starlette.requests import Request

ADMIN_VIEW_COOKIE = "kodi_lab_admin_view"


def admin_view_context(request: Request) -> dict[str, object]:
    # Older shared templates interpret a disabled selector as full navigation.
    # Current templates do not use these keys. Never inspect the legacy cookie.
    return {
        "admin_view_enabled": False,
        "admin_view_mode": "development",
    }


def admin_view_return_path(value: str) -> str:
    """Keep page/query/fragment, but never redirect outside this origin."""
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else "/"

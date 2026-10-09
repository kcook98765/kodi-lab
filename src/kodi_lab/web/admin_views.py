"""Non-sensitive, browser-local presentation preference for administrators."""

from urllib.parse import urlsplit

from starlette.requests import Request

ADMIN_VIEW_COOKIE = "kodi_lab_admin_view"


def admin_view_context(request: Request) -> dict[str, object]:
    mode = request.cookies.get(ADMIN_VIEW_COOKIE)
    return {
        "admin_view_enabled": True,
        "admin_view_mode": "development" if mode == "development" else "standard",
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

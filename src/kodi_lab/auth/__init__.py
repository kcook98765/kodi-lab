"""Single-administrator Web authentication."""

from kodi_lab.auth.service import (
    AuthenticatedWebSession,
    LoginThrottler,
    WebAuthenticationService,
)

__all__ = [
    "AuthenticatedWebSession",
    "LoginThrottler",
    "WebAuthenticationService",
]

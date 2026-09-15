"""Minimal immutable records for Web credentials and sessions."""

from dataclasses import dataclass
from datetime import datetime

ADMIN_KEY = "local-admin"


@dataclass(frozen=True, slots=True)
class WebAdminCredential:
    admin_key: str
    password_hash: str
    password_updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebSession:
    token_digest: str
    admin_key: str
    csrf_token: str
    created_at: datetime
    last_used_at: datetime
    absolute_expires_at: datetime

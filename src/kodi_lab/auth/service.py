"""Authentication, opaque sessions, expiration, and login throttling."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.auth.models import ADMIN_KEY, WebSession
from kodi_lab.auth.passwords import PasswordService
from kodi_lab.storage.web_auth import (
    WebAdminCredentialRepository,
    WebSessionRepository,
)

SESSION_TOKEN_BYTES = 32
SESSION_IDLE_LIFETIME = timedelta(hours=12)
SESSION_ABSOLUTE_LIFETIME = timedelta(days=7)
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
_SESSION_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class AuthenticatedWebSession:
    csrf_token: str
    created_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedWebSession:
    token: str
    authenticated: AuthenticatedWebSession


class WebAuthenticationService:
    """Single-admin credential and opaque server-session boundary."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        passwords: PasswordService | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._passwords = passwords or PasswordService()
        self._now = now or (lambda: datetime.now(UTC))

    def credentials_configured(self) -> bool:
        with self._session_factory() as session:
            return WebAdminCredentialRepository(session).get() is not None

    def set_password(self, password: str) -> None:
        password_hash = self._passwords.hash(password)
        updated_at = self._require_utc(self._now())
        with self._session_factory.begin() as session:
            WebSessionRepository(session).delete_all()
            WebAdminCredentialRepository(session).set_password_hash(
                password_hash, updated_at=updated_at
            )

    def login(self, password: str) -> CreatedWebSession | None:
        now = self._require_utc(self._now())
        with self._session_factory.begin() as session:
            credential = WebAdminCredentialRepository(session).get()
            password_hash = None if credential is None else credential.password_hash
            if not self._passwords.verify(password, password_hash):
                return None
            token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            csrf_token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            record = WebSession(
                token_digest=self.token_digest(token),
                admin_key=ADMIN_KEY,
                csrf_token=csrf_token,
                created_at=now,
                last_used_at=now,
                absolute_expires_at=now + SESSION_ABSOLUTE_LIFETIME,
            )
            WebSessionRepository(session).create(record)
        return CreatedWebSession(
            token=token,
            authenticated=AuthenticatedWebSession(
                csrf_token=csrf_token,
                created_at=now,
                absolute_expires_at=record.absolute_expires_at,
            ),
        )

    def resolve(self, token: str | None) -> AuthenticatedWebSession | None:
        if not self._valid_token(token):
            return None
        assert token is not None
        now = self._require_utc(self._now())
        digest = self.token_digest(token)
        with self._session_factory.begin() as session:
            repository = WebSessionRepository(session)
            record = repository.get(digest)
            if record is None or not secrets.compare_digest(
                record.token_digest, digest
            ):
                return None
            idle_expiry = record.last_used_at + SESSION_IDLE_LIFETIME
            if now >= record.absolute_expires_at or now >= idle_expiry:
                repository.delete(digest)
                return None
            if now - record.last_used_at >= SESSION_TOUCH_INTERVAL:
                repository.touch(digest, last_used_at=now)
            return AuthenticatedWebSession(
                csrf_token=record.csrf_token,
                created_at=record.created_at,
                absolute_expires_at=record.absolute_expires_at,
            )

    def logout(self, token: str | None) -> None:
        if not self._valid_token(token):
            return
        assert token is not None
        with self._session_factory.begin() as session:
            WebSessionRepository(session).delete(self.token_digest(token))

    @staticmethod
    def csrf_matches(session: AuthenticatedWebSession, supplied: str | None) -> bool:
        return supplied is not None and secrets.compare_digest(
            session.csrf_token, supplied
        )

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _valid_token(token: str | None) -> bool:
        return token is not None and _SESSION_TOKEN_PATTERN.fullmatch(token) is not None

    @staticmethod
    def _require_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("authentication clock must return an aware datetime")
        return value.astimezone(UTC)


@dataclass(slots=True)
class _ThrottleState:
    failures: int = 0
    blocked_until: float = 0.0


class LoginThrottler:
    """Small per-process failed-login CPU guard for the loopback-only UI."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, _ThrottleState] = {}
        self._lock = threading.Lock()

    def allowed(self, source: str) -> bool:
        with self._lock:
            state = self._states.get(source)
            return state is None or self._clock() >= state.blocked_until

    def record_failure(self, source: str) -> None:
        with self._lock:
            state = self._states.setdefault(source, _ThrottleState())
            state.failures += 1
            if state.failures >= 5:
                state.blocked_until = self._clock() + min(
                    30.0, float(2 ** (state.failures - 4))
                )

    def record_success(self, source: str) -> None:
        with self._lock:
            self._states.pop(source, None)

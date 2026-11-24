"""Narrow Web-admin browser remote for the exact curated Kodi runtimes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.kodi.remote import (
    BrowserRemoteAction,
)

LOGGER = logging.getLogger("kodi_lab.browser_remote.audit")


class RemoteActorCategory(StrEnum):
    WEB_LOCAL_ADMIN = "web_local_admin"
    MCP_CLIENT = "mcp_client"


@dataclass(frozen=True, slots=True)
class RemoteActorContext:
    """Server-created remote actor identity; never accepted from request data."""

    category: RemoteActorCategory
    client_id: UUID | None = None

    def __post_init__(self) -> None:
        if (self.category is RemoteActorCategory.MCP_CLIENT) != (
            self.client_id is not None
        ):
            raise ValueError("remote actor identity does not match its category")

    @classmethod
    def web_local_admin(cls) -> RemoteActorContext:
        return cls(RemoteActorCategory.WEB_LOCAL_ADMIN)

    @classmethod
    def mcp_client(cls, client_id: UUID) -> RemoteActorContext:
        return cls(RemoteActorCategory.MCP_CLIENT, client_id)

    @property
    def audit_interface(self) -> str:
        return "mcp" if self.client_id is not None else "web"

    @property
    def audit_actor(self) -> str:
        return str(self.client_id) if self.client_id is not None else "local_admin"

    @property
    def rate_key(self) -> str:
        return f"mcp:{self.client_id}" if self.client_id is not None else "web"


class BrowserRemoteError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message[:256]
        super().__init__(self.safe_message)


class ScreenshotRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str = Field(max_length=64)
    capture_id: UUID | None = None
    captured_at: datetime | None = None


class BrowserRemoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: UUID
    managed_instance_id: UUID
    action: BrowserRemoteAction
    result: str = Field(max_length=64)
    completed_at: datetime
    screenshot_refresh: ScreenshotRefreshResult


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class BrowserRemoteRateLimiter:
    """Bounded in-process token buckets for browser-remote actions."""

    def __init__(
        self,
        *,
        rate: float = 10.0,
        burst: int = 20,
        idle_ttl_seconds: float = 15 * 60.0,
        max_buckets: int = 4_096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be positive")
        if max_buckets <= 0:
            raise ValueError("max_buckets must be positive")
        self._rate = rate
        self._burst = burst
        self._idle_ttl_seconds = idle_ttl_seconds
        self._max_buckets = max_buckets
        self._clock = clock
        self._buckets: dict[tuple[str, UUID], _Bucket] = {}
        self._lock = Lock()

    def require_allowed(self, session_key: str, instance_id: UUID) -> None:
        now = self._clock()
        key = (session_key, instance_id)
        with self._lock:
            self._evict_expired(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    raise BrowserRemoteError(
                        "visual_rate_limited",
                        "Browser remote action capacity is reached.",
                    )
                bucket = _Bucket(float(self._burst), now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                float(self._burst),
                bucket.tokens + max(0.0, now - bucket.updated_at) * self._rate,
            )
            bucket.updated_at = max(bucket.updated_at, now)
            if bucket.tokens < 1.0:
                raise BrowserRemoteError(
                    "visual_rate_limited", "Browser remote action rate is exceeded."
                )
            bucket.tokens -= 1.0

    def retire_session(self, session_key: str) -> int:
        """Forget all bounded token buckets for an explicitly ended Web session."""

        with self._lock:
            keys = [key for key in self._buckets if key[0] == session_key]
            for key in keys:
                del self._buckets[key]
            return len(keys)

    def bucket_count(self) -> int:
        """Expose bounded process-local accounting for diagnostics and tests."""

        with self._lock:
            return len(self._buckets)

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._buckets.items()
            if now >= bucket.updated_at
            and now - bucket.updated_at >= self._idle_ttl_seconds
        ]
        for key in expired:
            del self._buckets[key]

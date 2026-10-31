"""Bound replay metadata by expiring the request identity itself.

Caller serializes remember/cancel with viewer admission. Clock access has its own
lock so template rendering can safely seed browser clocks without viewer locks.
"""

from __future__ import annotations

import time
from threading import Lock
from uuid import UUID

ATTEMPT_WINDOW_MS = 120_000
ATTEMPT_FUTURE_MS = 5_000
ATTEMPT_CAPACITY = 256


class FutureViewCancellation(ValueError):
    """The request may become valid later; cancellation cannot be confirmed."""


class ViewAttempts:
    def __init__(self) -> None:
        self.seen: dict[tuple[str, UUID], int] = {}
        self.blocked_until = 0
        self._clock_lock = Lock()
        self._wall_ns = time.time_ns()
        self._mono_ns = time.monotonic_ns()

    def clock_ms(self) -> int:
        """Never revive expired IDs after a wall-clock adjustment."""
        with self._clock_lock:
            mono = time.monotonic_ns()
            self._wall_ns = max(
                time.time_ns(), self._wall_ns + max(0, mono - self._mono_ns)
            )
            self._mono_ns = mono
            return self._wall_ns // 1_000_000

    def expires(self, attempt: UUID, now: int | None = None) -> int:
        now = self.clock_ms() if now is None else now
        created = attempt.int >> 80
        expiry = created + ATTEMPT_WINDOW_MS
        if attempt.version != 7 or created > now + ATTEMPT_FUTURE_MS or expiry <= now:
            raise ValueError("view attempt expired or invalid; reload Current Kodi")
        return expiry

    def _prune(self, now: int) -> None:
        self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}

    def _remember(self, owner: str, attempt: UUID, expiry: int, now: int) -> bool:
        key = (owner, attempt)
        self._prune(now)
        if key in self.seen:
            return False
        if now < self.blocked_until or len(self.seen) >= ATTEMPT_CAPACITY:
            # Remember every denied/cancelled identity without allocating a row:
            # admit nothing until all such identities are intrinsically expired.
            self.blocked_until = max(self.blocked_until, expiry, *self.seen.values())
            return False
        self.seen[key] = expiry
        return True

    def claim(self, owner: str, attempt: UUID) -> None:
        now = self.clock_ms()
        expiry = self.expires(attempt, now)
        if not self._remember(owner, attempt, expiry, now):
            raise ValueError("view attempt unavailable")

    def cancel(self, owner: str, attempt: UUID) -> None:
        now = self.clock_ms()
        self._prune(now)
        created = attempt.int >> 80
        if attempt.version != 7 or created + ATTEMPT_WINDOW_MS <= now:
            return  # Permanently inadmissible; native Stop still runs.
        # Future IDs can become admissible later. Refuse cancellation explicitly
        # rather than acknowledging a forgotten Stop or quarantining arbitrarily
        # far into the future.
        if created > now + ATTEMPT_FUTURE_MS:
            raise FutureViewCancellation("future view cancellation unconfirmed")
        self._remember(owner, attempt, created + ATTEMPT_WINDOW_MS, now)

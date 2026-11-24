"""Per-target admission and exact managed-runtime UI observation routing."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from kodi_lab.kodi.ui_state_native import UiObservationError

if TYPE_CHECKING:
    pass


class ObservationGate:
    """Bounded per-target admission; does not lock navigation or Kodi's GUI."""

    def __init__(self, *, timeout: float = 3.0, limit: int = 1024) -> None:
        self._timeout = timeout
        self._limit = limit
        self._guard = threading.Lock()
        self._entries: dict[UUID, tuple[threading.Lock, int]] = {}

    @contextmanager
    def enter(self, target_id: UUID) -> Iterator[None]:
        with self._guard:
            if target_id not in self._entries:
                if len(self._entries) >= self._limit:
                    raise UiObservationError("rate_limited")
                self._entries[target_id] = (threading.Lock(), 0)
            lock, users = self._entries[target_id]
            self._entries[target_id] = (lock, users + 1)
        acquired = False
        try:
            acquired = lock.acquire(timeout=self._timeout)
            if not acquired:
                raise UiObservationError("rate_limited")
            yield
        finally:
            if acquired:
                lock.release()
            with self._guard:
                _, users = self._entries[target_id]
                if users == 1:
                    del self._entries[target_id]
                else:
                    self._entries[target_id] = (lock, users - 1)

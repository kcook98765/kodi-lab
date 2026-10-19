"""One bounded Web-owned idle evaluator; operation receipts survive worker loss."""

import logging
from threading import Event, Thread

from kodi_lab.application.managed_resources import ManagedResourceService


class ManagedIdleWorker:
    def __init__(
        self, resources: ManagedResourceService, *, poll_seconds: float = 30.0
    ) -> None:
        if not 0 < poll_seconds <= 60:
            raise ValueError(
                "managed idle polling must be positive and at most one minute"
            )
        self._resources, self._interval = resources, poll_seconds
        self._closed = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="managed-idle-stop", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._closed.wait(self._interval):
            try:
                self._resources.sweep_idle(cancelled=self._closed.is_set)
            except Exception:
                logging.getLogger(__name__).warning(
                    "Managed idle evaluation unavailable"
                )

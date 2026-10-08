"""Reuse client setup without retaining Kodi service request workers."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

import httpx

from kodi_lab.kodi.bridge import ExternalBridgeClient


class ObservationBridgeClient(ExternalBridgeClient):
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(connect_timeout=0.5, response_timeout=1.0, transport=transport)
        self._setup_lock = Lock()
        self._session: httpx.Client | None = None

    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:  # type: ignore[override]
        with self._setup_lock:
            if self._session is None:
                self._session = httpx.Client(
                    follow_redirects=False,
                    trust_env=False,
                    verify=True,
                    transport=self._transport,
                    headers={"Connection": "close"},
                )
            session = self._session
        yield session

    def close(self) -> None:
        with self._setup_lock:
            if self._session is not None:
                self._session.close()
                self._session = None

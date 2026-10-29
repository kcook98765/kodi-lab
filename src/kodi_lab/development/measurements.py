"""Aggregate deployment telemetry; never contains paths, content, or credentials."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Any


class DeploymentMeasurement:
    """Measure existing work without adding tree traversals or readbacks."""

    def __init__(self, operation_id: str, layer: str) -> None:
        self.operation_id = operation_id
        self.layer = layer
        self._started = time.monotonic()
        self.phases: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.completed = False

    def run[T](self, phase: str, function: Callable[[], T]) -> T:
        started = time.monotonic()
        try:
            return function()
        finally:
            self.phases[phase] = self.phases.get(phase, 0.0) + (
                time.monotonic() - started
            )

    def chunks(self, counter: str, chunks: Iterable[bytes]) -> Iterable[bytes]:
        self.counts.setdefault(counter, 0)
        for chunk in chunks:
            self.counts[counter] += len(chunk)
            yield chunk

    def emit(self) -> None:
        document: dict[str, Any] = {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "layer": self.layer,
            "completed": self.completed,
            "total_seconds": time.monotonic() - self._started,
            "phases_seconds": self.phases,
            "counts": self.counts,
        }
        # An unavailable log sink cannot alter a durable mutation outcome.
        with suppress(Exception):
            logging.getLogger("kodi_lab.deployment.measurement").info(
                "deployment measurement %s", json.dumps(document, sort_keys=True)
            )

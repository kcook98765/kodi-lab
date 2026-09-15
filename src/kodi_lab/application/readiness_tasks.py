"""Bounded, ephemeral per-instance readiness task registry."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from kodi_lab.kodi.readiness import (
    ReadinessCancelledError,
    ReadinessFailureReason,
    ReadinessResult,
    UsabilityState,
)


class ReadinessTaskState(StrEnum):
    IDLE = "idle"
    CHECKING = "checking"
    USABLE = "usable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReadinessTaskSnapshot:
    generation: int
    state: ReadinessTaskState
    result: ReadinessResult | None = None


type ReadinessRunner = Callable[[threading.Event], ReadinessResult]


@dataclass(slots=True)
class _TaskEntry:
    generation: int
    state: ReadinessTaskState
    cancel_event: threading.Event
    future: Future[ReadinessResult] | None = None
    result: ReadinessResult | None = None


class ReadinessTaskRegistry:
    """Keep at most one effective bounded readiness task per instance."""

    def __init__(self, *, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kodi-lab-readiness"
        )
        self._entries: dict[UUID, _TaskEntry] = {}
        self._generations: dict[UUID, int] = {}
        self._lock = threading.Lock()

    def start(
        self, instance_id: UUID, runner: ReadinessRunner
    ) -> ReadinessTaskSnapshot:
        with self._lock:
            current = self._entries.get(instance_id)
            if current is not None and current.state is ReadinessTaskState.CHECKING:
                return self._snapshot(current)
            predecessor = (
                current
                if current is not None
                and current.future is not None
                and not current.future.done()
                else None
            )
            if predecessor is not None:
                predecessor.cancel_event.set()
            generation = self._generations.get(instance_id, 0) + 1
            self._generations[instance_id] = generation
            entry = _TaskEntry(
                generation=generation,
                state=ReadinessTaskState.CHECKING,
                cancel_event=threading.Event(),
            )
            self._entries[instance_id] = entry

            def serial_runner() -> ReadinessResult:
                if predecessor is not None and predecessor.future is not None:
                    with suppress(Exception):
                        predecessor.future.result()
                if entry.cancel_event.is_set():
                    raise ReadinessCancelledError()
                return runner(entry.cancel_event)

            future = self._executor.submit(serial_runner)
            entry.future = future
            snapshot = self._snapshot(entry)

        def complete(completed: Future[ReadinessResult]) -> None:
            self._complete(instance_id, generation, completed)

        future.add_done_callback(complete)
        return snapshot

    def snapshot(self, instance_id: UUID) -> ReadinessTaskSnapshot:
        with self._lock:
            entry = self._entries.get(instance_id)
            if entry is None:
                return ReadinessTaskSnapshot(
                    generation=self._generations.get(instance_id, 0),
                    state=ReadinessTaskState.IDLE,
                )
            return self._snapshot(entry)

    def cancel(self, instance_id: UUID) -> None:
        with self._lock:
            entry = self._entries.get(instance_id)
            if entry is None:
                return
            entry.cancel_event.set()
            entry.state = ReadinessTaskState.CANCELLED
            entry.result = None

    def clear(self, instance_id: UUID) -> None:
        with self._lock:
            entry = self._entries.pop(instance_id, None)
            if entry is not None:
                entry.cancel_event.set()

    def close(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            for entry in entries:
                entry.cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _complete(
        self, instance_id: UUID, generation: int, future: Future[ReadinessResult]
    ) -> None:
        try:
            completed_result = future.result()
        except ReadinessCancelledError:
            state = ReadinessTaskState.CANCELLED
            result = None
        except Exception:
            state = ReadinessTaskState.FAILED
            result = None
        else:
            result = completed_result
            if completed_result.usability_state is UsabilityState.USABLE:
                state = ReadinessTaskState.USABLE
            elif (
                completed_result.failure_reason
                is ReadinessFailureReason.READINESS_TIMEOUT
            ):
                state = ReadinessTaskState.TIMED_OUT
            else:
                state = ReadinessTaskState.FAILED
        with self._lock:
            entry = self._entries.get(instance_id)
            if entry is None or entry.generation != generation:
                return
            if entry.state is ReadinessTaskState.CANCELLED:
                return
            entry.state = state
            entry.result = result

    @staticmethod
    def _snapshot(entry: _TaskEntry) -> ReadinessTaskSnapshot:
        return ReadinessTaskSnapshot(entry.generation, entry.state, entry.result)

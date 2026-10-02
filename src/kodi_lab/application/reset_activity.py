"""Bounded process-local semantic reset activity and result tracking."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from uuid import UUID


class ResetActivityState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    STOPPING = "stopping"
    DEPROVISIONING = "deprovisioning"
    PROVISIONING = "provisioning"
    APPLYING_FIXTURE = "applying_fixture"
    STARTING_FOR_FIXTURE = "starting_for_fixture"
    WAITING_READINESS = "waiting_readiness"
    SCANNING_FIXTURE = "scanning_fixture"
    VERIFYING_FIXTURE = "verifying_fixture"
    APPLYING_POST_SCAN_STATE = "applying_post_scan_state"
    VERIFYING_POST_SCAN_STATE = "verifying_post_scan_state"
    FINAL_STOPPING = "final_stopping"
    FINAL_VERIFYING = "final_verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ResetOutcome(StrEnum):
    NONE = "none"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


_ACTIVE_STATES = (
    ResetActivityState.QUEUED,
    ResetActivityState.PREFLIGHT,
    ResetActivityState.STOPPING,
    ResetActivityState.DEPROVISIONING,
    ResetActivityState.PROVISIONING,
    ResetActivityState.APPLYING_FIXTURE,
    ResetActivityState.STARTING_FOR_FIXTURE,
    ResetActivityState.WAITING_READINESS,
    ResetActivityState.SCANNING_FIXTURE,
    ResetActivityState.VERIFYING_FIXTURE,
    ResetActivityState.APPLYING_POST_SCAN_STATE,
    ResetActivityState.VERIFYING_POST_SCAN_STATE,
    ResetActivityState.FINAL_STOPPING,
    ResetActivityState.FINAL_VERIFYING,
)
_ACTIVE_ORDER = {state: index for index, state in enumerate(_ACTIVE_STATES)}


class ResetActivityConflictError(RuntimeError):
    """A process-local activity entry cannot accept the requested transition."""


class ResetActivityCapacityError(RuntimeError):
    """The bounded registry has no completed entry available for eviction."""


@dataclass(frozen=True, slots=True)
class ResetActivitySnapshot:
    generation: int
    state: ResetActivityState
    last_outcome: ResetOutcome = ResetOutcome.NONE
    failure_phase: str | None = None
    failure_reason: str | None = None
    reconciliation_required: bool = False

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_ORDER


class ResetActivityRegistry:
    """Retain a small bounded set of ephemeral per-instance reset outcomes."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("reset activity registry must retain at least one entry")
        self._max_entries = max_entries
        self._entries: OrderedDict[UUID, ResetActivitySnapshot] = OrderedDict()
        self._generations: dict[UUID, int] = {}
        self._lock = Lock()

    def begin(self, instance_id: UUID) -> ResetActivitySnapshot:
        with self._lock:
            current = self._entries.get(instance_id)
            if current is not None and current.active:
                raise ResetActivityConflictError("reset activity is already active")
            if instance_id not in self._entries:
                self._make_room()
            generation = self._generations.get(instance_id, 0) + 1
            self._generations[instance_id] = generation
            snapshot = ResetActivitySnapshot(
                generation=generation,
                state=ResetActivityState.PREFLIGHT,
            )
            self._entries[instance_id] = snapshot
            self._entries.move_to_end(instance_id)
            return snapshot

    def transition(
        self,
        instance_id: UUID,
        generation: int,
        state: ResetActivityState,
    ) -> ResetActivitySnapshot:
        if state not in _ACTIVE_ORDER:
            raise ValueError("reset transition must be an active semantic phase")
        with self._lock:
            current = self._require_current(instance_id, generation)
            if (
                not current.active
                or _ACTIVE_ORDER[state] < _ACTIVE_ORDER[current.state]
            ):
                raise ResetActivityConflictError("reset activity transition is invalid")
            updated = ResetActivitySnapshot(generation=generation, state=state)
            self._entries[instance_id] = updated
            return updated

    def succeed(self, instance_id: UUID, generation: int) -> ResetActivitySnapshot:
        return self._complete(
            instance_id,
            generation,
            ResetActivitySnapshot(
                generation=generation,
                state=ResetActivityState.SUCCEEDED,
                last_outcome=ResetOutcome.SUCCEEDED,
            ),
        )

    def fail(
        self,
        instance_id: UUID,
        generation: int,
        *,
        failure_phase: str,
        failure_reason: str,
        reconciliation_required: bool,
    ) -> ResetActivitySnapshot:
        reason = failure_reason.strip()[:256] or "Reset could not be completed."
        outcome = (
            ResetOutcome.RECONCILIATION_REQUIRED
            if reconciliation_required
            else ResetOutcome.FAILED
        )
        state = (
            ResetActivityState.RECONCILIATION_REQUIRED
            if reconciliation_required
            else ResetActivityState.FAILED
        )
        return self._complete(
            instance_id,
            generation,
            ResetActivitySnapshot(
                generation=generation,
                state=state,
                last_outcome=outcome,
                failure_phase=failure_phase[:64],
                failure_reason=reason,
                reconciliation_required=reconciliation_required,
            ),
        )

    def snapshot(self, instance_id: UUID) -> ResetActivitySnapshot:
        with self._lock:
            return self._entries.get(
                instance_id,
                ResetActivitySnapshot(0, ResetActivityState.IDLE),
            )

    def clear(self, instance_id: UUID) -> None:
        with self._lock:
            current = self._entries.get(instance_id)
            if current is not None and current.active:
                raise ResetActivityConflictError(
                    "active reset activity cannot be cleared"
                )
            self._entries.pop(instance_id, None)
            self._generations.pop(instance_id, None)

    def _complete(
        self,
        instance_id: UUID,
        generation: int,
        snapshot: ResetActivitySnapshot,
    ) -> ResetActivitySnapshot:
        with self._lock:
            current = self._require_current(instance_id, generation)
            if not current.active:
                raise ResetActivityConflictError("reset activity is not active")
            self._entries[instance_id] = snapshot
            self._entries.move_to_end(instance_id)
            return snapshot

    def _require_current(
        self, instance_id: UUID, generation: int
    ) -> ResetActivitySnapshot:
        current = self._entries.get(instance_id)
        if current is None or current.generation != generation:
            raise ResetActivityConflictError("reset activity generation is stale")
        return current

    def _make_room(self) -> None:
        while len(self._entries) >= self._max_entries:
            completed = next(
                (
                    instance_id
                    for instance_id, snapshot in self._entries.items()
                    if not snapshot.active
                ),
                None,
            )
            if completed is None:
                raise ResetActivityCapacityError(
                    "reset activity registry capacity is exhausted"
                )
            self._entries.pop(completed)
            self._generations.pop(completed, None)

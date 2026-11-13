"""Payload-free viewer lifecycle records with process-local bounded retention."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import UUID

LOGGER = "kodi_lab.viewer.diagnostic"
MAX_BYTES = 65536
EVENTS = frozenset(
    {
        "web_accepted",
        "web_ended",
        "web_rejected",
        "helper_started",
        "helper_ended",
        "helper_rejected",
        "ownership_renewed",
        "binding_checked",
    }
)
REASONS = frozenset(
    {
        "pending",
        "failed",
        "cancelled",
        "timeout",
        "transport_error",
        "internal_error",
        "task_completed",
        "authorization_rejected",
        "ticket_rejected",
        "binding_changed",
        "ownership_rejected",
        "session_ended",
        "browser_disconnect",
        "invalid_browser_message",
        "input_limit",
        "broker_eof",
        "worker_refused",
        "capacity",
        "admission_rejected",
        "web_lease_expired",
        "control_lease_expired",
        "revoked",
        "disconnect",
        "exec_eof",
        "input_revoked",
        "invalid_envelope",
        "invalid_docker_stream",
        "helper_stderr",
        "arming_denied",
        "backlog_exceeded",
        "consumer_stalled",
    }
)
PHASES = frozenset(
    {
        "binding_check",
        "current_check",
        "broker_connect",
        "broker_handshake",
        "ws_accept",
        "streaming",
        "helper_create",
        "helper_attach",
        "helper_stream",
        "ownership_lock",
        "ownership_update",
        "ownership_commit",
    }
)
WS_CODES = frozenset(
    {
        1000,
        1001,
        1002,
        1003,
        1005,
        1006,
        1007,
        1008,
        1009,
        1010,
        1011,
        1012,
        1013,
        1014,
        1015,
        4403,
    }
)
WATCHDOG_PHASES = frozenset(
    {
        "not_started",
        "check_queue",
        "auth_check",
        "binding_check",
        "control_check",
        "ownership_check",
        "lease_write",
        "sleep",
    }
)
BINDING_PHASES = frozenset(
    {
        "auth",
        "binding",
        "input_exclusion",
        "capability_before",
        "broker_lookup",
        "capability_after",
        "catalog_instance",
        "runtime_reconciliation",
        "operation_reconciliation",
        "runtime_profile",
        "development_binding",
        "mutation_exclusion",
        "foreground",
        "ownership",
        "capability",
        "provisioning",
        "docker_client",
        "docker_inventory",
        "docker_container",
        "docker_image",
        "docker_validation",
    }
)
BINDING_ORIGINS = frozenset({"web_watchdog", "broker_lookup", "broker_validation"})
SLOW_BINDING_SECONDS = 0.25
_binding_timing: ContextVar[
    tuple[dict[str, int], dict[str, int], dict[str, int]] | None
] = ContextVar("viewer_binding_timing", default=None)


@contextmanager
def binding_phase(name: str) -> Iterator[None]:
    """Inclusive elapsed and current-thread CPU time, never a causal label."""
    timing = _binding_timing.get()
    if timing is None or name not in BINDING_PHASES:
        yield
        return
    elapsed, cpu, calls = timing
    began, cpu_began = time.monotonic_ns(), time.thread_time_ns()
    try:
        yield
    finally:
        elapsed[name] = elapsed.get(name, 0) + time.monotonic_ns() - began
        cpu[name] = cpu.get(name, 0) + time.thread_time_ns() - cpu_began
        calls[name] = calls.get(name, 0) + 1


@contextmanager
def binding_timing(
    origin: str,
    instance: UUID,
    *,
    attempt: UUID | None = None,
    queued_at: float | None = None,
) -> Iterator[None]:
    """Emit only slow workers, in their own finally even after caller timeout.

    Off-CPU time includes I/O, locks and scheduling; it cannot identify a writer
    or attribute a storage delay. Nested phase durations overlap intentionally.
    No timing value is consumed by authorization or changes a deadline.
    """
    began, cpu_began = time.monotonic(), time.thread_time_ns()
    queue = 0.0 if queued_at is None else max(0.0, began - queued_at)
    elapsed: dict[str, int] = {}
    cpu: dict[str, int] = {}
    calls: dict[str, int] = {}
    token = _binding_timing.set((elapsed, cpu, calls))
    reason = "internal_error"
    try:
        yield
        reason = "task_completed"
    finally:
        duration = time.monotonic() - began
        cpu_duration = time.thread_time_ns() - cpu_began
        _binding_timing.reset(token)
        if queue + duration >= SLOW_BINDING_SECONDS:
            diagnostic(
                "binding_checked",
                instance=instance,
                attempt=attempt,
                binding_origin=origin,
                reason=reason,
                elapsed_ms=int((queue + duration) * 1000),
                queue_ms=int(queue * 1000),
                worker_ms=int(duration * 1000),
                worker_cpu_ms=cpu_duration // 1_000_000,
                phase_ms={name: value // 1_000_000 for name, value in elapsed.items()},
                phase_cpu_ms={name: value // 1_000_000 for name, value in cpu.items()},
                phase_calls=calls,
            )


def diagnostic(
    event: str,
    *,
    attempt: UUID | None = None,
    instance: UUID | None = None,
    **fields: object,
) -> None:
    """Accept no free text, exception messages, identities or transport payloads."""
    if event not in EVENTS:
        return
    data: dict[str, object] = {"event": event, "at_ms": int(time.time() * 1000)}
    for key, identity_value in (("attempt", attempt), ("instance", instance)):
        if isinstance(identity_value, UUID):
            data[key] = str(identity_value)
    for key, value in fields.items():
        if key in {"reason", "incoming", "outgoing", "watchdog"}:
            if isinstance(value, str) and value in REASONS:
                data[key] = value
        elif key == "phase":
            if isinstance(value, str) and value in PHASES:
                data[key] = value
        elif key == "watchdog_phase":
            if isinstance(value, str) and value in WATCHDOG_PHASES:
                data[key] = value
        elif key == "binding_origin":
            if isinstance(value, str) and value in BINDING_ORIGINS:
                data[key] = value
        elif key in {"phase_ms", "phase_cpu_ms", "phase_calls"}:
            if isinstance(value, dict):
                data[key] = {
                    name: duration
                    for name, duration in value.items()
                    if isinstance(name, str)
                    and name in BINDING_PHASES
                    and type(duration) is int
                    and 0 <= duration <= 10**12
                }
        elif key in {"ws_code", "close_requested_code"}:
            if type(value) is int and value in WS_CODES:
                data[key] = value
        elif key == "exit_code":
            if type(value) is int and 0 <= value <= 255:
                data[key] = value
        elif key in {"accepted", "armed", "terminated", "cleanup_completed"}:
            if type(value) is bool:
                data[key] = value
        elif (
            key
            in {
                "elapsed_ms",
                "startup_ms",
                "teardown_ms",
                "bytes_down",
                "bytes_up",
                "watchdog_check_ms",
                "watchdog_gap_ms",
                "watchdog_write_gap_ms",
                "gc_pause_ms",
                "web_renew_gap_ms",
                "lock_ms",
                "update_ms",
                "commit_ms",
                "validation_ms",
                "iteration_gap_ms",
                "queue_ms",
                "worker_ms",
                "worker_cpu_ms",
            }
            and type(value) is int
            and 0 <= value <= 10**12
        ):
            data[key] = value
    # Diagnostics must never interfere with admission, fencing or teardown.
    with suppress(Exception):
        logging.getLogger(LOGGER).info(
            json.dumps(data, separators=(",", ":"), sort_keys=True)
        )


def configure_diagnostics() -> None:
    """Keep at most two 64 KiB files in a private ephemeral directory per process.

    Installed containers already provide /tmp as tmpfs. No shared state, Docker
    configuration or credentials are needed. The directory is owned mode 0700;
    records do not propagate into an independently retained process log.
    """
    logger = logging.getLogger(LOGGER)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    try:
        directory = Path(tempfile.mkdtemp(prefix="kodi-lab-viewer-"))
        handler = RotatingFileHandler(
            directory / "events.jsonl", maxBytes=MAX_BYTES, backupCount=1
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    except OSError:
        # A full/read-only temp directory cannot change viewer behavior.
        logger.addHandler(logging.NullHandler())

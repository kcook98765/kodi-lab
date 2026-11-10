"""Payload-free viewer lifecycle records with process-local bounded retention."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from contextlib import suppress
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
            key in {"elapsed_ms", "startup_ms", "teardown_ms", "bytes_down", "bytes_up"}
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

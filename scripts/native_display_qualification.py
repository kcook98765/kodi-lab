"""Read timestamped display evidence even when its caller completed late."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def await_menu_display(
    fetch: Callable[[], list[dict[str, Any]]],
    expected: str,
    sent_ms: float,
    *,
    window_ms: float = 1100,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    record: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Accept only an in-window pixel observation, including buffered ones.

    An MCP response may arrive after an already displayed menu change. Always
    inspect the bounded browser buffer once before declaring the display late.
    Fetch remains subject to the caller's existing browser IPC timeout.
    """
    if window_ms <= 0:
        raise ValueError("Display observation window must be positive")
    while True:
        rows = fetch()
        if record is not None:
            record(rows)
        match = next(
            (
                r
                for r in rows
                if r["state"] == expected and 0 <= r["at"] - sent_ms <= window_ms
            ),
            None,
        )
        if match is not None:
            return match
        remaining = sent_ms + window_ms - clock() * 1000
        if remaining <= 0:
            raise RuntimeError(f"No displayed menu {expected} within {window_ms:g}ms")
        sleep(min(0.03, remaining / 1000))

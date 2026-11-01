"""Late polling cannot discard timely pixels or accept late display changes."""

from typing import Any

import pytest

from scripts.native_display_qualification import await_menu_display


def test_late_caller_reads_timely_buffer() -> None:
    journal: list[list[dict[str, Any]]] = []
    result = await_menu_display(
        lambda: [{"at": 100600, "state": "B"}],
        "B",
        100000,
        clock=lambda: 102,
        record=journal.append,
    )
    assert result["at"] == 100600 and len(journal) == 1


@pytest.mark.parametrize("at,state", [(101101, "B"), (99999, "B"), (100600, "A")])
def test_late_stale_or_wrong_pixels_rejected(at: int, state: str) -> None:
    with pytest.raises(RuntimeError, match="within 1100ms"):
        await_menu_display(
            lambda: [{"at": at, "state": state}], "B", 100000, clock=lambda: 102
        )


def test_ontime_polling_still_waits() -> None:
    now = [100.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    def fetch() -> list[dict[str, Any]]:
        return [] if now[0] < 100.6 else [{"at": 100600, "state": "B"}]

    assert (
        await_menu_display(fetch, "B", 100000, clock=lambda: now[0], sleep=sleep)["at"]
        == 100600
    )


def test_invalid_window_rejected_before_fetch() -> None:
    with pytest.raises(ValueError):
        await_menu_display(lambda: [], "B", 0, window_ms=0)

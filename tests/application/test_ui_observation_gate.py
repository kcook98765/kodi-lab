from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from kodi_lab.application.ui_state import ObservationGate
from kodi_lab.kodi.ui_state_native import UiObservationError


def test_serializes_same_target_but_not_other_target() -> None:
    gate = ObservationGate(timeout=1)
    target, other = uuid4(), uuid4()
    waiting = threading.Event()
    entered = threading.Event()

    def second() -> None:
        waiting.set()
        with gate.enter(target):
            entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with gate.enter(target):
            future = pool.submit(second)
            assert waiting.wait(1) and not entered.is_set()
            with gate.enter(other):
                assert not entered.is_set()
        future.result(timeout=1)
    assert entered.is_set() and not gate._entries


def test_releases_after_error_and_wait_timeout() -> None:
    gate = ObservationGate(timeout=0.01)
    target = uuid4()
    with pytest.raises(ValueError), gate.enter(target):
        raise ValueError("provider failed")
    with (
        gate.enter(target),
        pytest.raises(UiObservationError, match="rate_limited"),
        gate.enter(target),
    ):
        pass
    with gate.enter(target):
        pass
    assert not gate._entries

"""Cancellation tombstones can expire only when the request itself cannot recur."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.web.novnc import ChangeMode, PrototypeViewer, StartView
from kodi_lab.web.view_attempts import ATTEMPT_CAPACITY, ATTEMPT_WINDOW_MS, ViewAttempts

from .conftest import INSTANCE_ID, REVISION_ID
from .view_attempt_fixture import view_attempt


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    ticks = [1_800_000_000_000, 0]
    monkeypatch.setattr(
        "kodi_lab.web.view_attempts.time.time_ns", lambda: ticks[0] * 1_000_000
    )
    monkeypatch.setattr(
        "kodi_lab.web.view_attempts.time.monotonic_ns", lambda: ticks[1] * 1_000_000
    )
    return ticks


def viewer() -> PrototypeViewer:
    return PrototypeViewer(
        Path("/tmp/unused-view-attempts.sock"),
        lambda _: ViewBinding(
            "a" * 64, "sha256:" + "b" * 64, "one", (str(REVISION_ID), 1)
        ),
        registry=LiveDisplaySessionRegistry(),
    )


def test_more_than_lifetime_limit_with_bounded_metadata(clock: list[int]) -> None:
    service = viewer()
    old = view_attempt(clock[0])
    for index in range(600):
        attempt = old if index == 0 else view_attempt(clock[0])
        item = service.start(INSTANCE_ID, "owner", StartView(attempt=attempt))
        service.end(item)
        assert len(service.attempts.seen) <= 121
        clock[0] += 1000
        clock[1] += 1000
    with pytest.raises(ValueError, match="expired"):
        service.start(INSTANCE_ID, "owner", StartView(attempt=old))
    assert service.registry.active_count() == 0


@pytest.mark.parametrize("offset", [-ATTEMPT_WINDOW_MS, 5001])
def test_expired_and_future_request_ids_fail_closed(
    clock: list[int], offset: int
) -> None:
    service = viewer()
    with pytest.raises(ValueError, match="expired or invalid"):
        service.start(
            INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0] + offset))
        )
    assert not service.attempts.seen and not service.attachments


def test_untimed_request_ids_are_never_admitted(clock: list[int]) -> None:
    with pytest.raises(ValueError, match="expired or invalid"):
        viewer().start(INSTANCE_ID, "owner", StartView(attempt=uuid4()))


@pytest.mark.parametrize("cancel", [True, False])
def test_saturation_cannot_forget_denied_or_cancelled_ids(
    clock: list[int], cancel: bool
) -> None:
    service = viewer()
    for _ in range(ATTEMPT_CAPACITY):
        service.stop("owner", view_attempt(clock[0]))
    pending = view_attempt(clock[0] + 5000)
    if cancel:
        service.stop("owner", pending)
    else:
        with pytest.raises(ValueError, match="unavailable"):
            service.start(INSTANCE_ID, "owner", StartView(attempt=pending))
    assert len(service.attempts.seen) == ATTEMPT_CAPACITY
    clock[0] += ATTEMPT_WINDOW_MS
    clock[1] += ATTEMPT_WINDOW_MS
    with pytest.raises(ValueError, match="unavailable"):
        service.start(INSTANCE_ID, "owner", StartView(attempt=pending))
    assert len(service.attempts.seen) == 0
    clock[0] += 5001
    clock[1] += 5001
    with pytest.raises(ValueError, match="expired"):
        service.start(INSTANCE_ID, "owner", StartView(attempt=pending))
    item = service.start(
        INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0]))
    )
    service.end(item)


def test_stop_cancel_before_start_and_cross_owner_collision(clock: list[int]) -> None:
    service = viewer()
    cancelled = view_attempt(clock[0])
    service.stop("owner", cancelled)
    with pytest.raises(ValueError, match="unavailable"):
        service.start(INSTANCE_ID, "owner", StartView(attempt=cancelled))
    item = service.start(
        INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0]))
    )
    with pytest.raises(ValueError, match="unavailable"):
        service.start(uuid4(), "other", StartView(attempt=item.attempt))
    service.stop("other", item.attempt)
    assert not item.session.cancelled.is_set()
    service.end(item)


def test_expired_id_stop_still_retires_active_attachment(clock: list[int]) -> None:
    service = viewer()
    item = service.start(
        INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0]))
    )
    service.consume(INSTANCE_ID, "owner", item.ticket)
    clock[0] += ATTEMPT_WINDOW_MS
    clock[1] += ATTEMPT_WINDOW_MS
    service.stop("owner", item.attempt)
    assert item.session.cancelled.is_set()
    assert service.attachments[item.attempt] is item  # Worker proof still required.
    service.end(item)
    assert service.registry.active_count() == 0


def test_clock_rollback_does_not_revive_expired_ids(clock: list[int]) -> None:
    attempts = ViewAttempts()
    original = view_attempt(clock[0])
    attempts.claim("owner", original)
    clock[1] += ATTEMPT_WINDOW_MS
    clock[0] -= 30_000
    with pytest.raises(ValueError, match="expired"):
        attempts.claim("owner", original)
    advanced = attempts.clock_ms()
    clock[0] += 1_000_000
    assert attempts.clock_ms() > advanced
    advanced = attempts.clock_ms()
    clock[0] -= 2_000_000
    clock[1] += 1000
    assert attempts.clock_ms() == advanced + 1000
    with pytest.raises(ValueError, match="expired"):
        attempts.claim("owner", original)


def test_delayed_authority_rechecks_request_expiry(clock: list[int]) -> None:
    service = viewer()
    authority = service.authority

    def delayed(instance: Any) -> ViewBinding:
        clock[0] += ATTEMPT_WINDOW_MS
        clock[1] += ATTEMPT_WINDOW_MS
        return authority(instance)

    service.authority = delayed
    with pytest.raises(ValueError, match="expired"):
        service.start(INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0])))
    assert not service.attachments and service.registry.active_count() == 0


def test_invalid_handoff_does_not_retire_existing_view(clock: list[int]) -> None:
    service = viewer()
    service.ownership = cast(
        Any, SimpleNamespace(create=lambda *_: None, revoke=lambda *_: None)
    )
    item = service.start(
        INSTANCE_ID, "owner", StartView(attempt=view_attempt(clock[0]))
    )
    with pytest.raises(ValueError, match="expired or invalid"):
        service.handoff(
            INSTANCE_ID,
            "owner",
            ChangeMode(attempt=item.attempt, next_attempt=uuid4()),
            "view-only",
        )
    assert not item.session.cancelled.is_set()
    service.end(item)


@pytest.mark.parametrize("saturated", [False, True])
def test_future_stop_is_refused_instead_of_falsely_confirmed(
    clock: list[int], saturated: bool
) -> None:
    service = viewer()
    if saturated:
        for _ in range(ATTEMPT_CAPACITY):
            service.stop("owner", view_attempt(clock[0]))
    offset = ATTEMPT_WINDOW_MS + 1 if saturated else 5001
    pending = view_attempt(clock[0] + offset)
    with pytest.raises(ValueError, match="future view cancellation unconfirmed"):
        service.stop("owner", pending)
    assert service.attempts.blocked_until == 0  # No unbounded future quarantine.
    clock[0] += offset
    clock[1] += offset
    # Cancellation was explicitly refused, never falsely acknowledged. A fresh
    # later admission is possible once the original future ID is valid.
    item = service.start(INSTANCE_ID, "owner", StartView(attempt=pending))
    service.end(item)

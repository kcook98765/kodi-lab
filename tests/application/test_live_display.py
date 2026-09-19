from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from kodi_lab.application.live_display import (
    DISPLAY_SESSION_LIFETIME_SECONDS,
    LiveDisplayError,
    LiveDisplaySession,
    LiveDisplaySessionRegistry,
)


def session(instance_id: UUID) -> LiveDisplaySession:
    return LiveDisplaySession(
        session_id=uuid4(),
        instance_id=instance_id,
        runtime_revision_id=uuid4(),
        container_id="a" * 64,
        started_at=datetime.now(UTC),
        started_monotonic=10.0,
    )


def test_registry_enforces_instance_and_global_limits_and_releases_slots() -> None:
    registry = LiveDisplaySessionRegistry(global_limit=2)
    first = session(uuid4())
    same_instance = session(first.instance_id)
    second = session(uuid4())
    third = session(uuid4())

    registry.acquire(first)
    with pytest.raises(LiveDisplayError, match="already active") as conflict:
        registry.acquire(same_instance)
    assert conflict.value.code == "display_conflict"
    assert registry.active_count() == 1
    registry.acquire(second)
    with pytest.raises(LiveDisplayError, match="capacity") as limited:
        registry.acquire(third)
    assert limited.value.code == "display_rate_limited"
    assert registry.active_count() == 2

    registry.release(first)
    registry.acquire(third)
    assert registry.active_count() == 2
    registry.release(second)
    registry.release(third)
    assert registry.active_count() == 0


def test_registry_cancellation_is_process_local_and_nonblocking() -> None:
    registry = LiveDisplaySessionRegistry()
    active = session(uuid4())
    registry.acquire(active)
    assert registry.cancel(active.instance_id) is True
    assert active.cancelled.is_set()
    assert registry.cancel(uuid4()) is False
    registry.release(active)


def test_display_lifetime_constant_is_exactly_fifteen_minutes() -> None:
    assert DISPLAY_SESSION_LIFETIME_SECONDS == 900.0

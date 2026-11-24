from uuid import UUID

import pytest

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteRateLimiter,
)

from .conftest import INSTANCE_ID


def test_rate_limiter_burst_refill_and_bucket_isolation() -> None:
    now = 100.0
    limiter = BrowserRemoteRateLimiter(clock=lambda: now)
    for _ in range(20):
        limiter.require_allowed("session-a", INSTANCE_ID)
    try:
        limiter.require_allowed("session-a", INSTANCE_ID)
    except Exception as error:
        assert getattr(error, "code", None) == "visual_rate_limited"
    else:
        raise AssertionError("burst limit was not enforced")
    limiter.require_allowed("session-b", INSTANCE_ID)
    other_instance = INSTANCE_ID.__class__("00000000-0000-4000-8000-000000000102")
    limiter.require_allowed("session-a", other_instance)
    now += 0.100001
    limiter.require_allowed("session-a", INSTANCE_ID)


def test_rate_limiter_expires_idle_buckets_without_evicting_active_buckets() -> None:
    now = 100.0
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: now, idle_ttl_seconds=900.0, max_buckets=4
    )
    first = UUID("00000000-0000-4000-8000-000000000201")
    second = UUID("00000000-0000-4000-8000-000000000202")
    third = UUID("00000000-0000-4000-8000-000000000203")

    limiter.require_allowed("session-a", first)
    now += 899.0
    limiter.require_allowed("session-a", first)
    limiter.require_allowed("session-b", second)
    assert limiter.bucket_count() == 2

    now += 2.0
    limiter.require_allowed("session-c", third)
    assert limiter.bucket_count() == 3

    now += 899.0
    limiter.require_allowed("session-d", UUID(int=204))
    assert limiter.bucket_count() == 2


def test_rate_limiter_new_bucket_after_expiry_starts_cleanly() -> None:
    now = 100.0
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: now, idle_ttl_seconds=900.0, max_buckets=1
    )
    for _ in range(20):
        limiter.require_allowed("session-a", INSTANCE_ID)
    with pytest.raises(BrowserRemoteError) as limited:
        limiter.require_allowed("session-a", INSTANCE_ID)
    assert limited.value.code == "visual_rate_limited"

    now += 900.0
    for _ in range(20):
        limiter.require_allowed("session-a", INSTANCE_ID)
    assert limiter.bucket_count() == 1


def test_rate_limiter_hard_ceiling_bounds_many_hot_keys() -> None:
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: 100.0, idle_ttl_seconds=900.0, max_buckets=32
    )
    for index in range(32):
        limiter.require_allowed(f"session-{index}", UUID(int=index + 1))
    for index in range(32, 1_032):
        with pytest.raises(BrowserRemoteError) as limited:
            limiter.require_allowed(f"session-{index}", UUID(int=index + 1))
        assert limited.value.code == "visual_rate_limited"
        assert limiter.bucket_count() == 32


def test_rate_limiter_hot_bucket_enforcement_survives_capacity_pressure() -> None:
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: 100.0, idle_ttl_seconds=900.0, max_buckets=2
    )
    for _ in range(20):
        limiter.require_allowed("session-hot", INSTANCE_ID)
    limiter.require_allowed("session-second", UUID(int=302))
    with pytest.raises(BrowserRemoteError) as capacity:
        limiter.require_allowed("session-third", UUID(int=303))
    assert capacity.value.code == "visual_rate_limited"
    with pytest.raises(BrowserRemoteError) as still_hot:
        limiter.require_allowed("session-hot", INSTANCE_ID)
    assert still_hot.value.code == "visual_rate_limited"
    assert limiter.bucket_count() == 2


def test_rate_limiter_ignores_injected_monotonic_clock_rollback() -> None:
    now = 100.0
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: now, idle_ttl_seconds=900.0, max_buckets=2
    )
    for _ in range(20):
        limiter.require_allowed("session-hot", INSTANCE_ID)
    now = 50.0
    with pytest.raises(BrowserRemoteError) as limited:
        limiter.require_allowed("session-hot", INSTANCE_ID)
    assert limited.value.code == "visual_rate_limited"
    assert limiter.bucket_count() == 1

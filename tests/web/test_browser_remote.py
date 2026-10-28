import time
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteRateLimiter,
)
from kodi_lab.application.browser_remote_coordination import (
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.application.mutations import (
    ManagedInstanceMutationConflictError,
    ManagedInstanceMutationCoordinator,
)
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.kodi.remote import BrowserRemoteAction

from .conftest import INSTANCE_ID, ORIGIN, FakeBroker, login


def make_ready(client: TestClient, csrf: str) -> None:
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/provision", json={}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/start", json={}, headers=headers
        ).status_code
        == 200
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()[
            "screenshot_available_now"
        ]:
            return
        time.sleep(0.01)
    raise AssertionError("instance did not become ready")


def test_remote_route_security_and_strict_closed_body(client: TestClient) -> None:
    url = f"/api/instances/{INSTANCE_ID}/remote-actions"
    assert client.post(url, json={"action": "up"}).status_code == 401
    csrf = login(client)
    assert client.post(url, json={"action": "up"}).status_code == 403
    assert (
        client.post(
            url, json={"action": "up"}, headers={"Origin": "http://evil"}
        ).status_code
        == 403
    )
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(url, json={"action": "execute"}, headers=headers).status_code == 422
    )
    assert (
        client.post(
            url,
            json={"action": "up", "method": "Input.Up"},
            headers=headers,
        ).status_code
        == 422
    )


def test_navigation_refreshes_once_and_returns_latest_metadata(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/remote-actions",
        json={"action": "left"},
        headers=headers,
    )
    assert response.status_code == 200
    document = response.json()
    assert document["result"] == "succeeded"
    assert document["action"] == "left"
    assert document["screenshot_refresh"]["result"] == "succeeded"
    assert document["screenshot_refresh"]["capture_id"] is not None
    assert document["screenshot_refresh"]["captured_at"] is not None
    assert broker.remote_calls == [BrowserRemoteAction.LEFT]
    assert broker.screenshot_calls == 1


def test_refresh_failure_preserves_prior_and_remote_stays_succeeded(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    baseline = client.post(
        f"/api/instances/{INSTANCE_ID}/screenshots", json={}, headers=headers
    ).json()["capture_id"]
    broker.screenshot_error = BrokerUnavailableError("offline")
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/remote-actions",
        json={"action": "back"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["result"] == "succeeded"
    assert response.json()["screenshot_refresh"]["result"] == (
        "screenshot_capture_unavailable"
    )
    assert (
        client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()["capture_id"]
        == baseline
    )
    assert broker.remote_calls == [BrowserRemoteAction.BACK]


def test_non_navigation_does_not_refresh_and_ui_is_eligibility_gated(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    assert "Browser remote" not in client.get(f"/instances/{INSTANCE_ID}").text
    make_ready(client, csrf)
    page = client.get(f"/instances/{INSTANCE_ID}")
    assert "Browser remote" in page.text
    assert 'data-remote-action="play_pause"' in page.text
    assert "noVNC" not in page.text
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/remote-actions",
        json={"action": "volume_up"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["screenshot_refresh"]["result"] == "not_requested"
    assert broker.remote_calls == [BrowserRemoteAction.VOLUME_UP]


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


def test_logout_retires_remote_rate_limit_session_bucket(client: TestClient) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    context = cast(Any, client.app).state.kodi_lab
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/remote-actions",
        json={"action": "volume_down"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 200
    assert context.browser_remote_rate_limiter.bucket_count() == 1

    logged_out = client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert logged_out.status_code == 303
    assert context.browser_remote_rate_limiter.bucket_count() == 0


def test_capacity_rejection_occurs_before_remote_dispatch(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    limiter = BrowserRemoteRateLimiter(
        clock=lambda: 100.0, idle_ttl_seconds=900.0, max_buckets=1
    )
    limiter.require_allowed("other-session", UUID(int=401))
    context = cast(Any, client.app).state.kodi_lab
    object.__setattr__(context, "browser_remote_rate_limiter", limiter)

    rejected = client.post(
        f"/api/instances/{INSTANCE_ID}/remote-actions",
        json={"action": "volume_down"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "visual_rate_limited"
    assert broker.remote_calls == []


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.LIFECYCLE,
        ManagedInstanceMutationKind.RESET,
        ManagedInstanceMutationKind.SNAPSHOT_RESTORE,
        ManagedInstanceMutationKind.INSTANCE_DELETE,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
        ManagedInstanceMutationKind.PROMOTION_VERIFICATION,
    ],
)
def test_remote_conflicts_with_unsafe_per_instance_coordination(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    kind: ManagedInstanceMutationKind,
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    coordinator = ManagedInstanceMutationCoordinator(database)
    lease = coordinator.acquire(INSTANCE_ID, kind)
    try:
        response = client.post(
            f"/api/instances/{INSTANCE_ID}/remote-actions",
            json={"action": "select"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "remote_action_conflict"
        assert broker.remote_calls == []
    finally:
        coordinator.release(lease)


def test_web_rate_limit_rejects_before_dispatch(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    url = f"/api/instances/{INSTANCE_ID}/remote-actions"
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for _ in range(20):
        assert (
            client.post(
                url, json={"action": "volume_down"}, headers=headers
            ).status_code
            == 200
        )
    rejected = client.post(url, json={"action": "volume_down"}, headers=headers)
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "visual_rate_limited"
    assert broker.remote_calls == [BrowserRemoteAction.VOLUME_DOWN] * 20


def test_two_concurrent_remote_actions_conflict(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    make_ready(client, csrf)
    handle = acquire_browser_remote(database, INSTANCE_ID)
    try:
        with pytest.raises(ManagedInstanceMutationConflictError):
            ManagedInstanceMutationCoordinator(database).acquire(
                INSTANCE_ID, ManagedInstanceMutationKind.LIFECYCLE
            )
        response = client.post(
            f"/api/instances/{INSTANCE_ID}/remote-actions",
            json={"action": "select"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "remote_action_conflict"
        assert broker.remote_calls == []
    finally:
        release_browser_remote(handle)


@pytest.mark.parametrize("undispatched", [True, False])
def test_only_proven_undispatched_failure_releases_input_fence(
    client: TestClient, broker: FakeBroker, undispatched: bool
) -> None:
    from kodi_lab.kodi.remote import (
        RemoteActionUndispatchedError,
        RemoteKodiRejectedError,
    )

    csrf = login(client)
    make_ready(client, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    broker.remote_error = (
        RemoteActionUndispatchedError("Connection failed before request")
        if undispatched
        else RemoteKodiRejectedError("Invalid response after possible delivery")
    )
    url = f"/api/instances/{INSTANCE_ID}/remote-actions"
    failed = client.post(url, json={"action": "left"}, headers=headers)
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "remote_kodi_rejected"
    broker.remote_error = None
    recovered = client.post(url, json={"action": "right"}, headers=headers)
    assert recovered.status_code == (200 if undispatched else 409)

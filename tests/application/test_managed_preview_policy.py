"""Managed preview authorization is explicit, one-use and revocable in-process."""

from uuid import uuid4

import pytest

from kodi_lab.application.live_display import LiveDisplayError, ManagedPreviewPolicy
from kodi_lab.web.live_preview import decoded_ack


def test_default_disabled_native_probe_barrier_and_one_connection() -> None:
    policy = ManagedPreviewPolicy()
    instance = uuid4()
    authority = ("revision", "container", 1)
    with pytest.raises(LiveDisplayError):
        policy.require(instance, "owner", authority, 1)
    grant = policy.prepare(instance, "owner", authority, 1, 1)
    with pytest.raises(LiveDisplayError):
        policy.require(instance, "owner", authority, 2)
    policy.commit(grant, 2)
    assert policy.require(instance, "owner", authority, 2) is grant
    for owner, anchor in [
        ("other", authority),
        ("owner", ("revision", "new-container", 2)),
    ]:
        with pytest.raises(LiveDisplayError):
            policy.require(instance, owner, anchor, 2)
    policy.connect(grant, 2)
    with pytest.raises(LiveDisplayError):
        policy.connect(grant, 2)
    with pytest.raises(LiveDisplayError):
        policy.require(instance, "owner", authority, 903)


def test_revocation_during_probe_cannot_be_reenabled_by_late_completion() -> None:
    policy = ManagedPreviewPolicy()
    instance = uuid4()
    authority = ("revision", 1)
    pending = policy.prepare(instance, "owner", authority, 1, 1)
    policy.revoke(instance)
    new = policy.prepare(instance, "other", authority, 1, 2)
    policy.commit(new, 3)
    with pytest.raises(LiveDisplayError):
        policy.commit(pending, 3)
    policy.revoke(instance, policy_id=pending.id)
    assert policy.require(instance, "other", authority, 3) is new
    assert policy.retire_owner("owner") == ()
    assert policy.retire_owner("other") == ((instance, new.id),)
    with pytest.raises(LiveDisplayError):
        policy.require(instance, "other", authority, 3)


@pytest.mark.parametrize(
    "payload",
    [
        '{"v":1,"ack":1,"decode_ms":0,"command":"left"}',
        '{"v":1,"ack":2,"decode_ms":0}',
        '{"v":1,"ack":true,"decode_ms":0}',
        '{"v":1,"ack":1,"decode_ms":0,"ack":1}',
        '{"v":true,"ack":1,"decode_ms":0}',
        '{"v":1,"ack":1,"decode_ms":NaN}',
        '{"v":1,"ack":1,"decode_ms":3001}',
        "x" * 257,
    ],
)
def test_ack_is_closed_bounded_and_cannot_be_a_remote_command(payload: str) -> None:
    with pytest.raises(LiveDisplayError):
        decoded_ack({"type": "websocket.receive", "text": payload}, 1)


def test_ack_accepts_only_matching_sequence_and_bounded_timing() -> None:
    assert (
        decoded_ack(
            {"type": "websocket.receive", "text": '{"v":1,"ack":1,"decode_ms":12.5}'}, 1
        )
        == 12.5
    )


def test_stop_before_prepare_and_old_stop_cannot_revoke_new_attempt() -> None:
    policy = ManagedPreviewPolicy()
    instance, old, fresh = uuid4(), uuid4(), uuid4()
    anchor = ("revision", "container", 1)
    assert not policy.cancel_attempt(instance, "owner", old)
    with pytest.raises(LiveDisplayError):
        policy.prepare(instance, "owner", anchor, 1, 1, attempt_id=old)
    new = policy.prepare(instance, "owner", anchor, 1, 2, attempt_id=fresh)
    policy.commit(new, 3)
    assert not policy.cancel_attempt(instance, "owner", old)
    assert not policy.cancel_attempt(instance, "another-owner", fresh)
    assert policy.require(instance, "owner", anchor, 3) is new
    assert policy.cancel_attempt(instance, "owner", fresh)
    with pytest.raises(LiveDisplayError):
        policy.prepare(instance, "owner", anchor, 1, 4, attempt_id=fresh)


def test_attempt_saturation_preserves_existing_exact_stop() -> None:
    policy = ManagedPreviewPolicy()
    instance = uuid4()
    grant = policy.prepare(instance, "owner", ("anchor",), 1, 1)
    policy.commit(grant, 2)
    for _ in range(4095):
        policy.cancel_attempt(uuid4(), "owner", uuid4())
    with pytest.raises(LiveDisplayError):
        policy.prepare(uuid4(), "owner", ("anchor",), 1, 3)
    assert policy.require(instance, "owner", ("anchor",), 3) is grant
    assert policy.cancel_attempt(instance, "owner", grant.id)
    with pytest.raises(LiveDisplayError):
        policy.require(instance, "owner", ("anchor",), 3)

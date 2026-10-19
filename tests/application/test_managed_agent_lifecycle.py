"""Owned lifecycle requests never replay starts/stops or refresh holds on polling."""

from datetime import timedelta
from pathlib import Path
from typing import Any, Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from kodi_lab.application.managed_agent_lifecycle import (
    ManagedAgentLifecycleError,
    ManagedAgentLifecycleResult,
    ManagedAgentLifecycleService,
)
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import ManagedInstanceUseRow, McpClientRow
from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository
from tests.application import test_managed_resources as resources

env = resources.env
factory = resources.factory
INSTANCE_ID = resources.INSTANCE_ID


class Actor:
    def __init__(
        self, environment: resources.Environment, root: Path, *, name: str
    ) -> None:
        self.env = environment
        admin = McpAdministrationService(environment.sessions, clock=environment.clock)
        client = admin.create_client(
            display_name=name, preset_id=ADDON_DEVELOPMENT_PRESET_ID
        )
        self.id = client.id
        self.agent = ManagedAgentLifecycleService(
            environment.sessions,
            environment.resources,
            environment.lifecycle,
            environment.status,
            lock_root=root / "lifecycle-locks",
            clock=environment.clock,
            rate_limiter=BoundedIntervalRateLimiter(0),
        )

    def authorize(self, session: Session) -> None:
        client = session.get(McpClientRow, str(self.id))
        if client is None or not client.enabled:
            raise McpAuthorizationError("permission_denied")

    def ensure(
        self, request_id: UUID, *, minutes: int = 15
    ) -> ManagedAgentLifecycleResult:
        return self.agent.ensure_ready(
            client_id=self.id,
            request_id=request_id,
            target_id=INSTANCE_ID,
            hold_minutes=minutes,
            authorize=self.authorize,
        )

    def release(self, request_id: UUID) -> ManagedAgentLifecycleResult:
        return self.agent.release(
            client_id=self.id,
            request_id=request_id,
            target_id=INSTANCE_ID,
            authorize=self.authorize,
        )


def test_running_ensure_and_replay_preserve_runtime_and_original_hold(
    env: resources.Environment, tmp_path: Path
) -> None:
    actor, request_id = Actor(env, tmp_path, name="Use fixture"), uuid4()
    with env.sessions() as session:
        original = ManagedRuntimeProjectionRepository(session).observation_token(
            INSTANCE_ID
        )
    first = actor.ensure(request_id)
    assert first.hold_until == env.clock() + timedelta(minutes=15)
    env.clock.value += timedelta(minutes=5)
    replay = actor.ensure(request_id)
    assert replay.replayed and replay.operation_id == first.operation_id
    assert replay.hold_until == first.hold_until
    assert env.broker.starts == 0
    with env.sessions() as session:
        assert (
            ManagedRuntimeProjectionRepository(session).observation_token(INSTANCE_ID)
            == original
        )


def test_stopped_ensure_starts_exactly_once_and_replay_is_observation(
    env: resources.Environment, tmp_path: Path
) -> None:
    env.broker.running = False
    actor, request_id = Actor(env, tmp_path, name="Resume fixture"), uuid4()
    first = actor.ensure(request_id)
    assert first.lifecycle_operation_id == first.operation_id
    assert env.broker.starts == 1 and env.broker.running
    replay = actor.ensure(request_id)
    assert replay.replayed and replay.operation_id == first.operation_id
    assert env.broker.starts == 1
    assert replay.receipt_state in {"not_ready", "ready"}


def test_lost_start_response_is_not_dispatched_again(
    env: resources.Environment, tmp_path: Path
) -> None:
    env.broker.running, env.broker.start_error = False, True
    actor, request_id = Actor(env, tmp_path, name="Uncertain fixture"), uuid4()
    first = actor.ensure(request_id)
    assert first.receipt_state == "ambiguous" and not first.ready
    assert env.broker.starts == 1
    for _ in range(3):
        assert actor.ensure(request_id).receipt_state == "ambiguous"
    assert env.broker.starts == 1


def test_release_clears_only_own_hold_and_protects_other_client(
    env: resources.Environment, tmp_path: Path
) -> None:
    first, second = (
        Actor(env, tmp_path, name="First fixture"),
        Actor(env, tmp_path, name="Second fixture"),
    )
    first.ensure(uuid4())
    second_use = second.ensure(uuid4())
    assert second_use.hold_until is not None
    released = first.release(uuid4())
    assert released.receipt_state == "retained" and released.code == "keep_awake"
    assert env.broker.stops == 0
    with env.sessions() as session:
        use = session.get(ManagedInstanceUseRow, (str(INSTANCE_ID), f"mcp:{second.id}"))
        assert use is not None and use.hold_until == second_use.hold_until.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    request_id = uuid4()
    assert second.release(request_id).receipt_state == "released"
    assert env.broker.stops == 1
    assert second.release(request_id).replayed
    assert env.broker.stops == 1


def test_original_ensure_replay_does_not_restart_after_idle_stop(
    env: resources.Environment, tmp_path: Path
) -> None:
    env.broker.running = False
    env.resources.enroll(INSTANCE_ID, enabled=True)
    actor, request_id = Actor(env, tmp_path, name="Idle fixture"), uuid4()
    actor.ensure(request_id)
    env.clock.value += timedelta(minutes=60)
    assert env.resources.sweep_idle() == 1
    assert not env.broker.running
    replay = actor.ensure(request_id)
    assert not replay.ready and replay.current_state == "stopped"
    assert replay.code == "runtime_changed_since_request"
    assert env.broker.starts == 1
    actor.ensure(uuid4())
    assert env.broker.starts == 2 and env.broker.running


def test_request_identity_cannot_change_action_or_hold(
    env: resources.Environment, tmp_path: Path
) -> None:
    actor, request_id = Actor(env, tmp_path, name="Identity fixture"), uuid4()
    actor.ensure(request_id)
    with pytest.raises(ManagedAgentLifecycleError, match="request_conflict"):
        actor.release(request_id)
    with pytest.raises(ManagedAgentLifecycleError, match="request_conflict"):
        actor.ensure(request_id, minutes=30)
    assert env.broker.stops == 0 and env.broker.starts == 0


def test_pending_preclaim_request_can_resume_once(
    env: resources.Environment, tmp_path: Path
) -> None:
    env.broker.running = False
    actor, request_id = Actor(env, tmp_path, name="Pending fixture"), uuid4()
    actor.agent._claim(
        actor.id, request_id, INSTANCE_ID, "ensure_ready", 15, actor.authorize
    )
    assert actor.ensure(request_id).replayed
    assert env.broker.starts == 1


def test_crash_after_atomic_claim_never_replays_start(
    env: resources.Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProcessLoss(BaseException):
        pass

    env.broker.running = False
    actor, request_id = Actor(env, tmp_path, name="Crash fixture"), uuid4()
    original = env.lifecycle._operations.begin

    def crash(*args: Any, **kwargs: Any) -> Never:
        original(*args, **kwargs)
        raise ProcessLoss()

    with monkeypatch.context() as patch:
        patch.setattr(env.lifecycle._operations, "begin", crash)
        with pytest.raises(ProcessLoss):
            actor.ensure(request_id)
    replay = actor.ensure(request_id)
    assert replay.receipt_state == "dispatched" and not replay.ready
    assert env.broker.starts == 0
    env.clock.value += timedelta(minutes=16)
    assert actor.ensure(request_id).receipt_state == "ambiguous"
    assert env.broker.starts == 0


def test_disabled_client_cannot_read_or_resume_owned_request(
    env: resources.Environment, tmp_path: Path
) -> None:
    actor, request_id = Actor(env, tmp_path, name="Revoked fixture"), uuid4()
    actor.ensure(request_id)
    McpAdministrationService(env.sessions, clock=env.clock).set_client_enabled(
        actor.id, False
    )
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        actor.ensure(request_id)
    assert env.broker.starts == 0 and env.broker.stops == 0

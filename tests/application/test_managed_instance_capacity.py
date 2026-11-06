"""Installed profiles are independent of the administrator's running limit."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.domain.create_kodi import CreateAcceptance, CreateCheckpoint
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.managed_activity import resource_policy
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedStartAuthorizationRow,
)
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.application.test_managed_start_selection import setup
from tests.create_kodi_runner_support import Environment
from tests.create_kodi_support import accept


def test_install_all_four_with_one_running_and_finish_stopped(
    factory: sessionmaker[Session],
) -> None:
    env = Environment(factory, start_after_setup=False)
    env.broker.unique_containers = True
    app = CreateKodiService(env.repo)
    try:
        for generation in ("omega", "matrix", "nexus", "piers"):
            if generation != "omega":
                env.op = accept(
                    app,
                    generation=generation,
                    start_after_setup=False,
                    preview_approved=generation == "piers",
                )
                env.broker.parent = env.op.id
            for _ in range(20):
                if env.op.checkpoint is CreateCheckpoint.COMPLETE:
                    break
                env.step()
            assert env.op.checkpoint is CreateCheckpoint.COMPLETE, env.op
            assert env.op.stage == "provisioning"
            assert env.op.start_operation_id is None
            assert env.broker.starts == 0
            if generation == "omega":
                # Model an independently started exact instance filling the one slot.
                env.lifecycle.start_managed(env.op.target_id)
                env.broker.starts = 0
        assert len(env.broker.records) == 4
        assert sum(r.runtime_state == "running" for r in env.broker.records) == 1
        assert resource_policy_value(factory) == 1
        for target in [UUID(r.managed_instance_id) for r in env.broker.records]:
            for running in list(env.broker.records):
                if running.runtime_state == "running":
                    env.lifecycle.stop(UUID(running.managed_instance_id))
            env.lifecycle.start_managed(target)
            assert sum(r.runtime_state == "running" for r in env.broker.records) == 1
        assert len(env.broker.records) == 4
    finally:
        env.close()


def resource_policy_value(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return resource_policy(session).max_running


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 4])
def test_any_profile_can_run_up_to_admin_limit_without_changing_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, retained: bool
) -> None:
    env = setup(tmp_path, monkeypatch)
    s = env.s
    s.choose("omega")
    with s.env.sessions.begin() as session:
        resource_policy(session).max_running = limit
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy
        policy.retain_profiles = retained
        if not retained:
            policy.retained_omega_target_id = None
    with s.env.sessions() as session:
        selection = session.execute(select(DevelopmentSessionRow.__table__)).all()
    profiles = {key: path.read_bytes() for key, path in s.profiles.items()}
    resources = ManagedResourceService(s.env.sessions, env.broker, env.lifecycle)
    try:
        targets = list(s.ids.values())
        for target in targets[:limit]:
            env.lifecycle.start_managed(target)
        assert resources.capacity().limit == limit
        assert resources.capacity().used == limit
        if limit < 4:
            with pytest.raises((ManagedCapacityError, DevelopmentSessionConflict)):
                env.lifecycle.start_managed(targets[limit])
        with s.env.sessions() as session:
            assert (
                session.execute(select(DevelopmentSessionRow.__table__)).all()
                == selection
            )
        assert {key: path.read_bytes() for key, path in s.profiles.items()} == profiles
        # Lowering a limit preserves already running instances and blocks new work.
        with s.env.sessions.begin() as session:
            resource_policy(session).max_running = 1
        assert resources.capacity().used == limit
        assert not resources.capacity().available
    finally:
        env.readiness.close()


def test_concurrent_retained_starts_reserve_two_slots_and_refuse_third(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = setup(tmp_path, monkeypatch)
    try:
        with env.s.env.sessions.begin() as session:
            resource_policy(session).max_running = 2
        targets = list(env.s.ids.values())
        with ThreadPoolExecutor(max_workers=2) as workers:
            starts = [
                workers.submit(env.lifecycle.start_managed, t) for t in targets[:2]
            ]
            for start in starts:
                start.result(timeout=10)
        with pytest.raises(ManagedCapacityError, match="capacity_full"):
            env.lifecycle.start_managed(targets[2])
        assert env.broker.starts == 2
        with env.s.env.sessions() as session:
            authorization = session.scalar(
                select(ManagedStartAuthorizationRow).where(
                    ManagedStartAuthorizationRow.target_id == str(targets[2])
                )
            )
            assert authorization is None or authorization.consumed_at is None

    finally:
        env.readiness.close()


def test_setup_replay_cannot_change_start_or_preview_approval(
    factory: sessionmaker[Session],
) -> None:
    from kodi_lab.domain.create_kodi import CreateConflict
    from kodi_lab.runtime.recipes import load_installation_recipe

    app = CreateKodiService(CreateKodiRepository(factory))
    request = uuid4()

    def submit(
        *, start_after_setup: bool = False, preview_approved: bool = True
    ) -> CreateAcceptance:
        return app.accept(
            request_id=request,
            generation="piers",
            recipe_digest=load_installation_recipe("piers").digest,
            owner_approved=True,
            start_after_setup=start_after_setup,
            preview_approved=preview_approved,
        )

    original = submit().operation
    assert submit().operation == original
    with pytest.raises(CreateConflict):
        submit(start_after_setup=True)
    with pytest.raises(ValueError, match="prerelease approval"):
        submit(preview_approved=False)


def test_piers_explicit_start_finishes_with_its_own_native_identity(
    factory: sessionmaker[Session],
) -> None:
    env = Environment(factory, generation="piers", preview_approved=True)
    env.transport.native_version = {
        "major": 22,
        "minor": 0,
        "tag": "releasecandidate",
        "tagversion": "1",
    }
    try:
        for _ in range(24):
            if env.op.checkpoint is CreateCheckpoint.COMPLETE:
                break
            env.step()
        assert env.op.checkpoint is CreateCheckpoint.COMPLETE, env.op
        assert env.op.stage == "readiness" and env.op.start_after_setup
        assert env.broker.starts == 1
        assert "Application.GetProperties" in env.transport.methods
    finally:
        env.close()

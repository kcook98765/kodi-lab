"""Bounded fixed-stage progress and exact evidence recovery, without native Kodi."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_registration as registration_module
from kodi_lab.domain import InstanceRuntimeState
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as LifecycleState
from kodi_lab.domain.create_kodi import CreateCheckpoint as CP
from kodi_lab.domain.create_kodi import (
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
    RuntimeRecipeInstallationRow,
)
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import finish
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Interrupted
from tests.create_kodi_runner_support import Environment
from tests.runtime.test_recipe_installation import _ready


@pytest.fixture
def env(factory: sessionmaker[Session]) -> Iterator[Environment]:
    value = Environment(factory)
    try:
        yield value
    finally:
        value.close()


def reach(env: Environment, stage: CreateStage, checkpoint: CP) -> None:
    for _ in range(20):
        if (env.op.stage, env.op.checkpoint) == (stage, checkpoint):
            return
        assert env.op.checkpoint not in {CP.COMPLETE, CP.NEEDS_ATTENTION}, env.op
        env.step()
    pytest.fail("fixed Create progression did not reach requested checkpoint")


@pytest.mark.parametrize("cached", [False, True])
def test_full_sequence_uses_one_adapter_or_transition_per_invocation(
    factory: sessionmaker[Session], cached: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = Environment(factory, cached=cached)
    adapters = []
    for name in (
        "_installation",
        "_registration",
        "_provisioning",
        "_start",
        "_readiness",
    ):
        spy = Mock(wraps=getattr(env.runner, name))
        setattr(env.runner, name, spy)
        adapters.append(spy)
    plans = Mock(wraps=env.repo.plan)
    advances = Mock(wraps=env.repo.advance)
    monkeypatch.setattr(env.repo, "plan", plans)
    monkeypatch.setattr(env.repo, "advance", advances)
    planned: dict[str, UUID] = {}
    try:
        for _ in range(20):
            before = env.op
            calls = (
                sum(len(spy.mock_calls) for spy in adapters)
                + plans.call_count
                + advances.call_count
            )
            env.step()
            after = (
                sum(len(spy.mock_calls) for spy in adapters)
                + plans.call_count
                + advances.call_count
            )
            assert after - calls <= 1
            for field in ("runtime_revision_id", "image_id", "container_id"):
                if getattr(before, field) is not None:
                    assert getattr(env.op, field) == getattr(before, field)
            for field in (
                "installation_operation_id",
                "provision_operation_id",
                "start_operation_id",
            ):
                child = getattr(env.op, field)
                if child:
                    assert planned.setdefault(field, child) == child
            if before.checkpoint is CP.ACCEPTED:
                assert env.op.checkpoint is CP.PLANNED
            if env.op.checkpoint is CP.COMPLETE:
                break
        assert env.op.checkpoint is CP.COMPLETE
        assert len(env.broker.launches) == (0 if cached else 1)
        assert env.broker.provisions == env.broker.starts == 1
        assert env.broker.stops == 0
        assert env.transport.methods == ["JSONRPC.Ping", "Application.GetProperties"]
        if cached:
            with factory() as session:
                assert (
                    session.get(
                        RuntimeRecipeInstallationRow,
                        str(planned["installation_operation_id"]),
                    )
                    is None
                )
        with factory() as session:
            assert session.scalars(select(AgentWorkspaceRow)).all() == []
            assert session.scalars(select(DevelopmentSessionRow)).all() == []
            proof = session.get(
                ManagedStartAuthorizationRow, str(env.op.start_operation_id)
            )
            assert proof and proof.consumed_at
    finally:
        env.close()


def test_accepted_and_planned_installation_survive_reopened_connections(
    env: Environment,
) -> None:
    before = env.op
    env.step()
    planned_op = env.op
    assert planned_op.checkpoint is CP.PLANNED and not env.broker.launches
    planned = env.op.installation_operation_id
    with env.sessions() as session:
        url = str(session.get_bind().engine.url)
    engine = create_database_engine(url)
    try:
        reopened = CreateKodiRepository(create_session_factory(engine))
        assert reopened.get(before.id) == env.op
        assert reopened.for_request(before.request_id) == env.op
    finally:
        engine.dispose()
    env.step()
    assert env.op.installation_operation_id == planned and len(env.broker.launches) == 1
    assert (
        env.op.stage is CreateStage.INSTALLATION and env.op.checkpoint is CP.CONFIRMED
    )


def test_active_original_installation_is_waited_for_then_exactly_confirmed(
    env: Environment,
) -> None:
    env.broker.mode = "pending"
    env.step()
    env.step()
    active = env.op
    assert active.checkpoint is CP.DISPATCHED
    env.step()
    assert env.op == active and len(env.broker.launches) == 1
    assert active.installation_operation_id
    finish(env.sessions, active.installation_operation_id)
    env.broker.workers[str(active.installation_operation_id)] = "exited"
    env.broker.operation_id = env.broker.generation = None
    env.step()
    assert (
        env.op.checkpoint is CP.CONFIRMED
        and env.op.installation_operation_id == active.installation_operation_id
    )
    assert len(env.broker.launches) == 1


def test_lost_install_response_can_only_resolve_original_terminal_evidence(
    env: Environment,
) -> None:
    env.broker.mode = "lost_response"
    env.step()
    env.step()
    original = env.op
    assert (
        original.checkpoint is CP.NEEDS_ATTENTION
        and original.outcome is CreateOutcome.UNCERTAIN
    )
    env.step()
    assert env.op == original  # Original active worker, no new launch.
    assert original.installation_operation_id
    finish(env.sessions, original.installation_operation_id)
    env.broker.workers[str(original.installation_operation_id)] = "exited"
    env.broker.operation_id = env.broker.generation = None
    env.step()
    assert (
        env.op.stage is CreateStage.INSTALLATION and env.op.checkpoint is CP.CONFIRMED
    )
    assert env.op.installation_operation_id == original.installation_operation_id
    assert len(env.broker.launches) == 1 and env.broker.provisions == 0


@pytest.mark.parametrize("mode", ["no_response", "failed"])
def test_missing_install_receipt_or_verified_failure_never_relaunches(
    env: Environment, mode: str
) -> None:
    env.broker.mode = mode
    env.step()
    env.step()
    original = env.op
    assert original.checkpoint is CP.NEEDS_ATTENTION
    for _ in range(3):
        env.step()
        assert env.op == original
    assert (
        len(env.broker.launches) == 1
        and env.broker.provisions == env.broker.starts == 0
    )


def test_registration_completed_before_confirmation_uses_readonly_existing_path(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    reach(env, CreateStage.REGISTRATION, CP.PLANNED)
    original = bootstrap_recipe_instance

    def crash(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(registration_module, "bootstrap_recipe_instance", crash)
        with pytest.raises(Interrupted):
            env.step()
    found = env.repo.get(env.op.id)
    assert found and found.checkpoint is CP.NEEDS_ATTENTION
    env.op = found
    monkeypatch.setattr(
        registration_module,
        "bootstrap_recipe_instance",
        lambda *_a, **_k: pytest.fail("must confirm without mutation"),
    )
    env.step()
    assert (
        env.op.stage is CreateStage.REGISTRATION and env.op.checkpoint is CP.CONFIRMED
    )
    assert env.broker.provisions == env.broker.starts == 0


def test_unrelated_ready_cache_cannot_resolve_missing_original_installation(
    env: Environment,
) -> None:
    env.broker.mode = "no_response"
    env.step()
    env.step()
    uncertain = env.op
    assert uncertain.checkpoint is CP.NEEDS_ATTENTION
    _ready(env.sessions, generation="omega")
    env.step()
    assert env.op == uncertain and len(env.broker.launches) == 1


def test_predispatch_refusal_is_not_implicitly_resumed(env: Environment) -> None:
    env.step()
    env.op = env.repo.refuse(
        env.op.id, version=env.op.version, reason="installation_admission_refused"
    )
    refused = env.op
    env.step()
    assert env.op == refused and not env.broker.launches


@pytest.mark.parametrize("stage", [CreateStage.PROVISIONING, CreateStage.STARTUP])
def test_confirmed_lifecycle_advances_without_repeating_child(
    env: Environment, stage: CreateStage
) -> None:
    reach(env, stage, CP.CONFIRMED)
    before = env.op
    calls = (env.broker.provisions, env.broker.starts)
    env.step()
    assert env.op.checkpoint is CP.ACCEPTED and env.op.stage != stage
    assert (env.broker.provisions, env.broker.starts) == calls
    assert (env.op.provision_operation_id, env.op.start_operation_id) == (
        before.provision_operation_id,
        before.start_operation_id,
    )


def paused_dispatch(
    env: Environment, stage: CreateStage, monkeypatch: pytest.MonkeyPatch
) -> None:
    reach(env, stage, CP.PLANNED)
    if stage is CreateStage.PROVISIONING:
        env.broker.fault = "before_dispatch"
    else:
        env.broker.start_fault = "before_dispatch"

    def crash(*_args: Any, **_kwargs: Any) -> Any:
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(env.repo, "note_uncertain", crash)
        with pytest.raises(Interrupted):
            env.step()
    found = env.repo.get(env.op.id)
    assert found and found.checkpoint is CP.DISPATCHED
    env.op = found
    env.broker.fault = env.broker.start_fault = ""


@pytest.mark.parametrize("stage", [CreateStage.PROVISIONING, CreateStage.STARTUP])
def test_live_exact_child_waits_but_expiration_fences_without_redispatch(
    env: Environment, monkeypatch: pytest.MonkeyPatch, stage: CreateStage
) -> None:
    paused_dispatch(env, stage, monkeypatch)
    active = env.op
    counts = (env.broker.provisions, env.broker.starts)
    env.step()
    assert env.op == active
    with env.sessions.begin() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(active.target_id))
        assert lease
        lease.lease_expires_at = timestamp(datetime.now(UTC) - timedelta(seconds=1))
    env.step()
    fenced = env.op
    assert (
        fenced.checkpoint is CP.NEEDS_ATTENTION
        and fenced.outcome is CreateOutcome.UNCERTAIN
    )
    env.step()
    assert env.op == fenced and (env.broker.provisions, env.broker.starts) == counts


@pytest.mark.parametrize("stage", [CreateStage.PROVISIONING, CreateStage.STARTUP])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_receipt",
        "wrong_lease",
        "terminal_receipt",
        "missing_lease",
        "wrong_target",
    ],
)
def test_uncertain_or_mismatched_lifecycle_evidence_is_not_success_or_retry(
    env: Environment, monkeypatch: pytest.MonkeyPatch, stage: CreateStage, fault: str
) -> None:
    paused_dispatch(env, stage, monkeypatch)
    original = env.op
    with env.sessions.begin() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(original.child_id)
        )
        lease = session.get(ManagedInstanceMutationLeaseRow, str(original.target_id))
        assert child and lease
        if fault == "missing_receipt":
            auth = session.get(ManagedStartAuthorizationRow, str(original.child_id))
            if auth:
                session.delete(auth)
                session.flush()
            session.delete(child)
        elif fault == "wrong_lease":
            lease.operation_id = str(uuid4())
        elif fault == "missing_lease":
            session.delete(lease)
        elif fault == "terminal_receipt":
            # A terminal receipt without joint captured parent confirmation is
            # inconsistent, not authority to synthesize success from inventory.
            child.state, child.completed_at, child.resulting_state = (
                LifecycleState.SUCCEEDED,
                child.started_at,
                "stopped",
            )
        else:
            child.original_managed_instance_id = str(uuid4())
    before = (env.broker.provisions, env.broker.starts)
    env.step()
    assert env.op.checkpoint is CP.NEEDS_ATTENTION
    assert env.op.child_id == original.child_id
    env.step()
    assert (env.broker.provisions, env.broker.starts) == before


@pytest.mark.parametrize("fault", ["missing", "lease", "image", "projection"])
def test_dispatched_start_requires_original_matching_ordinary_authorization(
    env: Environment, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    paused_dispatch(env, CreateStage.STARTUP, monkeypatch)
    original = env.op
    with env.sessions.begin() as session:
        auth = session.get(ManagedStartAuthorizationRow, str(original.child_id))
        assert auth
        if fault == "missing":
            session.delete(auth)
        elif fault == "lease":
            auth.lease_digest = "0" * 64
        elif fault == "image":
            auth.image_id = "sha256:" + "0" * 64
        else:
            auth.runtime_state_revision += 1
    env.step()
    fenced = env.op
    assert fenced.checkpoint is CP.NEEDS_ATTENTION
    assert fenced.child_id == original.child_id
    env.step()
    assert env.op == fenced and env.broker.starts == 0


@pytest.mark.parametrize("stage", [CreateStage.PROVISIONING, CreateStage.STARTUP])
def test_lost_mutation_reply_remains_attention_even_when_container_exists(
    env: Environment, stage: CreateStage
) -> None:
    reach(env, stage, CP.PLANNED)
    if stage is CreateStage.PROVISIONING:
        env.broker.fault = "lost_response"
    else:
        env.broker.start_fault = "lost_response"
    env.step()
    uncertain = env.op
    assert uncertain.checkpoint is CP.NEEDS_ATTENTION
    before = (env.broker.provisions, env.broker.starts)
    env.broker.fault = env.broker.start_fault = ""
    env.step()
    assert env.op == uncertain and (env.broker.provisions, env.broker.starts) == before


def test_native_temporary_failure_rechecks_readonly_same_identity(
    env: Environment,
) -> None:
    reach(env, CreateStage.READINESS, CP.PLANNED)
    env.transport.mode = "invalid"
    env.step()
    pending = env.op
    assert pending.checkpoint is CP.DISPATCHED
    env.transport.mode = "ready"
    env.step()
    assert env.op.checkpoint is CP.COMPLETE and env.op.child_id == pending.child_id
    assert (
        env.broker.provisions == env.broker.starts == 1
        and len(env.broker.launches) == 1
    )


@pytest.mark.parametrize(
    "stage", [CreateStage.PROVISIONING, CreateStage.STARTUP, CreateStage.READINESS]
)
def test_reconstructed_services_continue_durable_boundary_without_replaying_mutations(
    env: Environment, stage: CreateStage
) -> None:
    checkpoint = CP.PLANNED if stage is CreateStage.READINESS else CP.CONFIRMED
    reach(env, stage, checkpoint)
    if stage is CreateStage.READINESS:
        env.transport.mode = "invalid"
        env.step()
        assert env.op.checkpoint is CP.DISPATCHED
    before = env.op
    env.close()  # Drop the original process-local readiness registry.
    with env.sessions() as session:
        url = str(session.get_bind().engine.url)
    engine = create_database_engine(url)
    resumed = Environment(create_session_factory(engine), operation_id=before.id)
    try:
        # The fake native runtime survives application loss independently of DB.
        resumed.broker.records = list(env.broker.records)
        resumed.step()
        assert resumed.op.checkpoint is (
            CP.COMPLETE if stage is CreateStage.READINESS else CP.ACCEPTED
        )
        assert (
            resumed.op.installation_operation_id,
            resumed.op.provision_operation_id,
            resumed.op.start_operation_id,
        ) == (
            before.installation_operation_id,
            before.provision_operation_id,
            before.start_operation_id,
        )
        assert not resumed.broker.launches
        assert resumed.broker.provisions == resumed.broker.starts == 0
    finally:
        resumed.close()
        engine.dispose()


def test_completed_history_after_stop_and_status_reads_never_execute(
    env: Environment,
) -> None:
    reach(env, CreateStage.READINESS, CP.PLANNED)
    env.step()
    completed = env.op
    assert completed.checkpoint is CP.COMPLETE
    before = (
        len(env.broker.launches),
        env.broker.provisions,
        env.broker.starts,
        len(env.transport.methods),
    )
    env.broker.records[0] = replace(
        env.broker.records[0], runtime_state=cast(Any, "stopped")
    )
    with env.sessions.begin() as session:
        instance = session.get(ManagedInstanceRow, str(completed.target_id))
        assert instance
        instance.runtime_state = InstanceRuntimeState.STOPPED
        instance.runtime_state_revision += 2
    for _ in range(3):
        assert env.repo.get(completed.id) == completed
        assert env.repo.for_target(completed.target_id) == completed
        env.step()
        assert env.op == completed
    assert before == (
        len(env.broker.launches),
        env.broker.provisions,
        env.broker.starts,
        len(env.transport.methods),
    )


def test_stale_or_missing_invocation_cannot_execute(env: Environment) -> None:
    with pytest.raises(CreateTransitionError, match="not_found"):
        env.runner.continue_once(uuid4(), version=0)
    env.step()
    with pytest.raises(CreateTransitionError, match="stale"):
        env.runner.continue_once(env.op.id, version=0)
    assert not env.broker.launches

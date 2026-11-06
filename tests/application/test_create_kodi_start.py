"""Exact startup receipts and failure fences without a real Kodi or readiness stage."""

from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_start as module
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.domain import ManagedInstanceLifecycleKind as Kind
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as State
from kodi_lab.domain import ManagedInstanceSemanticState
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedResourcePolicyRow,
    ManagedStartAuthorizationRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Interrupted
from tests.create_kodi_start_support import Environment, select_stopped
from tests.runtime.test_recipe_installation import _ready


@pytest.fixture
def env(factory: sessionmaker[Session]) -> Iterator[Environment]:
    value = Environment(factory)
    try:
        yield value
    finally:
        value.close()


def test_exact_start_uses_original_child_authority_and_stops_before_readiness_stage(
    env: Environment,
) -> None:
    before = env.op
    env.advance()
    assert env.op.stage is CreateStage.STARTUP and env.broker.calls == 0
    env.op = env.repo.plan(env.op.id, version=env.op.version)
    original = env.op.start_operation_id
    result = env.start.start(env.op.id, version=env.op.version)
    assert (
        result.checkpoint is CreateCheckpoint.CONFIRMED
        and result.stage is CreateStage.STARTUP
    )
    assert (
        result.container_id == before.container_id
        and result.start_operation_id == original
    )
    assert result.runtime_state_revision == (before.runtime_state_revision or 0) + 2
    assert env.broker.starts == 1 and env.broker.calls == 1
    assert env.start.start(result.id, version=result.version) == result
    with env.sessions() as session:
        authority = session.get(ManagedStartAuthorizationRow, str(original))
        child = session.get(ManagedInstanceLifecycleOperationRow, str(original))
        assert (
            authority
            and authority.consumed_at
            and child
            and child.state is State.SUCCEEDED
        )
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(result.target_id)) is None
        )
        assert session.scalars(select(AgentWorkspaceRow)).all() == []
        assert session.scalars(select(DevelopmentSessionRow)).all() == []
    assert env.broker.proof and env.broker.proof.get_secret_value() not in str(
        asdict(result)
    )
    with pytest.raises(ManagedCapacityError):
        env.broker.admission.run_start(
            instance_id=str(result.target_id),
            container_id=str(result.container_id),
            revision_id=str(result.runtime_revision_id),
            ordinary_start_proof=env.broker.proof.get_secret_value(),
            dispatch=lambda: env.broker.dispatch(str(result.target_id)),
        )
    assert env.broker.starts == 1


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "container",
        "image",
        "revision",
        "pin",
        "projection",
        "running",
        "receipt",
    ],
)
def test_changed_expected_binding_never_issues_another_start(
    env: Environment, change: str
) -> None:
    env.advance()
    if change == "missing":
        env.broker.records.clear()
    elif change == "container":
        env.broker.records[0] = replace(env.broker.records[0], container_id="e" * 64)
    elif change == "image":
        env.broker.records[0] = replace(
            env.broker.records[0], image_id="sha256:" + "e" * 64
        )
    elif change == "revision":
        env.broker.records[0] = replace(
            env.broker.records[0], runtime_revision_id=str(uuid4())
        )
    elif change == "pin":
        other = _ready(env.sessions, generation="omega")
        with env.sessions.begin() as session:
            ready = session.get(RuntimeRecipeInstallationRow, str(other))
            instance = session.get(ManagedInstanceRow, str(env.op.target_id))
            assert ready and ready.runtime_revision_id and instance
            instance.runtime_revision_id = ready.runtime_revision_id
    elif change == "running":
        from kodi_lab.broker.docker_inventory import ContainerRuntimeState

        env.broker.records[0] = replace(
            env.broker.records[0], runtime_state=ContainerRuntimeState.RUNNING
        )
    else:
        with env.sessions.begin() as session:
            if change == "projection":
                instance = session.get(ManagedInstanceRow, str(env.op.target_id))
                assert instance
                instance.runtime_state_revision += 1
            else:
                child = session.get(
                    ManagedInstanceLifecycleOperationRow,
                    str(env.op.provision_operation_id),
                )
                assert child
                session.delete(child)
    result = env.start.start(env.op.id, version=env.op.version)
    assert result.outcome is CreateOutcome.REFUSED and result.dispatched_at is None
    assert env.broker.starts == env.broker.calls == 0
    with env.sessions() as session:
        assert (
            session.get(ManagedStartAuthorizationRow, str(result.start_operation_id))
            is None
        )


@pytest.mark.parametrize(
    "fault", ["before_dispatch", "lost_response", "wrong_container", "no_start"]
)
def test_uncertain_start_never_replays_or_regenerates_proof(
    env: Environment, fault: str
) -> None:
    env.advance()
    env.broker.fault = fault
    if fault == "before_dispatch":
        with pytest.raises(Interrupted):
            env.start.start(env.op.id, version=env.op.version)
        result = env.repo.get(env.op.id)
        assert result
    else:
        result = env.start.start(env.op.id, version=env.op.version)
    assert result.checkpoint is CreateCheckpoint.NEEDS_ATTENTION
    assert result.outcome is CreateOutcome.UNCERTAIN and result.start_operation_id
    proof = env.broker.proof
    assert env.start.start(result.id, version=result.version) == result
    assert env.broker.calls == 1 and env.broker.proof is proof
    with env.sessions() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(result.start_operation_id)
        )
        assert child and child.state is not State.SUCCEEDED


@pytest.mark.parametrize(
    "boundary",
    ["before_claim", "claim", "capture", "after_capture", "terminal", "after_terminal"],
)
def test_interrupted_claim_reply_and_terminalization_keep_original_context(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    env.advance()
    if boundary == "before_claim":
        monkeypatch.setattr(
            env.lifecycle,
            "start_managed",
            lambda *_a, **_kw: (_ for _ in ()).throw(Interrupted()),
        )
    elif boundary in {"claim", "capture", "terminal"}:
        name = {
            "claim": "claim_start",
            "capture": "capture_start",
            "terminal": "finish_start",
        }[boundary]
        original = getattr(module, name)

        def interrupted(*args: object, **kwargs: object) -> None:
            original(*args, **kwargs)
            raise Interrupted()

        monkeypatch.setattr(module, name, interrupted)
    elif boundary == "after_capture":
        monkeypatch.setattr(
            env.lifecycle._status,
            "get",
            lambda *_a: (_ for _ in ()).throw(Interrupted()),
        )
    else:
        start = env.lifecycle.start_managed

        def after(*args: object, **kwargs: object) -> None:
            start(*args, **kwargs)  # type: ignore[arg-type]
            raise Interrupted()

        monkeypatch.setattr(env.lifecycle, "start_managed", after)
    with pytest.raises(Interrupted):
        env.start.start(env.op.id, version=env.op.version)
    result = env.repo.get(env.op.id)
    assert result and result.start_operation_id
    with env.sessions() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(result.start_operation_id)
        )
        auth = session.get(ManagedStartAuthorizationRow, str(result.start_operation_id))
        lease = session.get(ManagedInstanceMutationLeaseRow, str(result.target_id))
        if boundary in {"before_claim", "claim"}:
            assert child is None and auth is None and lease is None
            assert result.dispatched_at is None and env.broker.calls == 0
        elif boundary == "after_terminal":
            assert (
                child and child.state is State.SUCCEEDED and auth and auth.consumed_at
            )
            assert result.checkpoint is CreateCheckpoint.CONFIRMED and lease is None
        else:
            assert child and child.state is State.RUNNING and lease and auth
            assert result.outcome is CreateOutcome.UNCERTAIN
    assert env.start.start(result.id, version=result.version) == result


@pytest.mark.parametrize("drift", ["expired", "lease", "projection", "authority"])
def test_broker_revalidates_atomic_authority_before_native_dispatch(
    env: Environment, drift: str
) -> None:
    env.advance()

    def changed() -> None:
        with env.sessions.begin() as session:
            op = env.repo.get(env.op.id)
            assert op and op.start_operation_id
            lease = session.get(ManagedInstanceMutationLeaseRow, str(op.target_id))
            assert lease
            if drift == "expired":
                lease.lease_expires_at = "2000-01-01T00:00:00.000000Z"
            elif drift == "lease":
                lease.lease_token_digest = "e" * 64
            elif drift == "projection":
                instance = session.get(ManagedInstanceRow, str(op.target_id))
                assert instance
                instance.runtime_state_revision += 1
            else:
                auth = session.get(
                    ManagedStartAuthorizationRow, str(op.start_operation_id)
                )
                assert auth
                session.delete(auth)

    env.broker.before_rpc = changed
    result = env.start.start(env.op.id, version=env.op.version)
    assert result.checkpoint is not CreateCheckpoint.CONFIRMED
    assert env.broker.starts == 0 and env.broker.calls == 1
    assert env.start.start(result.id, version=result.version) == result


def test_start_receipt_and_authorization_survive_pruning_and_later_stop(
    env: Environment,
) -> None:
    env.advance()
    result = env.start.start(env.op.id, version=env.op.version)
    assert result.checkpoint is CreateCheckpoint.CONFIRMED
    env.lifecycle.stop(result.target_id)
    with env.sessions.begin() as session:
        repo = ManagedInstanceLifecycleOperationRepository(session)
        original = repo.get(result.start_operation_id or uuid4())
        assert original
        for n in range(25):
            now = original.requested_at + timedelta(seconds=n + 1)
            repo.create(
                replace(
                    original,
                    id=uuid4(),
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )
        repo.prune_terminal(result.target_id)
        assert repo.get(original.id) == original
        assert session.get(ManagedStartAuthorizationRow, str(original.id)) is not None
    assert env.start.start(result.id, version=result.version) == result
    assert env.broker.starts == 1


@pytest.mark.parametrize("incomplete", [False, True])
def test_existing_selection_is_preserved_and_incomplete_selection_denied(
    env: Environment,
    incomplete: bool,
) -> None:
    select_stopped(env, incomplete=incomplete)
    with env.sessions() as session:
        before = session.execute(select(DevelopmentSessionRow.__table__)).all()
    env.advance()
    result = env.start.start(env.op.id, version=env.op.version)
    assert env.broker.starts == (0 if incomplete else 1)
    assert result.checkpoint is (
        CreateCheckpoint.NEEDS_ATTENTION if incomplete else CreateCheckpoint.CONFIRMED
    )
    with env.sessions() as session:
        assert session.execute(select(DevelopmentSessionRow.__table__)).all() == before


@pytest.mark.parametrize(
    "state",
    [State.RUNNING, State.SUCCEEDED, State.FAILED, State.RECONCILIATION_REQUIRED],
)
def test_existing_child_evidence_is_not_replayed(
    env: Environment, state: State
) -> None:
    env.advance()
    planned = env.repo.plan(env.op.id, version=env.op.version)
    with env.sessions.begin() as session:
        repo = ManagedInstanceLifecycleOperationRepository(session)
        provision = repo.get(planned.provision_operation_id or uuid4())
        assert provision and planned.start_operation_id
        running = state is State.RUNNING
        bad = state in {State.FAILED, State.RECONCILIATION_REQUIRED}
        original = replace(
            provision,
            id=planned.start_operation_id,
            kind=Kind.START,
            state=state,
            resulting_state=None
            if running
            else ManagedInstanceSemanticState.RUNNING_NOT_READY,
            completed_at=None if running else provision.completed_at,
            failure_reason="fake_failure" if bad else None,
            reconciliation_required=state is State.RECONCILIATION_REQUIRED,
        )
        repo.create(original)
    with pytest.raises(CreateTransitionError, match="already_recorded"):
        env.start.start(planned.id, version=planned.version)
    with env.sessions() as session:
        assert (
            ManagedInstanceLifecycleOperationRepository(session).get(original.id)
            == original
        )
    assert env.broker.calls == 0


def test_missing_uncertain_receipt_does_not_regenerate_authority(
    env: Environment,
) -> None:
    env.advance()
    env.broker.fault = "before_dispatch"
    with pytest.raises(Interrupted):
        env.start.start(env.op.id, version=env.op.version)
    op = env.repo.get(env.op.id)
    assert op and op.start_operation_id
    with env.sessions.begin() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(op.start_operation_id)
        )
        assert child
        session.delete(child)
    assert env.start.start(op.id, version=op.version) == op
    with env.sessions() as session:
        assert (
            session.get(ManagedStartAuthorizationRow, str(op.start_operation_id))
            is None
        )
    assert env.broker.calls == 1 and env.broker.starts == 0


def test_multi_runtime_legacy_policy_is_never_silently_changed(
    env: Environment,
) -> None:
    with env.sessions.begin() as session:
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy
        policy.retain_profiles = False
        policy.retained_omega_target_id = None
        session.add(ManagedResourcePolicyRow(id=1, max_running=4, idle_minutes=60))
    env.advance()
    assert env.op.stage == "startup" and env.broker.calls == 0
    with env.sessions() as session:
        saved = session.get(ManagedResourcePolicyRow, 1)
        assert saved and (saved.max_running, saved.idle_minutes) == (4, 60)


def test_optional_bridge_is_not_prepared(env: Environment) -> None:
    from typing import cast
    from unittest.mock import Mock

    from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService

    bridge = Mock(spec=ManagedBridgeTrustService)
    env.lifecycle._bridge_trust = cast(ManagedBridgeTrustService, bridge)
    env.advance()
    result = env.start.start(env.op.id, version=env.op.version)
    env.readiness.close()
    bridge.prepare_background.assert_not_called()
    assert (
        result.stage is CreateStage.STARTUP
        and result.checkpoint is CreateCheckpoint.CONFIRMED
    )


@pytest.mark.parametrize("drift", ["expired", "owner"])
def test_ownership_lost_after_native_reply_cannot_confirm_or_replay(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    from typing import Any

    from kodi_lab.broker.operations import StartManagedContainerResult

    env.advance()
    start = env.broker.start_managed_container

    def changed(**kwargs: Any) -> StartManagedContainerResult:
        result = start(**kwargs)
        with env.sessions.begin() as session:
            lease = session.get(ManagedInstanceMutationLeaseRow, str(env.op.target_id))
            assert lease
            if drift == "expired":
                lease.lease_expires_at = "2000-01-01T00:00:00.000000Z"
            else:
                lease.lease_token_digest = "f" * 64
        return result

    monkeypatch.setattr(env.broker, "start_managed_container", changed)
    result = env.start.start(env.op.id, version=env.op.version)
    assert result.checkpoint in {
        CreateCheckpoint.DISPATCHED,
        CreateCheckpoint.NEEDS_ATTENTION,
    }
    assert env.start.start(result.id, version=result.version) == result
    assert env.broker.starts == env.broker.calls == 1

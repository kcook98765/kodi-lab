"""Transactional child/lease/result binding with fake Docker, never Start."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import create_kodi_provisioning as module
from kodi_lab.broker.operations import ProvisionManagedContainerResult
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as ChildState
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateOutcome, CreateStage
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_provisioning_support import Environment, Interrupted
from tests.runtime.test_recipe_installation import _ready


@pytest.fixture
def env(factory: sessionmaker[Session]) -> Iterator[Environment]:
    e = Environment(factory)
    e.registered()
    try:
        yield e
    finally:
        e.close()


def test_provision_retains_exact_child_container_pin_and_final_projection(
    env: Environment,
) -> None:
    planned = env.repo.plan(env.op.id, version=env.op.version)
    result = env.provisioning.provision(planned.id, version=planned.version)
    assert result.checkpoint is CreateCheckpoint.CONFIRMED
    assert (
        result.stage is CreateStage.PROVISIONING and result.start_operation_id is None
    )
    assert result.container_id == "a" * 64
    assert result.provision_operation_id == planned.provision_operation_id
    assert env.provisioning.provision(result.id, version=result.version) == result
    assert env.broker.provisions == 1 and env.broker.launches == []
    with env.sessions() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(result.provision_operation_id)
        )
        instance = session.get(ManagedInstanceRow, str(result.target_id))
        assert child and child.state is ChildState.SUCCEEDED
        assert instance and instance.runtime_revision_id == str(
            result.runtime_revision_id
        )
        assert instance.runtime_state_revision == result.runtime_state_revision
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(result.target_id)) is None
        )
        for model in (AgentWorkspaceRow, DevelopmentSessionRow):
            assert session.scalars(select(model)).all() == []


@pytest.mark.parametrize(
    "fault",
    [
        "before_dispatch",
        "lost_response",
        "wrong_container",
        "wrong_image",
        "reused_container",
        "replacement",
    ],
)
def test_ambiguous_or_wrong_result_never_reprovisions(
    env: Environment, fault: str
) -> None:
    env.broker.fault = fault
    if fault == "before_dispatch":
        with pytest.raises(Interrupted):
            env.provisioning.provision(env.op.id, version=env.op.version)
        result = env.repo.get(env.op.id)
        assert result
    else:
        result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.checkpoint is CreateCheckpoint.NEEDS_ATTENTION
    assert result.outcome is CreateOutcome.UNCERTAIN
    assert (
        result.provision_operation_id is not None and result.dispatched_at is not None
    )
    count = env.broker.provisions
    assert env.provisioning.provision(result.id, version=result.version) == result
    assert env.broker.provisions == count
    if fault == "replacement":
        assert result.container_id == "a" * 64  # Never rebind to the replacement.
    with env.sessions() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(result.provision_operation_id)
        )
        assert child and child.state is not ChildState.SUCCEEDED


@pytest.mark.parametrize(
    "boundary", ["claim", "capture", "after_capture", "terminal", "after_terminal"]
)
def test_interruptions_preserve_receipt_and_binding(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    if boundary in {"claim", "capture", "terminal"}:
        name = {
            "claim": "claim_provision",
            "capture": "capture_provision",
            "terminal": "finish_provision",
        }[boundary]
        original = getattr(module, name)

        def crash(*args: object, **kwargs: object) -> None:
            original(*args, **kwargs)
            raise Interrupted()

        monkeypatch.setattr(module, name, crash)
    elif boundary == "after_capture":
        monkeypatch.setattr(
            env.lifecycle._status,
            "get",
            lambda *_a: (_ for _ in ()).throw(Interrupted()),
        )
    else:
        provision = env.lifecycle.provision

        def done(*args: object, **kwargs: object) -> None:
            provision(*args, **kwargs)  # type: ignore[arg-type]
            raise Interrupted()

        monkeypatch.setattr(env.lifecycle, "provision", done)
    with pytest.raises(Interrupted):
        env.provisioning.provision(env.op.id, version=env.op.version)
    result = env.repo.get(env.op.id)
    assert result and result.provision_operation_id is not None
    with env.sessions() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(result.provision_operation_id)
        )
        lease = session.get(ManagedInstanceMutationLeaseRow, str(result.target_id))
        if boundary == "claim":
            assert child is None and lease is None and result.dispatched_at is None
            assert env.broker.provisions == 0
        elif boundary == "after_terminal":
            assert child and child.state is ChildState.SUCCEEDED and lease is None
            assert result.checkpoint is CreateCheckpoint.CONFIRMED
        else:
            assert child and child.state is ChildState.RUNNING and lease is not None
            assert result.outcome is CreateOutcome.UNCERTAIN
            assert (result.container_id is not None) == (
                boundary in {"after_capture", "terminal"}
            )
    before = env.broker.provisions
    assert env.provisioning.provision(result.id, version=result.version) == result
    assert env.broker.provisions == before


def test_retained_no_selection_provision_keeps_selection_and_profile_empty(
    env: Environment,
) -> None:
    with env.sessions.begin() as session:
        session.add(
            DevelopmentSessionPolicyRow(
                id=1,
                retain_profiles=True,
                retained_omega_target_id=str(env.op.target_id),
            )
        )
    result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.checkpoint is CreateCheckpoint.CONFIRMED
    with env.sessions() as session:
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert policy and policy.active_generation is None
        assert session.scalars(select(DevelopmentSessionRow)).all() == []


def test_existing_container_is_never_adopted_or_replaced(env: Environment) -> None:
    from kodi_lab.broker.docker_inventory import (
        ContainerHealthState,
        ContainerRuntimeState,
        ManagedContainerRecord,
    )

    env.broker.records.append(
        ManagedContainerRecord(
            str(env.op.target_id),
            "d" * 64,
            str(env.op.image_id),
            ContainerRuntimeState.STOPPED,
            ContainerHealthState.NOT_CONFIGURED,
            str(env.op.runtime_revision_id),
        )
    )
    result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.outcome is CreateOutcome.REFUSED and result.dispatched_at is None
    assert env.broker.provisions == 0 and env.broker.records[0].container_id == "d" * 64


def test_changed_pin_refuses_before_child_or_broker_claim(env: Environment) -> None:
    other = _ready(env.sessions, generation="omega")
    with env.sessions.begin() as session:
        ready = session.get(RuntimeRecipeInstallationRow, str(other))
        instance = session.get(ManagedInstanceRow, str(env.op.target_id))
        assert ready and ready.runtime_revision_id and instance
        replacement = ready.runtime_revision_id
        instance.runtime_revision_id = replacement
    result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.outcome is CreateOutcome.REFUSED and result.dispatched_at is None
    with env.sessions() as session:
        assert (
            session.get(
                ManagedInstanceLifecycleOperationRow, str(result.provision_operation_id)
            )
            is None
        )
        instance = session.get(ManagedInstanceRow, str(env.op.target_id))
        assert instance and instance.runtime_revision_id == replacement
    assert env.broker.provisions == 0


def test_missing_receipt_does_not_authorize_another_provision(env: Environment) -> None:
    env.broker.fault = "before_dispatch"
    with pytest.raises(Interrupted):
        env.provisioning.provision(env.op.id, version=env.op.version)
    original = env.repo.get(env.op.id)
    assert original and original.provision_operation_id
    with env.sessions.begin() as session:
        child = session.get(
            ManagedInstanceLifecycleOperationRow, str(original.provision_operation_id)
        )
        assert child
        session.delete(
            child
        )  # Simulate unavailable/corrupt history in this isolated DB.
    assert env.provisioning.provision(original.id, version=original.version) == original
    assert env.broker.provisions == 0


def test_parent_referenced_provision_receipt_survives_pruning(env: Environment) -> None:
    result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.checkpoint is CreateCheckpoint.CONFIRMED
    with env.sessions.begin() as session:
        repo = ManagedInstanceLifecycleOperationRepository(session)
        original = repo.get(result.provision_operation_id or uuid4())
        assert original
        for index in range(25):
            now = original.requested_at + timedelta(seconds=index + 1)
            repo.create(
                replace(
                    original,
                    id=uuid4(),
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )
        assert repo.prune_terminal(result.target_id) == 5
        assert repo.get(original.id) == original
    assert env.repo.get(result.id) == result


@pytest.mark.parametrize("drift", ["lease", "expired", "projection"])
def test_lost_ownership_never_confirms_or_retries(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    provision = env.broker.provision_managed_container

    def changed(*, managed_instance_id: str) -> ProvisionManagedContainerResult:
        result = provision(managed_instance_id=managed_instance_id)
        with env.sessions.begin() as session:
            lease = session.get(ManagedInstanceMutationLeaseRow, managed_instance_id)
            instance = session.get(ManagedInstanceRow, managed_instance_id)
            assert lease and instance
            if drift == "lease":
                lease.lease_token_digest = "f" * 64
            elif drift == "expired":
                lease.lease_expires_at = "2000-01-01T00:00:00.000000Z"
            else:
                instance.runtime_state_revision += 1
        return result

    monkeypatch.setattr(env.broker, "provision_managed_container", changed)
    result = env.provisioning.provision(env.op.id, version=env.op.version)
    assert result.checkpoint in {
        CreateCheckpoint.DISPATCHED,
        CreateCheckpoint.NEEDS_ATTENTION,
    }
    assert result.container_id is None and result.provision_operation_id
    assert env.provisioning.provision(result.id, version=result.version) == result
    assert env.broker.provisions == 1

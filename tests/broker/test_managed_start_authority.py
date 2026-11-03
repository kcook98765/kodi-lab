"""A generic caller cannot borrow, transfer or replay ordinary Start authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.lifecycle_operations import LifecycleOperationConflictError
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.domain import (
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
    ManagedStartReservationRow,
)
from tests.application.test_managed_start import Environment
from tests.application.test_managed_start import env as env
from tests.runtime.test_recipe_installation import factory as factory


def attempt(env: Environment, proof: str | None) -> Any:
    return env.broker.admission.run_start(
        instance_id=str(env.target),
        container_id=env.broker.records[0].container_id,
        revision_id=env.revision,
        ordinary_start_proof=proof,
        dispatch=lambda: env.broker.dispatch(str(env.target)),
    )


@pytest.mark.parametrize("proof_kind", ["missing", "wrong", "valid"])
def test_request_must_possess_its_opaque_proof(
    env: Environment, proof_kind: str
) -> None:
    proof = env.mint()
    if proof_kind == "valid":
        attempt(env, proof)
        assert env.broker.starts == 1
    else:
        with pytest.raises(ManagedCapacityError):
            attempt(env, None if proof_kind == "missing" else "x" * 43)
        assert env.broker.starts == 0
        with env.sessions() as session:
            assert session.scalars(select(ManagedStartReservationRow)).all() == []
            assert (
                session.scalars(select(ManagedStartAuthorizationRow)).one().consumed_at
                is None
            )


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "token",
        "kind",
        "operation",
        "terminal",
        "uncertain",
        "target",
        "revision",
        "container",
        "image",
        "state_revision",
        "lease_binding",
        "consumed",
        "missing_authority",
        "missing_claim",
        "heartbeat",
        "operation_target",
    ],
)
def test_exact_authority_mismatches_never_reach_docker(
    env: Environment, change: str
) -> None:
    proof = env.mint()
    with env.sessions.begin() as session:
        authority = session.scalars(select(ManagedStartAuthorizationRow)).one()
        lease = session.scalars(select(ManagedInstanceMutationLeaseRow)).one()
        op = session.get(ManagedInstanceLifecycleOperationRow, authority.operation_id)
        assert op is not None
        if change == "expired":
            lease.lease_expires_at = timestamp(datetime.now(UTC) - timedelta(seconds=1))
        elif change == "token":
            lease.lease_token_digest = "0" * 64
        elif change == "kind":
            lease.operation_kind = ManagedInstanceMutationKind.RECONSTRUCTION
        elif change == "operation":
            lease.operation_id = str(uuid4())
        elif change in {"terminal", "uncertain"}:
            op.state = (
                ManagedInstanceLifecycleOperationState.SUCCEEDED
                if change == "terminal"
                else ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
            )
            op.completed_at = timestamp(datetime.now(UTC))
            op.resulting_state = "running_not_ready"
            op.reconciliation_required = change == "uncertain"
            op.failure_reason = (
                "broker_runtime_dispatch_unconfirmed" if change == "uncertain" else None
            )
        elif change == "target":
            authority.target_id = str(uuid4())
        elif change == "revision":
            authority.runtime_revision_id = str(uuid4())
        elif change == "container":
            authority.container_id = "b" * 64
        elif change == "image":
            authority.image_id = "sha256:" + "b" * 64
        elif change == "state_revision":
            instance = session.get(ManagedInstanceRow, str(env.target))
            assert instance is not None
            instance.runtime_state_revision += 2
        elif change == "lease_binding":
            authority.lease_digest = "b" * 64
        elif change == "consumed":
            authority.consumed_at = timestamp(datetime.now(UTC))
        elif change == "missing_authority":
            session.delete(authority)
        elif change == "missing_claim":
            lease.lease_token_digest = None
        elif change == "heartbeat":
            lease.heartbeat_at = timestamp(datetime.now(UTC) + timedelta(hours=1))
        else:
            op.original_managed_instance_id = str(uuid4())
    with pytest.raises(ManagedCapacityError):
        attempt(env, proof)
    assert env.broker.starts == 0


@pytest.mark.parametrize(
    "change", ["container", "image", "revision", "unknown", "duplicate"]
)
def test_fresh_inventory_is_rechecked_at_broker_boundary(
    env: Environment, change: str
) -> None:
    proof = env.mint()
    r = env.broker.records[0]
    if change == "duplicate":
        env.broker.records.append(replace(r, container_id="b" * 64))
    else:
        field, value = {
            "container": ("container_id", "b" * 64),
            "image": ("image_id", "sha256:" + "b" * 64),
            "revision": ("runtime_revision_id", str(uuid4())),
            "unknown": ("runtime_state", ContainerRuntimeState.UNKNOWN),
        }[change]
        changes: dict[str, Any] = {field: value}
        env.broker.records[0] = replace(r, **changes)
    with pytest.raises(ManagedCapacityError):
        attempt(env, proof)
    assert env.broker.starts == 0


def test_consumed_proof_cannot_be_replayed_while_lease_is_still_active(
    env: Environment,
) -> None:
    proof = env.mint()
    attempt(env, proof)
    with pytest.raises(ManagedCapacityError):
        attempt(env, proof)
    assert env.broker.starts == 1


def test_ambiguous_dispatch_retains_consumed_authority_and_reservation(
    env: Environment,
) -> None:
    proof = env.mint()

    def lost_response() -> Any:
        env.broker.dispatch(str(env.target))
        raise OSError("response lost")

    with pytest.raises(OSError):
        env.broker.admission.run_start(
            instance_id=str(env.target),
            container_id=env.broker.records[0].container_id,
            revision_id=env.revision,
            ordinary_start_proof=proof,
            dispatch=lost_response,
        )
    with env.sessions() as session:
        assert (
            session.scalars(select(ManagedStartReservationRow)).one().phase
            == "uncertain"
        )
        assert session.scalars(select(ManagedStartAuthorizationRow)).one().consumed_at
    with pytest.raises(ManagedCapacityError):
        attempt(env, proof)
    assert env.broker.starts == 1


def test_concurrent_request_cannot_borrow_or_replay_authority(env: Environment) -> None:
    proof = env.mint()
    env.broker.entered, env.broker.finish = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(attempt, env, proof)
            assert env.broker.entered.wait(5)
            for candidate in (None, proof, "b" * 43):
                with pytest.raises(ManagedCapacityError):
                    attempt(env, candidate)
            env.broker.finish.set()
            first.result(timeout=5)
    finally:
        env.broker.finish.set()
    assert env.broker.starts == 1


def test_conflicting_web_start_is_refused_during_dispatch(env: Environment) -> None:
    env.broker.entered, env.broker.finish = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(env.lifecycle.start_managed, env.target)
            assert env.broker.entered.wait(5)
            with pytest.raises(
                (DevelopmentSessionConflict, LifecycleOperationConflictError)
            ):
                env.lifecycle.start_managed(env.target)
            env.broker.finish.set()
            first.result(timeout=5)
    finally:
        env.broker.finish.set()
    assert env.broker.starts == 1


def test_proof_from_previous_start_cannot_authorize_a_later_start(
    env: Environment,
) -> None:
    env.lifecycle.start_managed(env.target)
    old = env.broker.proof
    assert old is not None
    env.lifecycle.stop(env.target)
    current = env.mint()
    assert old.get_secret_value() != current
    with pytest.raises(ManagedCapacityError):
        attempt(env, old.get_secret_value())
    attempt(env, current)
    assert env.broker.starts == 2

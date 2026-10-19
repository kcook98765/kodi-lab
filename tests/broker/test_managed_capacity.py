"""Capacity is reserved across concurrent starts, crashes and uncertain outcomes."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, RLock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
    StopManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    PostStartVerificationError,
    RequestError,
    RuntimeStatePreconditionError,
)
from kodi_lab.broker.managed_capacity import (
    ManagedCapacityAdmission,
    ManagedCapacityError,
)
from kodi_lab.broker.operations import OperationRegistry, build_foundation_registry
from kodi_lab.domain.snapshots import ManagedInstanceMutationKind
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import resource_policy, timestamp
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.repositories import TargetRepository
from tests.broker.test_authorization_dispatch import request
from tests.storage.test_repositories import make_runtime, make_target

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


class RegistryProvider:
    def __init__(self, env: Environment) -> None:
        self.env, self.start_calls = env, 0

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        return self.env.inventory()

    def resolve_runtime_revision_id(self, instance_id: str) -> str:
        value = self.env.records[instance_id].runtime_revision_id
        assert value is not None
        return value

    def start_managed_container(
        self, **parameters: object
    ) -> StartManagedContainerRecord:
        identity = str(parameters["managed_instance_id"])
        self.start_calls += 1
        self.env.state(self.env.ids.index(identity), ContainerRuntimeState.RUNNING)
        return StartManagedContainerRecord(
            managed_instance_id=identity,
            container_id=self.env.records[identity].container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            start_performed=True,
        )

    def stop_managed_container(
        self, **parameters: object
    ) -> StopManagedContainerRecord:
        identity = str(parameters["managed_instance_id"])
        self.env.state(self.env.ids.index(identity), ContainerRuntimeState.STOPPED)
        return StopManagedContainerRecord(
            managed_instance_id=identity,
            container_id=self.env.records[identity].container_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )


def _production_registry(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OperationRegistry, RegistryProvider]:
    provider = RegistryProvider(env)
    monkeypatch.setattr(
        "kodi_lab.broker.operations.DockerManagedContainerAdapter", lambda: provider
    )
    monkeypatch.setattr(
        ManagedCapacityAdmission, "production", lambda _inventory: env.admission
    )
    return build_foundation_registry(revision_resolver=provider), provider


class Environment:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.records: dict[str, ManagedContainerRecord] = {}
        self.lock = RLock()
        with sessions.begin() as session:
            revision = make_runtime(session)
            for number in range(6):
                target = make_target(f"Capacity fixture {number}")
                TargetRepository(session).create(target)
                session.add(
                    ManagedInstanceRow(
                        target_id=str(target.id),
                        instance_type="disposable",
                        runtime_revision_id=str(revision.id),
                        runtime_state="stopped",
                        start_on_host_boot=False,
                    )
                )
                session.flush()
                session.add(
                    ManagedInstanceMutationLeaseRow(
                        managed_instance_id=str(target.id),
                        operation_kind=ManagedInstanceMutationKind.LIFECYCLE,
                        operation_id=str(uuid4()),
                        lease_token_digest="a" * 64,
                        acquired_at=timestamp(NOW),
                        heartbeat_at=timestamp(NOW),
                        lease_expires_at=timestamp(NOW + timedelta(minutes=15)),
                    )
                )
                self.records[str(target.id)] = ManagedContainerRecord(
                    managed_instance_id=str(target.id),
                    container_id=f"{number + 1:064x}",
                    image_id="sha256:" + "b" * 64,
                    runtime_state=ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                    runtime_revision_id=str(revision.id),
                )
        self.ids = list(self.records)
        self.admission = ManagedCapacityAdmission(
            sessions, self.inventory, clock=lambda: NOW
        )

    def inventory(self) -> tuple[ManagedContainerRecord, ...]:
        with self.lock:
            return tuple(self.records.values())

    def state(self, index: int, state: ContainerRuntimeState) -> None:
        with self.lock:
            identity = self.ids[index]
            self.records[identity] = replace(
                self.records[identity], runtime_state=state
            )

    def start(
        self, index: int, *, dispatch: object = None
    ) -> StartManagedContainerRecord:
        record = self.records[self.ids[index]]

        def perform() -> StartManagedContainerRecord:
            if isinstance(dispatch, BaseException):
                raise dispatch
            performed = record.runtime_state is not ContainerRuntimeState.RUNNING
            self.state(index, ContainerRuntimeState.RUNNING)
            return StartManagedContainerRecord(
                managed_instance_id=record.managed_instance_id,
                container_id=record.container_id,
                runtime_state=ContainerRuntimeState.RUNNING,
                start_performed=performed,
            )

        assert record.runtime_revision_id is not None
        return self.admission.run_start(
            instance_id=record.managed_instance_id,
            container_id=record.container_id,
            revision_id=record.runtime_revision_id,
            dispatch=perform,
        )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Environment]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'capacity.db'}")
    Base.metadata.create_all(engine)
    yield Environment(create_session_factory(engine))
    engine.dispose()


def test_four_parallel_start_reservations_block_fifth_until_capacity_is_free(
    env: Environment,
) -> None:
    entered = Barrier(5)
    release = Event()

    def run(index: int) -> StartManagedContainerRecord:
        record = env.records[env.ids[index]]

        def dispatch() -> StartManagedContainerRecord:
            entered.wait(timeout=10)
            assert release.wait(timeout=10)
            env.state(index, ContainerRuntimeState.RUNNING)
            return StartManagedContainerRecord(
                managed_instance_id=record.managed_instance_id,
                container_id=record.container_id,
                runtime_state=ContainerRuntimeState.RUNNING,
                start_performed=True,
            )

        assert record.runtime_revision_id is not None
        return env.admission.run_start(
            instance_id=record.managed_instance_id,
            container_id=record.container_id,
            revision_id=record.runtime_revision_id,
            dispatch=dispatch,
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(run, index) for index in range(4)]
        try:
            entered.wait(timeout=10)
            with pytest.raises(
                ManagedCapacityError, match="managed_capacity_full"
            ) as error:
                env.start(4)
            assert (error.value.used, error.value.limit) == (4, 4)
            assert (
                env.records[env.ids[4]].runtime_state is ContainerRuntimeState.STOPPED
            )
        finally:
            release.set()
        assert all(future.result(timeout=10).start_performed for future in futures)
    with env.sessions() as session:
        assert list(session.scalars(select(ManagedStartReservationRow))) == []
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
        env.start(4)
    env.state(0, ContainerRuntimeState.STOPPED)
    assert env.start(4).start_performed


def test_existing_running_instances_are_counted_without_eviction(
    env: Environment,
) -> None:
    for index in range(5):
        env.state(index, ContainerRuntimeState.RUNNING)
    assert not env.start(0).start_performed
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full") as error:
        env.start(5)
    assert error.value.used == 5
    assert all(
        env.records[env.ids[index]].runtime_state is ContainerRuntimeState.RUNNING
        for index in range(5)
    )


def test_uncertain_start_keeps_capacity_until_exact_confirmed_stop(
    env: Environment,
) -> None:
    for index in range(1, 4):
        env.state(index, ContainerRuntimeState.RUNNING)
    with pytest.raises(PostStartVerificationError):
        env.start(0, dispatch=PostStartVerificationError("fixture unknown result"))
    with env.sessions() as session:
        saved = session.get(ManagedStartReservationRow, env.ids[0])
        assert saved is not None and saved.phase == "uncertain"
    with pytest.raises(ManagedCapacityError, match="requires_reconciliation"):
        env.start(0)
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
        env.start(4)
    record = env.records[env.ids[0]]
    assert record.runtime_revision_id is not None
    env.admission.confirmed_stop(
        instance_id=env.ids[0],
        container_id="f" * 64,
        revision_id=record.runtime_revision_id,
    )
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
        env.start(4)
    env.admission.confirmed_stop(
        instance_id=env.ids[0],
        container_id=record.container_id,
        revision_id=record.runtime_revision_id,
    )
    assert env.start(4).start_performed


def test_crash_after_reservation_survives_new_admission_service(
    env: Environment,
) -> None:
    record = env.records[env.ids[0]]
    assert record.runtime_revision_id is not None
    env.admission._claim(env.ids[0], record.container_id, record.runtime_revision_id)
    env.admission = ManagedCapacityAdmission(
        env.sessions, env.inventory, clock=lambda: NOW
    )
    with pytest.raises(ManagedCapacityError, match="requires_reconciliation"):
        env.start(0)
    with env.sessions() as session:
        saved = session.get(ManagedStartReservationRow, env.ids[0])
        assert saved is not None and saved.phase == "reserved"


def test_proven_predispatch_failure_releases_only_its_reservation(
    env: Environment,
) -> None:
    with pytest.raises(RuntimeStatePreconditionError):
        env.start(0, dispatch=RuntimeStatePreconditionError("fixture changed state"))
    with env.sessions() as session:
        assert session.get(ManagedStartReservationRow, env.ids[0]) is None


@pytest.mark.parametrize(
    "uncertain", [ContainerRuntimeState.TRANSITIONAL, ContainerRuntimeState.UNKNOWN]
)
def test_uncertain_runtime_consumes_capacity(
    env: Environment, uncertain: ContainerRuntimeState
) -> None:
    with env.sessions.begin() as session:
        resource_policy(session).max_running = 1
    env.state(0, uncertain)
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
        env.start(1)


def test_missing_mutation_authority_cannot_reserve_or_dispatch(
    env: Environment,
) -> None:
    with env.sessions.begin() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, env.ids[0])
        assert lease is not None
        session.delete(lease)
    with pytest.raises(
        ManagedCapacityError, match="managed_start_authority_unavailable"
    ):
        env.start(0)
    with env.sessions() as session:
        assert session.get(ManagedStartReservationRow, env.ids[0]) is None


def test_production_registry_start_uses_shared_admission(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, provider = _production_registry(env, monkeypatch)
    for index in range(4):
        env.state(index, ContainerRuntimeState.RUNNING)
    parameters: dict[str, object] = {
        "managed_instance_id": env.ids[4],
        "expected_container_id": env.records[env.ids[4]].container_id,
        "expected_runtime_state": "stopped",
    }
    with pytest.raises(RequestError) as error:
        registry.dispatch(request("start_managed_container", parameters))
    assert error.value.code == "managed_capacity_full"
    assert "4/4" in error.value.safe_message
    assert provider.start_calls == 0
    env.state(0, ContainerRuntimeState.STOPPED)
    registry.dispatch(request("start_managed_container", parameters))
    assert provider.start_calls == 1


def test_verified_stop_through_registry_releases_uncertain_slot(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, _provider = _production_registry(env, monkeypatch)
    with pytest.raises(PostStartVerificationError):
        env.start(0, dispatch=PostStartVerificationError("fixture uncertain start"))
    parameters: dict[str, object] = {
        "managed_instance_id": env.ids[0],
        "expected_container_id": env.records[env.ids[0]].container_id,
        "expected_runtime_state": "stopped",
    }
    registry.dispatch(request("stop_managed_container", parameters))
    with env.sessions() as session:
        assert session.get(ManagedStartReservationRow, env.ids[0]) is None

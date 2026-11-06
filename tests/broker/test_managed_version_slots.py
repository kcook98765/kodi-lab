"""Different target UUIDs must not bypass per-version writable-profile admission."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import RequestError
from kodi_lab.broker.managed_capacity import (
    ManagedCapacityAdmission,
    ManagedCapacityError,
)
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.domain import PythonBytecodePolicy
from kodi_lab.storage.managed_version_slots import managed_version_slots
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    RuntimeDefinitionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import TargetRepository
from tests.broker.test_authorization_dispatch import request
from tests.broker.test_managed_capacity import Environment
from tests.broker.test_managed_capacity import env as env
from tests.storage.test_repositories import make_runtime, make_target


def peer(env: Environment, index: int = 2) -> ManagedContainerRecord:
    original = env.records[env.ids[index]]
    with env.sessions.begin() as session:
        target = session.get(TargetRow, original.managed_instance_id)
        assert target is not None
        other = replace(make_target("Other profile"), kodi_version=target.kodi_version)
        TargetRepository(session).create(other)
        session.add(
            ManagedInstanceRow(
                target_id=str(other.id),
                instance_type="disposable",
                runtime_revision_id=original.runtime_revision_id,
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    return replace(original, managed_instance_id=str(other.id), container_id="f" * 64)


def provision(
    env: Environment, record: ManagedContainerRecord, *, created: bool = True
) -> ProvisionedManagedContainerRecord:
    with env.lock:
        env.records[record.managed_instance_id] = record
    assert record.runtime_revision_id is not None
    return ProvisionedManagedContainerRecord(
        record.managed_instance_id,
        record.runtime_revision_id,
        record.container_id,
        record.image_id,
        record.runtime_state,
        created,
    )


def test_four_logical_slots_have_distinct_canonical_identities() -> None:
    slots = managed_version_slots()
    assert [slot.generation for slot in slots] == ["matrix", "nexus", "omega", "piers"]
    assert [slot.major for slot in slots] == [19, 20, 21, 22]
    assert len({slot.canonical_target_id for slot in slots}) == 4
    assert slots[-1].kodi_version == "22.0-rc.1"


def test_second_profile_is_refused_without_touching_existing_inventory(
    env: Environment,
) -> None:
    other = peer(env)
    before = env.inventory()
    assert other.runtime_revision_id is not None
    with pytest.raises(ManagedCapacityError, match="managed_version_slot_conflict"):
        env.admission.run_provision(
            instance_id=other.managed_instance_id,
            revision_id=other.runtime_revision_id,
            dispatch=lambda: provision(env, other),
        )
    assert env.inventory() == before
    with env.sessions() as session:
        assert session.scalar(select(ManagedProvisionReservationRow)) is None


def test_duplicate_legacy_profiles_block_start_without_adoption_or_deletion(
    env: Environment,
) -> None:
    other = peer(env)
    env.records[other.managed_instance_id] = other
    before = env.inventory()
    with pytest.raises(ManagedCapacityError, match="managed_version_slot_conflict"):
        env.start(2)
    assert env.inventory() == before
    with env.sessions() as session:
        assert session.scalar(select(ManagedStartReservationRow)) is None


@pytest.mark.parametrize(
    ("index", "version"), [(2, "21.4"), (3, "22.0-beta.2"), (3, "22.0-rc.1")]
)
def test_different_runtime_release_or_revision_cannot_create_another_profile(
    env: Environment, index: int, version: str
) -> None:
    other = peer(env, index)
    with env.sessions.begin() as session:
        revision = make_runtime(session)
        definition = session.get(
            RuntimeDefinitionRow, str(revision.runtime_definition_id)
        )
        target = session.get(TargetRow, other.managed_instance_id)
        instance = session.get(ManagedInstanceRow, other.managed_instance_id)
        assert definition is not None and target is not None and instance is not None
        definition.kodi_version = target.kodi_version = version
        instance.runtime_revision_id = str(revision.id)
    other = replace(other, runtime_revision_id=str(revision.id))
    before = env.inventory()
    with pytest.raises(ManagedCapacityError, match="managed_version_slot_conflict"):
        env.admission.run_provision(
            instance_id=other.managed_instance_id,
            revision_id=str(revision.id),
            dispatch=lambda: provision(env, other),
        )
    assert env.inventory() == before


def test_duplicate_container_for_same_target_is_also_a_conflict(
    env: Environment,
) -> None:
    original = env.records[env.ids[2]]
    env.admission = ManagedCapacityAdmission(
        env.sessions,
        lambda: (*env.inventory(), replace(original, container_id="f" * 64)),
        clock=env.admission._clock,
    )
    with pytest.raises(
        ManagedCapacityError, match="managed_start_identity_unavailable"
    ):
        env.start(2)
    assert all(r.runtime_state == "stopped" for r in env.inventory())


def test_registration_without_profile_does_not_consume_a_version_slot(
    env: Environment,
) -> None:
    peer(env)
    original = env.records[env.ids[2]]
    assert env.start(2).start_performed
    assert env.records[env.ids[2]].container_id == original.container_id


def test_parallel_creates_across_broker_services_share_version_reservation(
    env: Environment,
) -> None:
    other = peer(env)
    original = env.records.pop(env.ids[2])
    entered, release = Event(), Event()

    def dispatch() -> ProvisionedManagedContainerRecord:
        entered.set()
        assert release.wait(timeout=10)
        return provision(env, original)

    assert original.runtime_revision_id is not None
    assert other.runtime_revision_id is not None
    second = ManagedCapacityAdmission(env.sessions, env.inventory)
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(
            env.admission.run_provision,
            instance_id=original.managed_instance_id,
            revision_id=original.runtime_revision_id,
            dispatch=dispatch,
        )
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(
                ManagedCapacityError, match="managed_version_slot_pending"
            ):
                second.run_provision(
                    instance_id=other.managed_instance_id,
                    revision_id=other.runtime_revision_id,
                    dispatch=lambda: provision(env, other),
                )
        finally:
            release.set()
        assert first.result(timeout=10).created
    result = second.run_provision(
        instance_id=original.managed_instance_id,
        revision_id=original.runtime_revision_id,
        dispatch=lambda: provision(env, original, created=False),
    )
    assert not result.created and result.container_id == original.container_id
    assert other.managed_instance_id not in env.records


def test_lost_create_response_remains_fenced_across_service_restart(
    env: Environment,
) -> None:
    other = peer(env)
    original = env.records.pop(env.ids[2])

    def lost_response() -> ProvisionedManagedContainerRecord:
        provision(env, original)
        raise TimeoutError("simulated lost broker reply")

    assert original.runtime_revision_id is not None
    with pytest.raises(TimeoutError):
        env.admission.run_provision(
            instance_id=original.managed_instance_id,
            revision_id=original.runtime_revision_id,
            dispatch=lost_response,
        )
    with env.sessions() as session:
        reservation = session.get(
            ManagedProvisionReservationRow, original.managed_instance_id
        )
        assert reservation is not None and reservation.phase == "uncertain"
        operation_id = reservation.operation_id
    second = ManagedCapacityAdmission(env.sessions, env.inventory)
    # Even an absent subsequent inventory cannot erase the uncertain create.
    env.records.pop(original.managed_instance_id)
    assert other.runtime_revision_id is not None
    with pytest.raises(ManagedCapacityError, match="managed_version_slot_pending"):
        second.run_provision(
            instance_id=other.managed_instance_id,
            revision_id=other.runtime_revision_id,
            dispatch=lambda: provision(env, other),
        )
    with pytest.raises(
        ManagedCapacityError, match="managed_provision_requires_reconciliation"
    ):
        second.run_provision(
            instance_id=original.managed_instance_id,
            revision_id=original.runtime_revision_id,
            dispatch=lambda: provision(env, original),
        )
    with env.sessions() as session:
        reservation = session.get(
            ManagedProvisionReservationRow, original.managed_instance_id
        )
        assert reservation is not None and reservation.operation_id == operation_id
    assert other.managed_instance_id not in env.records


@pytest.mark.parametrize("field", ["target", "pin", "version"])
def test_unidentifiable_inventory_blocks_new_work(env: Environment, field: str) -> None:
    unknown = env.records[env.ids[0]]
    if field == "target":
        env.records[env.ids[0]] = replace(unknown, managed_instance_id=str(uuid4()))
    elif field == "pin":
        env.records[env.ids[0]] = replace(unknown, runtime_revision_id=str(uuid4()))
    else:
        with env.sessions.begin() as session:
            target = session.get(TargetRow, unknown.managed_instance_id)
            assert target is not None
            target.kodi_version = "99.0"
    with pytest.raises(
        ManagedCapacityError, match="managed_version_slot_identity_unavailable"
    ):
        env.start(2)
    assert all(r.runtime_state == "stopped" for r in env.inventory())


def test_reprovision_cannot_replace_a_retained_container_identity(
    env: Environment,
) -> None:
    original = env.records[env.ids[2]]
    replacement = replace(original, container_id="e" * 64)
    assert original.runtime_revision_id is not None
    with pytest.raises(
        ManagedCapacityError, match="managed_provision_result_unverified"
    ):
        env.admission.run_provision(
            instance_id=original.managed_instance_id,
            revision_id=original.runtime_revision_id,
            dispatch=lambda: provision(env, replacement),
        )
    with env.sessions() as session:
        reservation = session.get(
            ManagedProvisionReservationRow, original.managed_instance_id
        )
        assert reservation is not None and reservation.phase == "uncertain"


def test_stopped_different_version_can_be_prepared_while_one_runs(
    env: Environment,
) -> None:
    assert env.start(0).start_performed
    original = env.records.pop(env.ids[2])
    assert original.runtime_revision_id is not None
    assert env.admission.run_provision(
        instance_id=original.managed_instance_id,
        revision_id=original.runtime_revision_id,
        dispatch=lambda: provision(env, original),
    ).created
    with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
        env.start(2)
    assert (
        env.records[original.managed_instance_id].runtime_state
        is ContainerRuntimeState.STOPPED
    )


def test_production_provision_registry_enforces_version_uniqueness(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = peer(env)
    env.records[other.managed_instance_id] = other
    original = env.records[env.ids[2]]
    assert original.runtime_revision_id is not None
    before = env.inventory()

    class Provider:
        calls = 0

        def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
            return env.inventory()

        def resolve(self, identity: str) -> ManagedContainerProvisioningSpec:
            assert identity == original.managed_instance_id
            return ManagedContainerProvisioningSpec(
                identity,
                str(original.runtime_revision_id),
                original.image_id,
                "kodi-lab-managed-" + identity.replace("-", ""),
                1000,
                1000,
                PythonBytecodePolicy.SUPPRESS,
            )

        def provision_managed_container(
            self, _spec: Any
        ) -> ProvisionedManagedContainerRecord:
            self.calls += 1
            return provision(env, original, created=False)

    provider = Provider()
    monkeypatch.setattr(
        "kodi_lab.broker.operations.DockerManagedContainerAdapter", lambda: provider
    )
    monkeypatch.setattr(
        ManagedCapacityAdmission, "production", lambda _inventory: env.admission
    )
    registry = build_foundation_registry(provisioning_resolver=provider)
    with pytest.raises(RequestError) as error:
        registry.dispatch(
            request("provision_managed_container", {"managed_instance_id": env.ids[2]})
        )
    assert error.value.code == "managed_version_slot_conflict"
    assert "Another writable profile exists" in error.value.safe_message
    assert provider.calls == 0 and env.inventory() == before

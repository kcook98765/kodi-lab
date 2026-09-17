"""Semantic Docker deprovision security, identity, and postcondition tests."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest
from docker.errors import APIError, NotFound  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    ContainerRuntimeState,
    DeprovisionedManagedContainerRecord,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedError,
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    DockerRemoveError,
    InvalidManagedContainerMetadataError,
    ManagedContainerNotFoundError,
    PostDeprovisionVerificationError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.provisioning import RUNTIME_REVISION_LABEL_KEY


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def revision_id(number: int = 2) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


def inspect_data(
    container_number: int = 1,
    *,
    instance_value: object = instance_id(),
    revision_value: object = revision_id(),
    managed_value: object = MANAGED_LABEL_VALUE,
    state: object = "exited",
    name: object | None = None,
    labels_present: bool = True,
) -> dict[str, object]:
    labels: object = None
    if labels_present:
        labels = {
            MANAGED_LABEL_KEY: managed_value,
            INSTANCE_ID_LABEL_KEY: instance_value,
            RUNTIME_REVISION_LABEL_KEY: revision_value,
            "PRIVATE": "not-returned",
        }
    return {
        "Id": container_id(container_number),
        "Name": name
        if name is not None
        else f"/kodi-lab-managed-{str(instance_value).replace('-', '')}",
        "Config": {"Labels": labels, "Env": ["PRIVATE=secret"]},
        "State": {"Status": state},
        "Mounts": [{"Source": "/private/path"}],
    }


class FakeDeprovisionDockerClient:
    def __init__(
        self,
        *,
        selections: tuple[object | BaseException, ...] = (
            [{"Id": container_id()}],
            [],
        ),
        inspections: tuple[object | BaseException, ...] = (
            inspect_data(),
            NotFound("removed"),
        ),
        remove_error: BaseException | None = None,
    ) -> None:
        self.selections = list(selections)
        self.inspections = list(inspections)
        self.remove_error = remove_error
        self.events: list[tuple[object, ...]] = []
        self.remove_calls: list[tuple[str, bool, bool]] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.closed = False

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        self.events.append(("list", all, filters))
        if not self.selections:
            raise AssertionError("unexpected container selection")
        result = self.selections.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def inspect_container(self, container: str) -> object:
        self.events.append(("inspect", container))
        if not self.inspections:
            raise AssertionError("unexpected inspect")
        result = self.inspections.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def remove_container(self, container: str, *, force: bool, v: bool) -> None:
        self.events.append(("remove", container, force, v))
        self.remove_calls.append((container, force, v))
        if self.remove_error is not None:
            raise self.remove_error

    def start(self, container: str) -> None:
        self.start_calls += 1

    def stop(self, container: str, *, timeout: int) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.closed = True


def deprovision(
    client: FakeDeprovisionDockerClient,
    *,
    requested_instance_id: str = instance_id(),
    expected_container_id: str = container_id(),
    expected_revision_id: str = revision_id(),
    expected_state: ContainerRuntimeState = ContainerRuntimeState.STOPPED,
) -> DeprovisionedManagedContainerRecord:
    return DockerManagedContainerAdapter(lambda: client).deprovision_managed_container(
        managed_instance_id=requested_instance_id,
        expected_container_id=expected_container_id,
        expected_runtime_revision_id=expected_revision_id,
        expected_runtime_state=expected_state,
    )


def test_exact_owned_stopped_container_is_removed_nonforced_without_volumes() -> None:
    client = FakeDeprovisionDockerClient()
    result = deprovision(client)

    assert result == DeprovisionedManagedContainerRecord(
        managed_instance_id=instance_id(),
        removed_container_id=container_id(),
        runtime_revision_id=revision_id(),
        removed=True,
    )
    expected_filters = {
        "label": [
            f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
            f"{INSTANCE_ID_LABEL_KEY}={instance_id()}",
        ]
    }
    assert client.events == [
        ("list", True, expected_filters),
        ("inspect", container_id()),
        ("remove", container_id(), False, False),
        ("inspect", container_id()),
        ("list", True, expected_filters),
    ]
    assert client.remove_calls == [(container_id(), False, False)]
    assert client.start_calls == 0
    assert client.stop_calls == 0
    assert client.closed


def test_no_owned_container_is_not_destructive_idempotent_success() -> None:
    client = FakeDeprovisionDockerClient(selections=([],), inspections=())
    with pytest.raises(ManagedContainerNotFoundError):
        deprovision(client)
    assert client.remove_calls == []


def test_duplicate_owned_containers_fail_before_inspect_or_remove() -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id(1)}, {"Id": container_id(2)}],),
        inspections=(),
    )
    with pytest.raises(AmbiguousManagedContainerError):
        deprovision(client)
    assert client.remove_calls == []
    assert all(event[0] != "inspect" for event in client.events)


@pytest.mark.parametrize(
    "inspection",
    [
        inspect_data(labels_present=False),
        inspect_data(managed_value="false"),
        inspect_data(instance_value=instance_id(3)),
        inspect_data(instance_value="not-a-uuid"),
        inspect_data(name="/legacy-lookalike"),
    ],
)
def test_ownership_or_deterministic_identity_failure_never_removes(
    inspection: dict[str, object],
) -> None:
    client = FakeDeprovisionDockerClient(inspections=(inspection,))
    with pytest.raises(InvalidManagedContainerMetadataError):
        deprovision(client)
    assert client.remove_calls == []


def test_unlabelled_legacy_id_cannot_be_used_as_removal_target() -> None:
    legacy_id = container_id(99)
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": legacy_id}],),
        inspections=(inspect_data(99, labels_present=False, name="/kodi19-matrix"),),
    )
    with pytest.raises(InvalidManagedContainerMetadataError):
        deprovision(client, expected_container_id=legacy_id)
    assert client.remove_calls == []


def test_expected_container_id_is_precondition_not_discovery() -> None:
    client = FakeDeprovisionDockerClient(inspections=(inspect_data(),))
    with pytest.raises(ContainerIdentityPreconditionError):
        deprovision(client, expected_container_id=container_id(2))
    assert client.remove_calls == []
    assert ("inspect", container_id(2)) not in client.events


def test_replacement_b_for_observed_a_is_not_removed() -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id(2)}],),
        inspections=(inspect_data(2),),
    )
    with pytest.raises(ContainerIdentityPreconditionError):
        deprovision(client, expected_container_id=container_id(1))
    assert client.remove_calls == []


@pytest.mark.parametrize("revision", [revision_id(3), "not-a-uuid"])
def test_revision_label_mismatch_or_malformed_value_never_removes(
    revision: str,
) -> None:
    client = FakeDeprovisionDockerClient(
        inspections=(inspect_data(revision_value=revision),)
    )
    expected = (
        RuntimeRevisionPreconditionError
        if revision == revision_id(3)
        else InvalidManagedContainerMetadataError
    )
    with pytest.raises(expected):
        deprovision(client)
    assert client.remove_calls == []


@pytest.mark.parametrize("state", ["running", "restarting", "future-state"])
def test_only_freshly_stopped_state_can_be_removed(state: str) -> None:
    client = FakeDeprovisionDockerClient(inspections=(inspect_data(state=state),))
    with pytest.raises(ContainerMustBeStoppedError):
        deprovision(client)
    assert client.remove_calls == []
    assert client.start_calls == client.stop_calls == 0


def test_adapter_rejects_nonstopped_expected_state_before_docker_access() -> None:
    client = FakeDeprovisionDockerClient()
    with pytest.raises(ContainerMustBeStoppedError):
        deprovision(client, expected_state=ContainerRuntimeState.RUNNING)
    assert client.events == []


def test_remove_failure_is_sanitized_and_not_retried() -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id()}],),
        inspections=(inspect_data(),),
        remove_error=APIError("PRIVATE REMOVE DETAIL"),
    )
    with pytest.raises(DockerRemoveError) as raised:
        deprovision(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.remove_calls == [(container_id(), False, False)]


def test_remove_return_without_id_absence_is_not_success() -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id()}],),
        inspections=(inspect_data(), inspect_data()),
    )
    with pytest.raises(PostDeprovisionVerificationError):
        deprovision(client)
    assert client.remove_calls == [(container_id(), False, False)]


def test_post_remove_inspect_failure_other_than_not_found_is_not_success() -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id()}],),
        inspections=(inspect_data(), APIError("PRIVATE POST INSPECT")),
    )
    with pytest.raises(PostDeprovisionVerificationError) as raised:
        deprovision(client)
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize(
    "remaining",
    ([{"Id": container_id(2)}], APIError("PRIVATE POST LIST"), "malformed"),
)
def test_remaining_replacement_or_unverifiable_label_lookup_is_not_success(
    remaining: object,
) -> None:
    client = FakeDeprovisionDockerClient(
        selections=([{"Id": container_id()}], remaining),
        inspections=(inspect_data(), NotFound("removed")),
    )
    with pytest.raises(PostDeprovisionVerificationError) as raised:
        deprovision(client)
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("stage", ["list", "inspect"])
def test_predelete_docker_reads_are_sanitized(stage: str) -> None:
    client = FakeDeprovisionDockerClient(
        selections=(APIError("PRIVATE LIST"),)
        if stage == "list"
        else ([{"Id": container_id()}],),
        inspections=(APIError("PRIVATE INSPECT"),) if stage == "inspect" else (),
    )
    with pytest.raises(DockerInventoryReadError) as raised:
        deprovision(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.remove_calls == []


def test_daemon_unavailable_before_discovery_is_sanitized() -> None:
    def unavailable() -> FakeDeprovisionDockerClient:
        raise OSError("PRIVATE DAEMON")

    with pytest.raises(DockerDaemonUnavailableError) as raised:
        DockerManagedContainerAdapter(unavailable).deprovision_managed_container(
            managed_instance_id=instance_id(),
            expected_container_id=container_id(),
            expected_runtime_revision_id=revision_id(),
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )
    assert "PRIVATE" not in str(raised.value)


def test_adapter_has_only_the_bounded_semantic_public_methods() -> None:
    public_callables = {
        name
        for name in dir(DockerManagedContainerAdapter)
        if not name.startswith("_")
        and callable(getattr(DockerManagedContainerAdapter, name))
    }
    assert public_callables == {
        "apply_assigned_fixture",
        "apply_managed_instance_snapshot",
        "capture_managed_instance_snapshot",
        "copy_sync_attached_development_source",
        "deprovision_managed_container",
        "list_managed_containers",
        "observe_managed_kodi_runtime",
        "provision_managed_container",
        "start_managed_container",
        "stop_managed_container",
        "verify_attached_development_source_destination",
    }

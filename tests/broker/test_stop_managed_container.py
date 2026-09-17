"""Semantic Docker stop adapter security and concurrency tests."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest
from docker.errors import APIError  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    DOCKER_STOP_TIMEOUT_SECONDS,
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
    StopManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotStoppableError,
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    DockerStopError,
    InvalidManagedContainerMetadataError,
    ManagedContainerNotFoundError,
    PostStopVerificationError,
    RuntimeStatePreconditionError,
)


def instance_id(number: int) -> str:
    return str(UUID(int=number))


def container_id(number: int) -> str:
    return f"{number:064x}"


def inspect_data(
    container_number: int,
    *,
    instance_number: int = 1,
    managed_value: str = MANAGED_LABEL_VALUE,
    instance_value: object | None = None,
    state: str = "running",
    labels_present: bool = True,
) -> dict[str, object]:
    labels: object = None
    if labels_present:
        labels = {
            MANAGED_LABEL_KEY: managed_value,
            INSTANCE_ID_LABEL_KEY: (
                instance_id(instance_number)
                if instance_value is None
                else instance_value
            ),
            "PRIVATE-LABEL": "PRIVATE-VALUE",
        }
    return {
        "Id": container_id(container_number),
        "Image": "sha256:" + ("b" * 64),
        "Config": {
            "Labels": labels,
            "Env": ["PRIVATE_ENV=secret"],
            "Cmd": ["PRIVATE-COMMAND"],
        },
        "State": {"Status": state},
        "Mounts": [{"Source": "/PRIVATE/HOST/PATH"}],
        "NetworkSettings": {"Networks": {"private": {}}},
    }


class FakeStopDockerClient:
    def __init__(
        self,
        *,
        summaries: object | None = None,
        inspections: tuple[object | BaseException, ...] = (),
        list_error: BaseException | None = None,
        stop_error: BaseException | None = None,
    ) -> None:
        self.summaries = [{"Id": container_id(1)}] if summaries is None else summaries
        self.inspections = list(inspections)
        self.list_error = list_error
        self.stop_error = stop_error
        self.events: list[tuple[object, ...]] = []
        self.stop_calls: list[tuple[str, int]] = []
        self.closed = False

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        self.events.append(("list", all, filters))
        if self.list_error is not None:
            raise self.list_error
        return self.summaries  # type: ignore[return-value]

    def inspect_container(self, container: str) -> object:
        self.events.append(("inspect", container))
        if not self.inspections:
            raise AssertionError("unexpected inspect call")
        result = self.inspections.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def start(self, container: str) -> None:
        raise AssertionError(f"unexpected start call for {container}")

    def stop(self, container: str, *, timeout: int) -> None:
        self.events.append(("stop", container, timeout))
        self.stop_calls.append((container, timeout))
        if self.stop_error is not None:
            raise self.stop_error

    def close(self) -> None:
        self.closed = True


def stop(
    client: FakeStopDockerClient,
    *,
    requested_instance_id: str | None = None,
    expected_container_id: str | None = None,
    expected_state: ContainerRuntimeState = ContainerRuntimeState.RUNNING,
) -> StopManagedContainerRecord:
    return DockerManagedContainerAdapter(lambda: client).stop_managed_container(
        managed_instance_id=requested_instance_id or instance_id(1),
        expected_container_id=expected_container_id or container_id(1),
        expected_runtime_state=expected_state,
    )


def test_running_owned_container_stops_with_fixed_timeout_and_postcondition() -> None:
    client = FakeStopDockerClient(
        inspections=(
            inspect_data(1, state="running"),
            inspect_data(1, state="exited"),
        )
    )
    result = stop(client)

    assert result.managed_instance_id == instance_id(1)
    assert result.container_id == container_id(1)
    assert result.runtime_state is ContainerRuntimeState.STOPPED
    assert result.stop_performed is True
    expected_filters = {
        "label": [
            f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
            f"{INSTANCE_ID_LABEL_KEY}={instance_id(1)}",
        ]
    }
    assert client.events == [
        ("list", True, expected_filters),
        ("inspect", container_id(1)),
        ("stop", container_id(1), DOCKER_STOP_TIMEOUT_SECONDS),
        ("inspect", container_id(1)),
    ]
    assert client.stop_calls == [(container_id(1), 10)]
    assert client.closed


def test_adapter_exposes_only_inventory_and_semantic_stop_public_methods() -> None:
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


@pytest.mark.parametrize("expected_state", list(ContainerRuntimeState))
def test_already_stopped_converges_without_stop_for_any_expected_state(
    expected_state: ContainerRuntimeState,
) -> None:
    client = FakeStopDockerClient(inspections=(inspect_data(1, state="exited"),))
    result = stop(client, expected_state=expected_state)
    assert result.runtime_state is ContainerRuntimeState.STOPPED
    assert result.stop_performed is False
    assert client.stop_calls == []


def test_running_container_rejects_expected_stopped_state() -> None:
    client = FakeStopDockerClient(inspections=(inspect_data(1, state="running"),))
    with pytest.raises(RuntimeStatePreconditionError):
        stop(client, expected_state=ContainerRuntimeState.STOPPED)
    assert client.stop_calls == []


@pytest.mark.parametrize("state", ["restarting", "future-state"])
def test_transitional_and_unknown_states_are_not_mutated(state: str) -> None:
    client = FakeStopDockerClient(inspections=(inspect_data(1, state=state),))
    with pytest.raises(ContainerStateNotStoppableError):
        stop(client, expected_state=ContainerRuntimeState.UNKNOWN)
    assert client.stop_calls == []


def test_no_matching_owned_container_is_not_found() -> None:
    client = FakeStopDockerClient(summaries=[], inspections=())
    with pytest.raises(ManagedContainerNotFoundError):
        stop(client)
    assert client.stop_calls == []


def test_duplicate_exact_instance_matches_are_ambiguous() -> None:
    client = FakeStopDockerClient(
        summaries=[{"Id": container_id(1)}, {"Id": container_id(2)}],
        inspections=(),
    )
    with pytest.raises(AmbiguousManagedContainerError):
        stop(client)
    assert client.stop_calls == []
    assert all(event[0] != "inspect" for event in client.events)


@pytest.mark.parametrize(
    "inspection",
    [
        inspect_data(1, labels_present=False),
        inspect_data(1, managed_value="false"),
        inspect_data(1, instance_number=2),
        inspect_data(1, instance_value="not-a-canonical-uuid"),
    ],
)
def test_fresh_ownership_revalidation_blocks_mutation(
    inspection: dict[str, object],
) -> None:
    client = FakeStopDockerClient(inspections=(inspection,))
    with pytest.raises(InvalidManagedContainerMetadataError):
        stop(client)
    assert client.stop_calls == []


def test_wrong_expected_container_identity_is_rejected() -> None:
    client = FakeStopDockerClient(inspections=(inspect_data(1),))
    with pytest.raises(ContainerIdentityPreconditionError):
        stop(client, expected_container_id=container_id(2))
    assert client.stop_calls == []


def test_replacement_container_with_same_instance_identity_is_rejected() -> None:
    client = FakeStopDockerClient(
        summaries=[{"Id": container_id(2)}],
        inspections=(inspect_data(2, instance_number=1),),
    )
    with pytest.raises(ContainerIdentityPreconditionError):
        stop(client, expected_container_id=container_id(1))
    assert client.stop_calls == []
    assert ("inspect", container_id(1)) not in client.events


def test_caller_cannot_target_unlabelled_legacy_container_by_id() -> None:
    legacy_id = container_id(99)
    client = FakeStopDockerClient(
        summaries=[{"Id": legacy_id}],
        inspections=(inspect_data(99, labels_present=False),),
    )
    with pytest.raises(InvalidManagedContainerMetadataError):
        stop(client, expected_container_id=legacy_id)
    assert client.stop_calls == []


def test_stop_return_without_stopped_state_fails_postcondition() -> None:
    client = FakeStopDockerClient(
        inspections=(inspect_data(1, state="running"), inspect_data(1, state="running"))
    )
    with pytest.raises(PostStopVerificationError):
        stop(client)
    assert client.stop_calls == [(container_id(1), 10)]


@pytest.mark.parametrize(
    "final_inspection",
    [
        APIError("PRIVATE-CONTAINER-DISAPPEARED"),
        inspect_data(1, state="exited", managed_value="false"),
        inspect_data(2, state="exited"),
    ],
)
def test_post_stop_disappearance_ownership_or_identity_change_fails_verification(
    final_inspection: object | BaseException,
) -> None:
    client = FakeStopDockerClient(
        inspections=(inspect_data(1, state="running"), final_inspection)
    )
    with pytest.raises(PostStopVerificationError) as raised:
        stop(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.stop_calls == [(container_id(1), 10)]


def test_daemon_unavailable_before_selection_is_distinct() -> None:
    def unavailable() -> FakeStopDockerClient:
        raise OSError("PRIVATE-DAEMON-DETAIL")

    with pytest.raises(DockerDaemonUnavailableError) as raised:
        DockerManagedContainerAdapter(unavailable).stop_managed_container(
            managed_instance_id=instance_id(1),
            expected_container_id=container_id(1),
            expected_runtime_state=ContainerRuntimeState.RUNNING,
        )
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("stage", ["list", "inspect"])
def test_selection_read_failures_are_sanitized(stage: str) -> None:
    error = APIError("PRIVATE-READ-DETAIL")
    client = FakeStopDockerClient(
        inspections=(error,) if stage == "inspect" else (),
        list_error=error if stage == "list" else None,
    )
    with pytest.raises(DockerInventoryReadError) as raised:
        stop(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.stop_calls == []


def test_stop_failure_is_sanitized_and_not_retried() -> None:
    client = FakeStopDockerClient(
        inspections=(inspect_data(1),),
        stop_error=APIError("PRIVATE-STOP-DETAIL"),
    )
    with pytest.raises(DockerStopError) as raised:
        stop(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.stop_calls == [(container_id(1), 10)]

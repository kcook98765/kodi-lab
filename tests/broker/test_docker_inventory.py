"""Read-only Docker adapter ownership, mapping, and bounds tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from docker.errors import APIError  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    MAX_MANAGED_CONTAINERS,
    ContainerHealthState,
    ContainerRuntimeState,
    DockerManagedContainerInventory,
)
from kodi_lab.broker.errors import (
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
)

IMAGE_ID = "sha256:" + ("b" * 64)
LEGACY_LABEL_KEY = "com.docker.compose.project"


def container_id(number: int) -> str:
    return f"{number:064x}"


def instance_id(number: int) -> str:
    return str(UUID(int=number))


def inspect_data(
    number: int,
    *,
    labels: Mapping[str, str] | None = None,
    state: str = "running",
    health: str | None = None,
) -> dict[str, object]:
    state_data: dict[str, object] = {"Status": state}
    if health is not None:
        state_data["Health"] = {"Status": health, "Log": ["PRIVATE-HEALTH-LOG"]}
    return {
        "Id": container_id(number),
        "Image": IMAGE_ID,
        "Config": {
            "Labels": dict(labels) if labels is not None else None,
            "Env": ["PRIVATE_ENV=secret"],
            "Cmd": ["PRIVATE-COMMAND"],
            "Entrypoint": ["PRIVATE-ENTRYPOINT"],
        },
        "State": state_data,
        "Mounts": [{"Source": "/PRIVATE/HOST/PATH"}],
        "NetworkSettings": {"Networks": {"private": {"IPAddress": "10.0.0.2"}}},
        "HostConfig": {"PortBindings": {"8080/tcp": [{"HostPort": "12345"}]}},
    }


def managed_labels(number: int) -> dict[str, str]:
    return {
        MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
        INSTANCE_ID_LABEL_KEY: instance_id(number),
    }


class FakeDockerClient:
    def __init__(
        self,
        inspections: Mapping[str, object],
        *,
        summaries: object | None = None,
        list_error: BaseException | None = None,
        inspect_error: BaseException | None = None,
    ) -> None:
        self.inspections = dict(inspections)
        self.summaries = (
            [{"Id": identity} for identity in inspections]
            if summaries is None
            else summaries
        )
        self.list_error = list_error
        self.inspect_error = inspect_error
        self.list_calls: list[tuple[bool, Mapping[str, list[str]]]] = []
        self.inspect_calls: list[str] = []
        self.closed = False

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        self.list_calls.append((all, filters))
        if self.list_error is not None:
            raise self.list_error
        return self.summaries  # type: ignore[return-value]

    def inspect_container(self, container: str) -> object:
        self.inspect_calls.append(container)
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.inspections[container]

    def close(self) -> None:
        self.closed = True


def inventory(client: FakeDockerClient) -> tuple[Any, ...]:
    return DockerManagedContainerInventory(lambda: client).list_managed_containers()


def test_adapter_uses_only_fixed_exact_ownership_filter() -> None:
    client = FakeDockerClient({})
    assert inventory(client) == ()
    assert client.list_calls == [
        (True, {"label": [f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"]})
    ]
    assert client.closed


def test_exactly_labelled_container_is_minimized_to_semantic_record() -> None:
    raw = inspect_data(1, labels=managed_labels(1), health="healthy")
    records = inventory(FakeDockerClient({container_id(1): raw}))
    assert len(records) == 1
    record = records[0]
    assert record.managed_instance_id == instance_id(1)
    assert record.container_id == container_id(1)
    assert record.image_id == IMAGE_ID
    assert record.runtime_state is ContainerRuntimeState.RUNNING
    assert record.health_state is ContainerHealthState.HEALTHY
    serialized = repr(record)
    for marker in (
        "PRIVATE_ENV",
        "PRIVATE-COMMAND",
        "PRIVATE-ENTRYPOINT",
        "PRIVATE-HOST-PATH",
        "PRIVATE-HEALTH-LOG",
        "NetworkSettings",
        "PortBindings",
        "Labels",
    ):
        assert marker not in serialized


@pytest.mark.parametrize(
    "labels",
    [
        None,
        {},
        {LEGACY_LABEL_KEY: "kodi-lab"},
        {MANAGED_LABEL_KEY: "True", INSTANCE_ID_LABEL_KEY: instance_id(1)},
        {MANAGED_LABEL_KEY: "false", INSTANCE_ID_LABEL_KEY: instance_id(1)},
    ],
)
def test_unowned_legacy_and_wrong_value_containers_are_excluded(
    labels: Mapping[str, str] | None,
) -> None:
    raw = inspect_data(1, labels=labels)
    raw["Name"] = "/kodi-lab-lookalike"
    assert inventory(FakeDockerClient({container_id(1): raw})) == ()


@pytest.mark.parametrize("bad_instance_id", [None, "not-a-uuid", "A" * 36])
def test_exactly_managed_container_requires_canonical_instance_uuid(
    bad_instance_id: object,
) -> None:
    labels: dict[str, object] = {MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE}
    if bad_instance_id is not None:
        labels[INSTANCE_ID_LABEL_KEY] = bad_instance_id
    raw = inspect_data(1, labels={})
    config = raw["Config"]
    assert isinstance(config, dict)
    config["Labels"] = labels
    with pytest.raises(InvalidManagedContainerMetadataError):
        inventory(FakeDockerClient({container_id(1): raw}))


def test_duplicate_managed_instance_identity_fails_whole_inventory() -> None:
    duplicate = managed_labels(1)
    client = FakeDockerClient(
        {
            container_id(1): inspect_data(1, labels=duplicate),
            container_id(2): inspect_data(2, labels=duplicate),
        }
    )
    with pytest.raises(InvalidManagedContainerMetadataError):
        inventory(client)


@pytest.mark.parametrize(
    ("docker_state", "expected"),
    [
        ("running", ContainerRuntimeState.RUNNING),
        ("exited", ContainerRuntimeState.STOPPED),
        ("dead", ContainerRuntimeState.STOPPED),
        ("created", ContainerRuntimeState.TRANSITIONAL),
        ("restarting", ContainerRuntimeState.TRANSITIONAL),
        ("paused", ContainerRuntimeState.TRANSITIONAL),
        ("future-state", ContainerRuntimeState.UNKNOWN),
    ],
)
def test_runtime_state_normalization(
    docker_state: str, expected: ContainerRuntimeState
) -> None:
    raw = inspect_data(1, labels=managed_labels(1), state=docker_state)
    assert (
        inventory(FakeDockerClient({container_id(1): raw}))[0].runtime_state is expected
    )


@pytest.mark.parametrize(
    ("docker_health", "expected"),
    [
        ("healthy", ContainerHealthState.HEALTHY),
        ("unhealthy", ContainerHealthState.UNHEALTHY),
        ("starting", ContainerHealthState.STARTING),
        ("future-health", ContainerHealthState.UNKNOWN),
        (None, ContainerHealthState.NOT_CONFIGURED),
    ],
)
def test_health_state_normalization(
    docker_health: str | None, expected: ContainerHealthState
) -> None:
    raw = inspect_data(1, labels=managed_labels(1), health=docker_health)
    assert (
        inventory(FakeDockerClient({container_id(1): raw}))[0].health_state is expected
    )


def test_inventory_is_sorted_by_canonical_instance_uuid() -> None:
    client = FakeDockerClient(
        {
            container_id(2): inspect_data(2, labels=managed_labels(2)),
            container_id(1): inspect_data(1, labels=managed_labels(1)),
        }
    )
    assert [record.managed_instance_id for record in inventory(client)] == [
        instance_id(1),
        instance_id(2),
    ]


def test_fixed_maximum_count_is_accepted() -> None:
    inspections = {
        container_id(number): inspect_data(number, labels=managed_labels(number))
        for number in range(1, MAX_MANAGED_CONTAINERS + 1)
    }
    assert len(inventory(FakeDockerClient(inspections))) == MAX_MANAGED_CONTAINERS


def test_count_above_maximum_fails_before_inspection() -> None:
    summaries = [
        {"Id": container_id(number)} for number in range(1, MAX_MANAGED_CONTAINERS + 2)
    ]
    client = FakeDockerClient({}, summaries=summaries)
    with pytest.raises(InventoryLimitExceededError):
        inventory(client)
    assert client.inspect_calls == []


def test_client_factory_connection_failure_is_daemon_unavailable() -> None:
    def fail() -> FakeDockerClient:
        raise OSError("PRIVATE-DAEMON-DETAIL")

    with pytest.raises(DockerDaemonUnavailableError) as raised:
        DockerManagedContainerInventory(fail).list_managed_containers()
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("stage", ["list", "inspect"])
def test_docker_api_failure_is_safe_inventory_read_error(stage: str) -> None:
    error = APIError("PRIVATE-DOCKER-DETAIL")
    client = FakeDockerClient(
        {container_id(1): inspect_data(1, labels=managed_labels(1))},
        list_error=error if stage == "list" else None,
        inspect_error=error if stage == "inspect" else None,
    )
    with pytest.raises(DockerInventoryReadError) as raised:
        inventory(client)
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize(
    "client",
    [
        FakeDockerClient({}, summaries={"not": "a list"}),
        FakeDockerClient({}, summaries=[{"Id": "short"}]),
        FakeDockerClient(
            {container_id(1): "not an inspect mapping"},
        ),
        FakeDockerClient(
            {
                container_id(1): {
                    **inspect_data(1, labels=managed_labels(1)),
                    "Image": "mutable:tag",
                }
            }
        ),
    ],
)
def test_malformed_docker_responses_fail_closed(client: FakeDockerClient) -> None:
    with pytest.raises(DockerInventoryReadError):
        inventory(client)

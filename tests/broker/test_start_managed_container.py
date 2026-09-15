"""Semantic Docker start and shared two-state reconciliation tests."""

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
    StartManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotReconcilableError,
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    DockerStartError,
    InvalidManagedContainerMetadataError,
    ManagedContainerNotFoundError,
    PostStartVerificationError,
    RuntimeStatePreconditionError,
)


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


def inspect_data(
    container_number: int = 1,
    *,
    instance_number: int = 1,
    managed_value: str = MANAGED_LABEL_VALUE,
    instance_value: object | None = None,
    state: str = "exited",
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
        "Config": {"Labels": labels, "Env": ["PRIVATE=secret"]},
        "State": {"Status": state},
        "Mounts": [{"Source": "/PRIVATE/HOST/PATH"}],
    }


class FakeStartDockerClient:
    def __init__(
        self,
        *,
        summaries: object | None = None,
        inspections: tuple[object | BaseException, ...] = (),
        list_error: BaseException | None = None,
        start_error: BaseException | None = None,
    ) -> None:
        self.summaries = [{"Id": container_id()}] if summaries is None else summaries
        self.inspections = list(inspections)
        self.list_error = list_error
        self.start_error = start_error
        self.events: list[tuple[object, ...]] = []
        self.start_calls: list[str] = []
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
        self.events.append(("start", container))
        self.start_calls.append(container)
        if self.start_error is not None:
            raise self.start_error

    def stop(self, container: str, *, timeout: int) -> None:
        self.stop_calls.append((container, timeout))
        raise AssertionError("start operation attempted stop")

    def close(self) -> None:
        self.closed = True


def start(
    client: FakeStartDockerClient,
    *,
    requested_instance_id: str | None = None,
    expected_container_id: str | None = None,
    expected_state: ContainerRuntimeState = ContainerRuntimeState.STOPPED,
) -> StartManagedContainerRecord:
    return DockerManagedContainerAdapter(lambda: client).start_managed_container(
        managed_instance_id=requested_instance_id or instance_id(),
        expected_container_id=expected_container_id or container_id(),
        expected_runtime_state=expected_state,
    )


def test_stopped_owned_container_starts_exact_id_and_verifies_running() -> None:
    client = FakeStartDockerClient(
        inspections=(inspect_data(state="exited"), inspect_data(state="running"))
    )
    result = start(client)

    assert result == StartManagedContainerRecord(
        managed_instance_id=instance_id(),
        container_id=container_id(),
        runtime_state=ContainerRuntimeState.RUNNING,
        start_performed=True,
    )
    fixed_filters = {
        "label": [
            f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
            f"{INSTANCE_ID_LABEL_KEY}={instance_id()}",
        ]
    }
    assert client.events == [
        ("list", True, fixed_filters),
        ("inspect", container_id()),
        ("start", container_id()),
        ("inspect", container_id()),
    ]
    assert client.start_calls == [container_id()]
    assert client.stop_calls == []
    assert client.closed


@pytest.mark.parametrize("expected_state", list(ContainerRuntimeState))
def test_already_running_converges_without_start_for_any_expected_state(
    expected_state: ContainerRuntimeState,
) -> None:
    client = FakeStartDockerClient(inspections=(inspect_data(state="running"),))
    result = start(client, expected_state=expected_state)
    assert result.runtime_state is ContainerRuntimeState.RUNNING
    assert result.start_performed is False
    assert client.start_calls == []


def test_stopped_container_rejects_stale_expected_running_state() -> None:
    client = FakeStartDockerClient(inspections=(inspect_data(state="exited"),))
    with pytest.raises(RuntimeStatePreconditionError):
        start(client, expected_state=ContainerRuntimeState.RUNNING)
    assert client.start_calls == []


@pytest.mark.parametrize("state", ["restarting", "future-state"])
def test_transitional_and_unknown_states_are_not_reconciled(state: str) -> None:
    client = FakeStartDockerClient(inspections=(inspect_data(state=state),))
    with pytest.raises(ContainerStateNotReconcilableError):
        start(client, expected_state=ContainerRuntimeState.UNKNOWN)
    assert client.start_calls == []


def test_no_match_and_duplicate_match_never_start() -> None:
    missing = FakeStartDockerClient(summaries=[], inspections=())
    with pytest.raises(ManagedContainerNotFoundError):
        start(missing)
    duplicate = FakeStartDockerClient(
        summaries=[{"Id": container_id()}, {"Id": container_id(2)}], inspections=()
    )
    with pytest.raises(AmbiguousManagedContainerError):
        start(duplicate)
    assert missing.start_calls == duplicate.start_calls == []


@pytest.mark.parametrize(
    "inspection",
    [
        inspect_data(labels_present=False),
        inspect_data(managed_value="false"),
        inspect_data(instance_number=2),
        inspect_data(instance_value="not-a-canonical-uuid"),
    ],
)
def test_fresh_ownership_revalidation_blocks_start(
    inspection: dict[str, object],
) -> None:
    client = FakeStartDockerClient(inspections=(inspection,))
    with pytest.raises(InvalidManagedContainerMetadataError):
        start(client)
    assert client.start_calls == []


def test_identity_mismatch_and_replacement_never_start() -> None:
    mismatch = FakeStartDockerClient(inspections=(inspect_data(),))
    with pytest.raises(ContainerIdentityPreconditionError):
        start(mismatch, expected_container_id=container_id(2))
    replacement = FakeStartDockerClient(
        summaries=[{"Id": container_id(2)}],
        inspections=(inspect_data(2, instance_number=1),),
    )
    with pytest.raises(ContainerIdentityPreconditionError):
        start(replacement, expected_container_id=container_id())
    assert mismatch.start_calls == replacement.start_calls == []


def test_unlabelled_legacy_id_cannot_be_used_as_start_target() -> None:
    legacy_id = container_id(99)
    client = FakeStartDockerClient(
        summaries=[{"Id": legacy_id}],
        inspections=(inspect_data(99, labels_present=False),),
    )
    with pytest.raises(InvalidManagedContainerMetadataError):
        start(client, expected_container_id=legacy_id)
    assert client.start_calls == []


@pytest.mark.parametrize(
    "final_inspection",
    [
        inspect_data(state="exited"),
        APIError("PRIVATE-CONTAINER-DISAPPEARED"),
        inspect_data(state="running", managed_value="false"),
        inspect_data(state="running", instance_number=2),
        inspect_data(2, state="running"),
    ],
)
def test_post_start_state_disappearance_ownership_or_identity_failure_is_safe(
    final_inspection: object | BaseException,
) -> None:
    client = FakeStartDockerClient(
        inspections=(inspect_data(state="exited"), final_inspection)
    )
    with pytest.raises(PostStartVerificationError) as raised:
        start(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.start_calls == [container_id()]


def test_daemon_read_and_start_failures_are_sanitized() -> None:
    def unavailable() -> FakeStartDockerClient:
        raise OSError("PRIVATE-DAEMON-DETAIL")

    with pytest.raises(DockerDaemonUnavailableError) as raised:
        DockerManagedContainerAdapter(unavailable).start_managed_container(
            managed_instance_id=instance_id(),
            expected_container_id=container_id(),
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )
    assert "PRIVATE" not in str(raised.value)

    for client in (
        FakeStartDockerClient(list_error=APIError("PRIVATE-LIST")),
        FakeStartDockerClient(inspections=(APIError("PRIVATE-INSPECT"),)),
    ):
        with pytest.raises(DockerInventoryReadError) as read_error:
            start(client)
        assert "PRIVATE" not in str(read_error.value)
        assert client.start_calls == []

    failed = FakeStartDockerClient(
        inspections=(inspect_data(),),
        start_error=APIError("PRIVATE-START"),
    )
    with pytest.raises(DockerStartError) as start_error:
        start(failed)
    assert "PRIVATE" not in str(start_error.value)
    assert failed.start_calls == [container_id()]


class StatefulDockerClient:
    def __init__(self) -> None:
        self.state = "exited"
        self.start_calls = 0
        self.stop_calls: list[int] = []

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        return [{"Id": container_id()}]

    def inspect_container(self, container: str) -> object:
        return inspect_data(state=self.state)

    def start(self, container: str) -> None:
        self.start_calls += 1
        self.state = "running"

    def stop(self, container: str, *, timeout: int) -> None:
        self.stop_calls.append(timeout)
        self.state = "exited"

    def close(self) -> None:
        pass


def test_shared_reconciliation_supports_only_stopped_running_round_trip() -> None:
    client = StatefulDockerClient()
    adapter = DockerManagedContainerAdapter(lambda: client)
    started = adapter.start_managed_container(
        managed_instance_id=instance_id(),
        expected_container_id=container_id(),
        expected_runtime_state=ContainerRuntimeState.STOPPED,
    )
    stopped = adapter.stop_managed_container(
        managed_instance_id=instance_id(),
        expected_container_id=container_id(),
        expected_runtime_state=ContainerRuntimeState.RUNNING,
    )
    assert started.start_performed is True
    assert stopped.stop_performed is True
    assert client.start_calls == 1
    assert client.stop_calls == [DOCKER_STOP_TIMEOUT_SECONDS]

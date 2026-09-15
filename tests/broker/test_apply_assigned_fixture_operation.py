import json
import os
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    AppliedAssignedFixtureRecord,
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AssignedFixtureIncompatibleError,
    AssignedFixtureMissingError,
    BrokerAuthorizationError,
    DockerFixtureArchiveError,
    FixtureApplicationClientError,
    FixtureBaselineConflictError,
    FixturePrerequisiteClientError,
    PackagedFixtureUnavailableError,
    PersistentProvisioningUnsupportedError,
    RemoteBrokerError,
)
from kodi_lab.broker.fixtures import AssignedFixtureSpec
from kodi_lab.broker.operations import (
    ApplyAssignedFixtureResult,
    OperationRegistry,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def revision_id(number: int = 2) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


def fixture_spec() -> AssignedFixtureSpec:
    return AssignedFixtureSpec(
        managed_instance_id=instance_id(),
        runtime_revision_id=revision_id(),
        fixture_id="35d6854d-6cfc-5247-97df-7f2023c6ae9d",
        fixture_slug="small-mixed-v1",
        fixture_version=1,
        content_digest="sha256:" + ("a" * 64),
        destination="/home/kodi/.kodi",
        archive=b"trusted",
        members=(),
    )


class FakeResolver:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def resolve(self, managed_instance_id: str) -> AssignedFixtureSpec:
        self.calls.append(managed_instance_id)
        if self.error is not None:
            raise self.error
        return fixture_spec()


class FakeProvider:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[AssignedFixtureSpec, str]] = []
        self.inventory_calls = 0

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        self.inventory_calls += 1
        return ()

    def start_managed_container(self, **_kwargs: object) -> object:
        raise AssertionError("fixture operation reached start")

    def stop_managed_container(self, **_kwargs: object) -> object:
        raise AssertionError("fixture operation reached stop")

    def apply_assigned_fixture(
        self, *, spec: AssignedFixtureSpec, expected_container_id: str
    ) -> AppliedAssignedFixtureRecord:
        self.calls.append((spec, expected_container_id))
        if self.error is not None:
            raise self.error
        return AppliedAssignedFixtureRecord(
            managed_instance_id=spec.managed_instance_id,
            runtime_revision_id=spec.runtime_revision_id,
            container_id=expected_container_id,
            fixture_slug="small-mixed-v1",
            fixture_version=1,
            content_digest=spec.content_digest,
            runtime_state=ContainerRuntimeState.STOPPED,
        )


def registry(provider: FakeProvider, resolver: FakeResolver) -> OperationRegistry:
    return build_foundation_registry(
        provider,  # type: ignore[arg-type]
        assigned_fixture_resolver=resolver,
        assigned_fixture_provider=provider,
    )


def call(client: BrokerClient) -> ApplyAssignedFixtureResult:
    return client.apply_assigned_fixture(
        managed_instance_id=instance_id(), expected_container_id=container_id()
    )


def raw_round_trip(path: Path, params: object) -> dict[str, object]:
    payload = json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "apply_assigned_fixture",
            "params": params,
        }
    ).encode()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(os.fspath(path))
        send_frame(connection, payload)
        connection.shutdown(socket.SHUT_WR)
        return decode_json_object(receive_frame(connection))
    finally:
        connection.close()


def test_authorized_fixture_operation_has_only_identity_preconditions(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeProvider()
    resolver = FakeResolver()
    with run_broker(socket_config, registry=registry(provider, resolver)):
        result = call(BrokerClient(socket_config.socket_path))
    assert resolver.calls == [instance_id()]
    assert provider.calls == [(fixture_spec(), container_id())]
    assert result == ApplyAssignedFixtureResult(
        managed_instance_id=instance_id(),
        runtime_revision_id=revision_id(),
        container_id=container_id(),
        fixture_slug="small-mixed-v1",
        fixture_version=1,
        content_digest="sha256:" + ("a" * 64),
        runtime_state=ContainerRuntimeState.STOPPED,
    )
    assert set(result.model_dump()) == {
        "managed_instance_id",
        "runtime_revision_id",
        "container_id",
        "fixture_slug",
        "fixture_version",
        "content_digest",
        "runtime_state",
    }


def test_unauthorized_fixture_operation_performs_no_resolution_or_docker_work(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeProvider()
    resolver = FakeResolver()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=registry(provider, resolver),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        call(BrokerClient(socket_config.socket_path))
    assert resolver.calls == []
    assert provider.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"fixture_id": "small-mixed-v1"},
        {"path": "/tmp/fixture"},
        {"destination": "/home/kodi"},
        {"archive": "bytes"},
        {"content": "data"},
        {"command": ["chown"]},
    ],
)
def test_caller_fixture_content_path_destination_and_command_are_unrepresentable(
    socket_config: SocketPathConfig,
    run_broker: Any,
    extra: dict[str, object],
) -> None:
    provider = FakeProvider()
    resolver = FakeResolver()
    params = {
        "managed_instance_id": instance_id(),
        "expected_container_id": container_id(),
        **extra,
    }
    with run_broker(socket_config, registry=registry(provider, resolver)):
        response = raw_round_trip(socket_config.socket_path, params)
    assert response["error"]["code"] == "invalid_params"  # type: ignore[index]
    assert resolver.calls == []
    assert provider.calls == []


@pytest.mark.parametrize(
    ("error", "expected_type", "code"),
    [
        (
            PersistentProvisioningUnsupportedError("PRIVATE"),
            FixturePrerequisiteClientError,
            "persistent_fixture_unsupported",
        ),
        (
            AssignedFixtureMissingError("PRIVATE"),
            FixturePrerequisiteClientError,
            "assigned_fixture_missing",
        ),
        (
            AssignedFixtureIncompatibleError("PRIVATE"),
            FixturePrerequisiteClientError,
            "assigned_fixture_incompatible",
        ),
        (
            PackagedFixtureUnavailableError("PRIVATE"),
            FixturePrerequisiteClientError,
            "packaged_fixture_unavailable",
        ),
        (
            FixtureBaselineConflictError("PRIVATE"),
            FixtureApplicationClientError,
            "fixture_baseline_conflict",
        ),
        (
            DockerFixtureArchiveError("PRIVATE"),
            FixtureApplicationClientError,
            "fixture_archive_failed",
        ),
    ],
)
def test_fixture_errors_are_typed_and_sanitized(
    socket_config: SocketPathConfig,
    run_broker: Any,
    error: BaseException,
    expected_type: type[RemoteBrokerError],
    code: str,
) -> None:
    provider = FakeProvider(error=error)
    resolver = FakeResolver(
        error=error
        if isinstance(
            error,
            (
                PersistentProvisioningUnsupportedError,
                AssignedFixtureMissingError,
                AssignedFixtureIncompatibleError,
                PackagedFixtureUnavailableError,
            ),
        )
        else None
    )
    if resolver.error is not None:
        provider.error = None
    with (
        run_broker(socket_config, registry=registry(provider, resolver)),
        pytest.raises(expected_type) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)

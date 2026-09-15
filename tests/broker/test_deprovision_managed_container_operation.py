"""Broker/client boundary tests for destructive managed-container deprovision."""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    DeprovisionedManagedContainerRecord,
    ManagedContainerRecord,
    StartManagedContainerRecord,
    StopManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerClientError,
    AmbiguousManagedContainerError,
    BrokerAuthorizationError,
    ClientProtocolError,
    ContainerIdentityPreconditionClientError,
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedClientError,
    ContainerMustBeStoppedError,
    DockerDaemonUnavailableClientError,
    DockerDaemonUnavailableError,
    DockerInventoryClientError,
    DockerInventoryReadError,
    DockerRemoveClientError,
    DockerRemoveError,
    InvalidManagedContainerMetadataError,
    ManagedContainerMetadataClientError,
    ManagedContainerNotFoundClientError,
    ManagedContainerNotFoundError,
    PostDeprovisionVerificationClientError,
    PostDeprovisionVerificationError,
    ProvisioningPrerequisiteClientError,
    ProvisioningStateError,
    RemoteBrokerError,
    RuntimeRevisionPreconditionClientError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    OperationRegistry,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.server import AUDIT_LOGGER_NAME
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def revision_id(number: int = 2) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


class FakeRevisionResolver:
    def __init__(
        self,
        *,
        resolved: str = revision_id(),
        error: BaseException | None = None,
    ) -> None:
        self.resolved = resolved
        self.error = error
        self.calls: list[str] = []

    def resolve_runtime_revision_id(self, managed_instance_id: str) -> str:
        self.calls.append(managed_instance_id)
        if self.error is not None:
            raise self.error
        return self.resolved


class FakeContainerProvider:
    def __init__(
        self,
        *,
        outcome: DeprovisionedManagedContainerRecord | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.outcome = outcome or DeprovisionedManagedContainerRecord(
            managed_instance_id=instance_id(),
            removed_container_id=container_id(),
            runtime_revision_id=revision_id(),
            removed=True,
        )
        self.error = error
        self.deprovision_calls: list[tuple[object, ...]] = []
        self.inventory_calls = 0

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        self.inventory_calls += 1
        return ()

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> DeprovisionedManagedContainerRecord:
        self.deprovision_calls.append(
            (
                managed_instance_id,
                expected_container_id,
                expected_runtime_revision_id,
                expected_runtime_state,
            )
        )
        if self.error is not None:
            raise self.error
        return self.outcome

    def start_managed_container(self, **_kwargs: object) -> StartManagedContainerRecord:
        raise AssertionError("deprovision reached start")

    def stop_managed_container(self, **_kwargs: object) -> StopManagedContainerRecord:
        raise AssertionError("deprovision reached stop")


def registry(
    provider: FakeContainerProvider,
    resolver: FakeRevisionResolver,
) -> OperationRegistry:
    return build_foundation_registry(
        provider,
        deprovisioning_provider=provider,
        revision_resolver=resolver,
    )


def valid_params() -> dict[str, object]:
    return {
        "managed_instance_id": instance_id(),
        "expected_container_id": container_id(),
        "expected_runtime_revision_id": revision_id(),
        "expected_runtime_state": "stopped",
    }


def raw_request(params: object) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "deprovision_managed_container",
            "params": params,
        }
    ).encode()


def raw_round_trip(path: Path, payload: bytes) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(os.fspath(path))
        send_frame(connection, payload)
        connection.shutdown(socket.SHUT_WR)
        return decode_json_object(receive_frame(connection))
    finally:
        connection.close()


def call(client: BrokerClient) -> DeprovisionManagedContainerResult:
    return client.deprovision_managed_container(
        managed_instance_id=instance_id(),
        expected_container_id=container_id(),
        expected_runtime_revision_id=revision_id(),
        expected_runtime_state=ContainerRuntimeState.STOPPED,
    )


def test_authorized_client_receives_typed_deprovision_result(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    resolver = FakeRevisionResolver()
    with run_broker(socket_config, registry=registry(provider, resolver)):
        result = call(BrokerClient(socket_config.socket_path))

    assert result == DeprovisionManagedContainerResult(
        managed_instance_id=instance_id(),
        removed_container_id=container_id(),
        runtime_revision_id=revision_id(),
        removed=True,
    )
    assert resolver.calls == [instance_id()]
    assert provider.deprovision_calls == [
        (
            instance_id(),
            container_id(),
            revision_id(),
            ContainerRuntimeState.STOPPED,
        )
    ]
    assert set(result.model_dump(mode="json")) == {
        "managed_instance_id",
        "removed_container_id",
        "runtime_revision_id",
        "removed",
    }


def test_unauthorized_peer_performs_zero_resolver_or_docker_work(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    resolver = FakeRevisionResolver()
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
    assert provider.deprovision_calls == []
    assert provider.inventory_calls == 0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {**valid_params(), "force": True},
        {**valid_params(), "remove_volumes": True},
        {**valid_params(), "expected_runtime_state": "running"},
        {**valid_params(), "managed_instance_id": "not-a-uuid"},
        {**valid_params(), "expected_container_id": "A" * 64},
        {**valid_params(), "expected_runtime_revision_id": "NOT-A-UUID"},
    ],
)
def test_invalid_schema_performs_zero_resolver_or_docker_work(
    socket_config: SocketPathConfig,
    run_broker: Any,
    params: dict[str, object],
) -> None:
    provider = FakeContainerProvider()
    resolver = FakeRevisionResolver()
    with run_broker(socket_config, registry=registry(provider, resolver)):
        response = raw_round_trip(socket_config.socket_path, raw_request(params))
    assert response["error"]["code"] == "invalid_params"  # type: ignore[index]
    assert resolver.calls == []
    assert provider.deprovision_calls == []


def test_client_rejects_nonstopped_request_before_connect(tmp_path: Path) -> None:
    with pytest.raises(ClientProtocolError, match="local validation"):
        BrokerClient(tmp_path / "never.sock").deprovision_managed_container(
            managed_instance_id=instance_id(),
            expected_container_id=container_id(),
            expected_runtime_revision_id=revision_id(),
            expected_runtime_state=ContainerRuntimeState.RUNNING,  # type: ignore[arg-type]
        )


def test_persisted_revision_mismatch_blocks_docker_operation(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    resolver = FakeRevisionResolver(resolved=revision_id(99))
    with (
        run_broker(socket_config, registry=registry(provider, resolver)),
        pytest.raises(RuntimeRevisionPreconditionClientError) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == "runtime_revision_precondition_failed"
    assert provider.deprovision_calls == []


@pytest.mark.parametrize(
    ("broker_error", "client_error", "code"),
    [
        (
            ProvisioningStateError("PRIVATE"),
            ProvisioningPrerequisiteClientError,
            "managed_instance_state_unavailable",
        ),
        (
            DockerDaemonUnavailableError("PRIVATE"),
            DockerDaemonUnavailableClientError,
            "docker_daemon_unavailable",
        ),
        (
            ManagedContainerNotFoundError("PRIVATE"),
            ManagedContainerNotFoundClientError,
            "managed_container_not_found",
        ),
        (
            AmbiguousManagedContainerError("PRIVATE"),
            AmbiguousManagedContainerClientError,
            "ambiguous_managed_container",
        ),
        (
            InvalidManagedContainerMetadataError("PRIVATE"),
            ManagedContainerMetadataClientError,
            "invalid_managed_container_metadata",
        ),
        (
            ContainerIdentityPreconditionError("PRIVATE"),
            ContainerIdentityPreconditionClientError,
            "container_identity_precondition_failed",
        ),
        (
            RuntimeRevisionPreconditionError("PRIVATE"),
            RuntimeRevisionPreconditionClientError,
            "runtime_revision_precondition_failed",
        ),
        (
            ContainerMustBeStoppedError("PRIVATE"),
            ContainerMustBeStoppedClientError,
            "container_must_be_stopped",
        ),
        (
            DockerRemoveError("PRIVATE"),
            DockerRemoveClientError,
            "docker_remove_failed",
        ),
        (
            PostDeprovisionVerificationError("PRIVATE"),
            PostDeprovisionVerificationClientError,
            "post_deprovision_verification_failed",
        ),
        (
            DockerInventoryReadError("PRIVATE"),
            DockerInventoryClientError,
            "docker_inventory_failed",
        ),
    ],
)
def test_deprovision_errors_are_stable_typed_and_sanitized(
    socket_config: SocketPathConfig,
    run_broker: Any,
    broker_error: BaseException,
    client_error: type[RemoteBrokerError],
    code: str,
) -> None:
    provider = FakeContainerProvider(error=broker_error)
    resolver = FakeRevisionResolver(
        error=broker_error if isinstance(broker_error, ProvisioningStateError) else None
    )
    if isinstance(broker_error, ProvisioningStateError):
        provider.error = None
    with (
        run_broker(socket_config, registry=registry(provider, resolver)),
        pytest.raises(client_error) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)


def test_unexpected_handler_failure_is_sanitized(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider(error=RuntimeError("PRIVATE HANDLER"))
    with (
        run_broker(
            socket_config,
            registry=registry(provider, FakeRevisionResolver()),
        ),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == "internal_error"
    assert "PRIVATE" not in str(raised.value)


def test_inconsistent_adapter_result_is_sanitized(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider(
        outcome=DeprovisionedManagedContainerRecord(
            managed_instance_id=instance_id(9),
            removed_container_id=container_id(9),
            runtime_revision_id=revision_id(9),
            removed=True,
        )
    )
    with (
        run_broker(
            socket_config,
            registry=registry(provider, FakeRevisionResolver()),
        ),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == "internal_error"


def completed_audits(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        event
        for event in caplog.records
        if event.name == AUDIT_LOGGER_NAME
        and event.getMessage() == "broker request completed"
    ]


def test_success_audit_is_bounded_and_excludes_container_identity(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    with run_broker(
        socket_config,
        registry=registry(FakeContainerProvider(), FakeRevisionResolver()),
    ):
        call(BrokerClient(socket_config.socket_path))

    event = completed_audits(caplog)[0]
    assert event.broker_managed_instance_id == instance_id()  # type: ignore[attr-defined]
    assert event.broker_runtime_revision_id == revision_id()  # type: ignore[attr-defined]
    assert event.broker_container_removed is True  # type: ignore[attr-defined]
    assert event.broker_result_code == "success"  # type: ignore[attr-defined]
    assert not hasattr(event, "broker_container_id")
    assert container_id() not in event.getMessage()

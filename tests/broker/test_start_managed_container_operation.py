"""Broker/client boundary tests for semantic managed-container start."""

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
    ManagedContainerRecord,
    StartManagedContainerRecord,
    StopManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    ClientProtocolError,
    ContainerStateNotReconcilableClientError,
    ContainerStateNotReconcilableError,
    DockerStartClientError,
    DockerStartError,
    PostStartVerificationClientError,
    PostStartVerificationError,
    RemoteBrokerError,
    RuntimeRevisionPreconditionClientError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.operations import (
    OperationRegistry,
    StartManagedContainerResult,
)
from kodi_lab.broker.operations import (
    build_foundation_registry as _build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.server import AUDIT_LOGGER_NAME
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


def running_outcome(*, performed: bool = True) -> StartManagedContainerRecord:
    return StartManagedContainerRecord(
        managed_instance_id=instance_id(),
        container_id=container_id(),
        runtime_state=ContainerRuntimeState.RUNNING,
        start_performed=performed,
    )


class FakeContainerProvider:
    def __init__(
        self,
        *,
        outcome: StartManagedContainerRecord | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.outcome = outcome or running_outcome()
        self.error = error
        self.start_calls: list[tuple[str, str, ContainerRuntimeState]] = []
        self.stop_calls = 0
        self.inventory_calls = 0

    def list_managed_containers(
        self, *, managed_instance_id: str | None = None
    ) -> tuple[ManagedContainerRecord, ...]:
        self.inventory_calls += 1
        return ()

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
        expected_runtime_revision_id: str,
    ) -> StartManagedContainerRecord:
        assert expected_runtime_revision_id == instance_id(101)
        self.start_calls.append(
            (managed_instance_id, expected_container_id, expected_runtime_state)
        )
        if self.error is not None:
            raise self.error
        return self.outcome

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
        expected_runtime_revision_id: str,
    ) -> StopManagedContainerRecord:
        self.stop_calls += 1
        raise AssertionError("start operation reached stop mutation")


class FakeRevisionResolver:
    def resolve_runtime_revision_id(self, _managed_instance_id: str) -> str:
        return instance_id(101)


def build_foundation_registry(provider: FakeContainerProvider) -> OperationRegistry:
    return _build_foundation_registry(
        provider, revision_resolver=FakeRevisionResolver()
    )


def valid_params() -> dict[str, object]:
    return {
        "managed_instance_id": instance_id(),
        "expected_container_id": container_id(),
        "expected_runtime_state": "stopped",
    }


def raw_start_request(params: object) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "start_managed_container",
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


def call(client: BrokerClient) -> StartManagedContainerResult:
    return client.start_managed_container(
        managed_instance_id=instance_id(),
        expected_container_id=container_id(),
        expected_runtime_state=ContainerRuntimeState.STOPPED,
    )


def test_authorized_client_receives_typed_start_result(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    with run_broker(socket_config, registry=build_foundation_registry(provider)):
        result = call(BrokerClient(socket_config.socket_path))

    assert result == StartManagedContainerResult(
        managed_instance_id=instance_id(),
        container_id=container_id(),
        runtime_state=ContainerRuntimeState.RUNNING,
        start_performed=True,
    )
    assert provider.start_calls == [
        (instance_id(), container_id(), ContainerRuntimeState.STOPPED)
    ]
    assert set(result.model_dump(mode="json")) == {
        "managed_instance_id",
        "container_id",
        "runtime_state",
        "start_performed",
    }


def test_unauthorized_start_performs_zero_docker_work(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=build_foundation_registry(provider),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        call(BrokerClient(socket_config.socket_path))
    assert provider.start_calls == []
    assert provider.inventory_calls == provider.stop_calls == 0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"expected_container_id": container_id()},
        {**valid_params(), "restart": True},
        {**valid_params(), "timeout": 99},
        {**valid_params(), "managed_instance_id": "not-a-uuid"},
        {**valid_params(), "expected_container_id": "A" * 64},
        {**valid_params(), "expected_runtime_state": "paused"},
    ],
)
def test_invalid_start_schema_performs_zero_docker_work(
    socket_config: SocketPathConfig,
    run_broker: Any,
    params: dict[str, object],
) -> None:
    provider = FakeContainerProvider()
    with run_broker(socket_config, registry=build_foundation_registry(provider)):
        response = raw_round_trip(socket_config.socket_path, raw_start_request(params))
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_params"
    assert provider.start_calls == []
    assert provider.inventory_calls == provider.stop_calls == 0


def test_client_rejects_invalid_start_before_connect(tmp_path: Path) -> None:
    with pytest.raises(ClientProtocolError, match="local validation"):
        BrokerClient(tmp_path / "never-used.sock").start_managed_container(
            managed_instance_id="not-a-uuid",
            expected_container_id=container_id(),
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )


@pytest.mark.parametrize(
    ("adapter_error", "client_error", "code"),
    [
        (
            ContainerStateNotReconcilableError("PRIVATE"),
            ContainerStateNotReconcilableClientError,
            "container_state_not_reconcilable",
        ),
        (
            DockerStartError("PRIVATE"),
            DockerStartClientError,
            "docker_start_failed",
        ),
        (
            PostStartVerificationError("PRIVATE"),
            PostStartVerificationClientError,
            "post_start_verification_failed",
        ),
        (
            RuntimeRevisionPreconditionError("PRIVATE"),
            RuntimeRevisionPreconditionClientError,
            "runtime_revision_precondition_failed",
        ),
    ],
)
def test_start_failures_become_sanitized_typed_client_errors(
    socket_config: SocketPathConfig,
    run_broker: Any,
    adapter_error: BaseException,
    client_error: type[RemoteBrokerError],
    code: str,
) -> None:
    provider = FakeContainerProvider(error=adapter_error)
    with (
        run_broker(socket_config, registry=build_foundation_registry(provider)),
        pytest.raises(client_error) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)


def test_inconsistent_or_exceptional_start_handler_is_internal_error(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    providers = [
        FakeContainerProvider(
            outcome=StartManagedContainerRecord(
                managed_instance_id=instance_id(2),
                container_id=container_id(2),
                runtime_state=ContainerRuntimeState.RUNNING,
                start_performed=True,
            )
        ),
        FakeContainerProvider(error=RuntimeError("PRIVATE-HANDLER")),
    ]
    for provider in providers:
        with (
            run_broker(socket_config, registry=build_foundation_registry(provider)),
            pytest.raises(RemoteBrokerError) as raised,
        ):
            call(BrokerClient(socket_config.socket_path))
        assert raised.value.code == "internal_error"
        assert "PRIVATE" not in str(raised.value)


def test_success_audit_is_bounded_semantic_start_metadata(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    provider = FakeContainerProvider()
    with run_broker(socket_config, registry=build_foundation_registry(provider)):
        call(BrokerClient(socket_config.socket_path))

    events = [
        event
        for event in caplog.records
        if event.name == AUDIT_LOGGER_NAME
        and event.getMessage() == "broker request completed"
    ]
    event = events[0]
    assert event.broker_managed_instance_id == instance_id()  # type: ignore[attr-defined]
    assert event.broker_start_performed is True  # type: ignore[attr-defined]
    assert event.broker_resulting_runtime_state == "running"  # type: ignore[attr-defined]
    assert event.broker_result_code == "success"  # type: ignore[attr-defined]
    assert container_id() not in event.getMessage()
    assert not hasattr(event, "broker_container_id")

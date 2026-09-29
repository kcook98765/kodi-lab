"""Broker/client boundary tests for semantic managed-container stop."""

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
    AmbiguousManagedContainerClientError,
    AmbiguousManagedContainerError,
    BrokerAuthorizationError,
    ClientProtocolError,
    ContainerIdentityPreconditionClientError,
    ContainerIdentityPreconditionError,
    ContainerStateNotStoppableClientError,
    ContainerStateNotStoppableError,
    DockerDaemonUnavailableClientError,
    DockerDaemonUnavailableError,
    DockerInventoryClientError,
    DockerInventoryReadError,
    DockerStopClientError,
    DockerStopError,
    InvalidManagedContainerMetadataError,
    ManagedContainerMetadataClientError,
    ManagedContainerNotFoundClientError,
    ManagedContainerNotFoundError,
    PostStopVerificationClientError,
    PostStopVerificationError,
    RemoteBrokerError,
    RuntimeStatePreconditionClientError,
    RuntimeStatePreconditionError,
)
from kodi_lab.broker.operations import (
    StopManagedContainerResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.server import AUDIT_LOGGER_NAME
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame


def instance_id(number: int = 1) -> str:
    return str(UUID(int=number))


def container_id(number: int = 1) -> str:
    return f"{number:064x}"


def stopped_outcome(*, performed: bool = True) -> StopManagedContainerRecord:
    return StopManagedContainerRecord(
        managed_instance_id=instance_id(),
        container_id=container_id(),
        runtime_state=ContainerRuntimeState.STOPPED,
        stop_performed=performed,
    )


class FakeContainerProvider:
    def __init__(
        self,
        *,
        outcome: StopManagedContainerRecord | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.outcome = outcome or stopped_outcome()
        self.error = error
        self.stop_calls: list[tuple[str, str, ContainerRuntimeState]] = []
        self.inventory_calls = 0

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        self.inventory_calls += 1
        return ()

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerRecord:
        self.stop_calls.append(
            (managed_instance_id, expected_container_id, expected_runtime_state)
        )
        if self.error is not None:
            raise self.error
        return self.outcome

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerRecord:
        raise AssertionError("stop operation reached start mutation")


def raw_stop_request(params: object) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "stop_managed_container",
            "params": params,
        }
    ).encode()


def valid_params() -> dict[str, object]:
    return {
        "managed_instance_id": instance_id(),
        "expected_container_id": container_id(),
        "expected_runtime_state": "running",
    }


def raw_round_trip(path: Path, payload: bytes) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(os.fspath(path))
        send_frame(connection, payload)
        connection.shutdown(socket.SHUT_WR)
        return decode_json_object(receive_frame(connection))
    finally:
        connection.close()


def call(client: BrokerClient) -> StopManagedContainerResult:
    return client.stop_managed_container(
        managed_instance_id=instance_id(),
        expected_container_id=container_id(),
        expected_runtime_state=ContainerRuntimeState.RUNNING,
    )


def test_authorized_client_receives_typed_stop_result(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        result = call(BrokerClient(socket_config.socket_path))

    assert result == StopManagedContainerResult(
        managed_instance_id=instance_id(),
        container_id=container_id(),
        runtime_state=ContainerRuntimeState.STOPPED,
        stop_performed=True,
    )
    assert provider.stop_calls == [
        (instance_id(), container_id(), ContainerRuntimeState.RUNNING)
    ]
    assert set(result.model_dump(mode="json")) == {
        "managed_instance_id",
        "container_id",
        "runtime_state",
        "stop_performed",
    }


def test_unauthorized_peer_performs_zero_docker_operations(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider()
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, expected_uid=os.getuid() + 10_000, registry=registry),
        pytest.raises(BrokerAuthorizationError),
    ):
        call(BrokerClient(socket_config.socket_path))
    assert provider.stop_calls == []
    assert provider.inventory_calls == 0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"expected_container_id": container_id()},
        {**valid_params(), "signal": "SIGKILL"},
        {**valid_params(), "timeout": 999},
        {**valid_params(), "managed_instance_id": "not-a-uuid"},
        {**valid_params(), "expected_container_id": "A" * 64},
        {**valid_params(), "expected_runtime_state": "paused"},
    ],
)
def test_strict_invalid_requests_perform_zero_docker_operations(
    socket_config: SocketPathConfig,
    run_broker: Any,
    params: dict[str, object],
) -> None:
    provider = FakeContainerProvider()
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        response = raw_round_trip(
            socket_config.socket_path,
            raw_stop_request(params),
        )
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_params"
    assert provider.stop_calls == []
    assert provider.inventory_calls == 0


def test_client_rejects_invalid_stop_locally_before_connect(tmp_path: Path) -> None:
    with pytest.raises(ClientProtocolError, match="local validation"):
        BrokerClient(tmp_path / "never-used.sock").stop_managed_container(
            managed_instance_id="not-a-uuid",
            expected_container_id=container_id(),
            expected_runtime_state=ContainerRuntimeState.RUNNING,
        )


def test_broker_sanitizes_adapter_result_for_different_identity(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider(
        outcome=StopManagedContainerRecord(
            managed_instance_id=instance_id(2),
            container_id=container_id(2),
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )
    )
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, registry=registry),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == "internal_error"


def test_capabilities_contains_exact_bounded_operation_set() -> None:
    provider = FakeContainerProvider()
    registry = build_foundation_registry(provider)
    assert registry.names == (
        "active_reviewed_runtime_installation",
        "apply_addon_zip_installation",
        "apply_assigned_fixture",
        "apply_managed_instance_snapshot",
        "apply_repository_replacement",
        "broker_info",
        "capabilities",
        "capture_managed_instance_snapshot",
        "capture_managed_kodi_screenshot",
        "capture_promotion_kodi_log_cursor",
        "copy_sync_attached_development_source",
        "copy_sync_mcp_workspace",
        "copy_sync_promoted_development_source",
        "delete_managed_snapshot_artifact",
        "deprovision_managed_container",
        "inspect_repository_replacement",
        "launch_reviewed_runtime_installation",
        "managed_container_inventory",
        "observe_addon_zip_destination",
        "observe_managed_kodi_runtime",
        "observe_reviewed_runtime_installation",
        "ping",
        "provision_managed_container",
        "read_promotion_kodi_log_window",
        "reviewed_runtime_image_present",
        "start_managed_container",
        "stop_managed_container",
        "verify_addon_zip_enablement_destination",
        "verify_addon_zip_smoke_destination",
        "verify_attached_development_source_destination",
        "verify_mcp_workspace_destination",
    )


@pytest.mark.parametrize(
    ("adapter_error", "client_error", "code"),
    [
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
            RuntimeStatePreconditionError("PRIVATE"),
            RuntimeStatePreconditionClientError,
            "runtime_state_precondition_failed",
        ),
        (
            ContainerStateNotStoppableError("PRIVATE"),
            ContainerStateNotStoppableClientError,
            "container_state_not_stoppable",
        ),
        (
            DockerStopError("PRIVATE"),
            DockerStopClientError,
            "docker_stop_failed",
        ),
        (
            PostStopVerificationError("PRIVATE"),
            PostStopVerificationClientError,
            "post_stop_verification_failed",
        ),
        (
            DockerInventoryReadError("PRIVATE"),
            DockerInventoryClientError,
            "docker_inventory_failed",
        ),
    ],
)
def test_stop_failures_become_stable_typed_client_errors(
    socket_config: SocketPathConfig,
    run_broker: Any,
    adapter_error: BaseException,
    client_error: type[RemoteBrokerError],
    code: str,
) -> None:
    provider = FakeContainerProvider(error=adapter_error)
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, registry=registry),
        pytest.raises(client_error) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)


def test_unexpected_handler_failure_remains_sanitized(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeContainerProvider(error=RuntimeError("PRIVATE-HANDLER-DETAIL"))
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, registry=registry),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        call(BrokerClient(socket_config.socket_path))
    assert raised.value.code == "internal_error"
    assert "PRIVATE" not in str(raised.value)


def completed_audits(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    return [
        event
        for event in caplog.records
        if event.name == AUDIT_LOGGER_NAME
        and event.getMessage() == "broker request completed"
    ]


def test_success_audit_has_only_semantic_stop_fields(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    provider = FakeContainerProvider()
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        call(BrokerClient(socket_config.socket_path))

    event = completed_audits(caplog)[0]
    assert event.broker_managed_instance_id == instance_id()  # type: ignore[attr-defined]
    assert event.broker_stop_performed is True  # type: ignore[attr-defined]
    assert event.broker_resulting_runtime_state == "stopped"  # type: ignore[attr-defined]
    assert event.broker_result_code == "success"  # type: ignore[attr-defined]
    assert container_id() not in event.getMessage()
    assert not hasattr(event, "broker_container_id")


def test_failure_audit_has_instance_and_stable_code_without_raw_error(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    provider = FakeContainerProvider(
        error=RuntimeStatePreconditionError("PRIVATE-STATE-DETAIL")
    )
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry), pytest.raises(RemoteBrokerError):
        call(BrokerClient(socket_config.socket_path))

    event = completed_audits(caplog)[0]
    assert event.broker_managed_instance_id == instance_id()  # type: ignore[attr-defined]
    assert event.broker_result_code == "runtime_state_precondition_failed"  # type: ignore[attr-defined]
    assert "PRIVATE" not in event.getMessage()

"""Broker/client integration tests for managed Docker inventory."""

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
    MAX_MANAGED_CONTAINERS,
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
    StopManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    DockerDaemonUnavailableClientError,
    DockerDaemonUnavailableError,
    DockerInventoryClientError,
    DockerInventoryReadError,
    InvalidManagedContainerMetadataError,
    InventoryLimitClientError,
    InventoryLimitExceededError,
    ManagedContainerMetadataClientError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import (
    Request,
    decode_json_object,
    encode_model,
    success_response,
)
from kodi_lab.broker.server import AUDIT_LOGGER_NAME
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import MAX_FRAME_PAYLOAD, receive_frame, send_frame


def instance_id(number: int) -> str:
    return str(UUID(int=number))


def record(number: int = 1) -> ManagedContainerRecord:
    return ManagedContainerRecord(
        managed_instance_id=instance_id(number),
        container_id=f"{number:064x}",
        image_id="sha256:" + ("b" * 64),
        runtime_state=ContainerRuntimeState.RUNNING,
        health_state=ContainerHealthState.HEALTHY,
    )


class FakeInventoryProvider:
    def __init__(
        self,
        records: tuple[ManagedContainerRecord, ...] = (),
        error: BaseException | None = None,
    ) -> None:
        self.records = records
        self.error = error
        self.calls = 0
        self.requested_ids: list[str | None] = []

    def list_managed_containers(
        self, *, managed_instance_id: str | None = None
    ) -> tuple[ManagedContainerRecord, ...]:
        self.calls += 1
        self.requested_ids.append(managed_instance_id)
        if self.error is not None:
            raise self.error
        return tuple(
            item
            for item in self.records
            if managed_instance_id is None
            or item.managed_instance_id == managed_instance_id
        )

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerRecord:
        raise AssertionError("inventory-only test reached stop mutation")

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerRecord:
        raise AssertionError("inventory-only test reached start mutation")


def raw_inventory_request(params: object) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "managed_container_inventory",
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


def test_authorized_client_receives_typed_minimal_inventory(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider((record(),))
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        result = BrokerClient(socket_config.socket_path).managed_container_inventory()

    assert isinstance(result, ManagedContainerInventoryResult)
    assert result.containers[0].managed_instance_id == instance_id(1)
    assert result.containers[0].runtime_state is ContainerRuntimeState.RUNNING
    assert provider.calls == 1
    document = result.model_dump(mode="json")
    assert set(document["containers"][0]) == {
        "managed_instance_id",
        "container_id",
        "image_id",
        "runtime_state",
        "health_state",
    }


def test_authorized_client_scopes_inventory_to_one_managed_instance(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider((record(1), record(2)))
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        result = BrokerClient(socket_config.socket_path).managed_container_inventory(
            managed_instance_id=instance_id(1)
        )
    assert [item.managed_instance_id for item in result.containers] == [instance_id(1)]
    assert provider.requested_ids == [instance_id(1)]


def test_unauthorized_peer_is_denied_before_docker_adapter(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider((record(),))
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, expected_uid=os.getuid() + 10_000, registry=registry),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).managed_container_inventory()
    assert provider.calls == 0


def test_invalid_params_are_rejected_before_docker_adapter(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider((record(),))
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        response = raw_round_trip(
            socket_config.socket_path,
            raw_inventory_request(
                {"container": "legacy-lookalike", "uid": os.getuid()}
            ),
        )
    assert provider.calls == 0
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_params"


def test_noncanonical_scoped_inventory_identity_is_rejected_before_docker(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider((record(),))
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        response = raw_round_trip(
            socket_config.socket_path,
            raw_inventory_request({"managed_instance_id": "not-a-uuid"}),
        )
    assert provider.calls == 0
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_params"


def test_capabilities_reports_exact_semantic_operations() -> None:
    provider = FakeInventoryProvider()
    result = build_foundation_registry(provider).dispatch(
        Request(
            version=1,
            request_id=str(uuid4()),
            operation="capabilities",
            params={},
        )
    )
    assert result.model_dump(mode="json")["operations"] == [
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
        "capture_smoke_log_cursor",
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
        "read_smoke_log_window",
        "reviewed_runtime_image_present",
        "start_managed_container",
        "stop_managed_container",
        "verify_addon_zip_enablement_destination",
        "verify_addon_zip_smoke_destination",
        "verify_attached_development_source_destination",
        "verify_mcp_workspace_destination",
    ]
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("adapter_error", "client_error", "code"),
    [
        (
            DockerDaemonUnavailableError("PRIVATE"),
            DockerDaemonUnavailableClientError,
            "docker_daemon_unavailable",
        ),
        (
            DockerInventoryReadError("PRIVATE"),
            DockerInventoryClientError,
            "docker_inventory_failed",
        ),
        (
            InvalidManagedContainerMetadataError("PRIVATE"),
            ManagedContainerMetadataClientError,
            "invalid_managed_container_metadata",
        ),
        (
            InventoryLimitExceededError("PRIVATE"),
            InventoryLimitClientError,
            "inventory_limit_exceeded",
        ),
    ],
)
def test_adapter_failures_become_stable_typed_client_errors(
    socket_config: SocketPathConfig,
    run_broker: Any,
    adapter_error: BaseException,
    client_error: type[RemoteBrokerError],
    code: str,
) -> None:
    provider = FakeInventoryProvider(error=adapter_error)
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, registry=registry),
        pytest.raises(client_error) as raised,
    ):
        BrokerClient(socket_config.socket_path).managed_container_inventory()
    assert raised.value.code == code
    assert "PRIVATE" not in str(raised.value)


def test_unexpected_adapter_failure_is_sanitized(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    provider = FakeInventoryProvider(error=RuntimeError("PRIVATE-TRACEBACK-DETAIL"))
    registry = build_foundation_registry(provider)
    with (
        run_broker(socket_config, registry=registry),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        BrokerClient(socket_config.socket_path).managed_container_inventory()
    assert raised.value.code == "internal_error"
    assert "PRIVATE" not in str(raised.value)


def test_success_audit_contains_count_not_inventory_data(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    provider = FakeInventoryProvider((record(),))
    registry = build_foundation_registry(provider)
    with run_broker(socket_config, registry=registry):
        BrokerClient(socket_config.socket_path).managed_container_inventory()

    completed = [
        event
        for event in caplog.records
        if event.name == AUDIT_LOGGER_NAME
        and event.getMessage() == "broker request completed"
    ]
    assert len(completed) == 1
    event = completed[0]
    assert event.broker_inventory_count == 1  # type: ignore[attr-defined]
    assert event.broker_operation == "managed_container_inventory"  # type: ignore[attr-defined]
    assert instance_id(1) not in event.getMessage()


def test_registry_has_no_generic_docker_or_execution_operation() -> None:
    names = build_foundation_registry(FakeInventoryProvider()).names
    assert names == (
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
        "capture_smoke_log_cursor",
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
        "read_smoke_log_window",
        "reviewed_runtime_image_present",
        "start_managed_container",
        "stop_managed_container",
        "verify_addon_zip_enablement_destination",
        "verify_addon_zip_smoke_destination",
        "verify_attached_development_source_destination",
        "verify_mcp_workspace_destination",
    )
    assert not {"docker", "execute", "shell", "command", "raw"}.intersection(names)


def test_maximum_inventory_response_fits_protocol_frame() -> None:
    provider = FakeInventoryProvider(
        tuple(record(number) for number in range(1, MAX_MANAGED_CONTAINERS + 1))
    )
    registry = build_foundation_registry(provider)
    request_id = str(uuid4())
    result = registry.dispatch(
        Request(
            version=1,
            request_id=request_id,
            operation="managed_container_inventory",
            params={},
        )
    )
    assert len(encode_model(success_response(request_id, result))) <= MAX_FRAME_PAYLOAD

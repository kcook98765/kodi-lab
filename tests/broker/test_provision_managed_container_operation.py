"""Broker/client integration for trusted semantic provisioning."""

import json
import os
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    ClientProtocolError,
    ManagedNetworkUnavailableError,
    PartialProvisioningClientError,
    PersistentProvisioningUnsupportedError,
    PostCreateVerificationClientError,
    ProvisioningPrerequisiteClientError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    OperationRegistry,
    ProvisionManagedContainerResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame
from kodi_lab.domain import PythonBytecodePolicy

from .test_provision_managed_container import (
    FakeProvisioningDockerClient,
    container_data,
)

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = str(UUID(int=2))
CONTAINER_ID = "1" * 64
IMAGE_ID = "sha256:" + ("a" * 64)


class FakeResolver:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def resolve(self, managed_instance_id: str) -> ManagedContainerProvisioningSpec:
        self.calls.append(managed_instance_id)
        if self.error is not None:
            raise self.error
        return ManagedContainerProvisioningSpec(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=REVISION_ID,
            image_id=IMAGE_ID,
            container_name="kodi-lab-managed-" + UUID(managed_instance_id).hex,
            runtime_uid=1000,
            runtime_gid=1000,
            python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        )


class FakeProvisioner:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[ManagedContainerProvisioningSpec] = []

    def provision_managed_container(
        self, spec: ManagedContainerProvisioningSpec
    ) -> ProvisionedManagedContainerRecord:
        self.calls.append(spec)
        if self.error is not None:
            raise self.error
        return ProvisionedManagedContainerRecord(
            managed_instance_id=spec.managed_instance_id,
            runtime_revision_id=spec.runtime_revision_id,
            container_id=CONTAINER_ID,
            image_id=spec.image_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            created=True,
        )


def registry(resolver: FakeResolver, provisioner: FakeProvisioner) -> OperationRegistry:
    return build_foundation_registry(
        provisioning_resolver=resolver, provisioning_provider=provisioner
    )


def raw_round_trip(path: Path, params: object) -> dict[str, object]:
    payload = json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": "provision_managed_container",
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


def test_authorized_client_gets_typed_minimal_provision_result(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver()
    provisioner = FakeProvisioner()
    with run_broker(socket_config, registry=registry(resolver, provisioner)):
        result = BrokerClient(socket_config.socket_path).provision_managed_container(
            managed_instance_id=INSTANCE_ID
        )
    assert result == ProvisionManagedContainerResult(
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        container_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        runtime_state=ContainerRuntimeState.STOPPED,
        created=True,
    )
    assert resolver.calls == [INSTANCE_ID]
    assert len(provisioner.calls) == 1


def test_unauthorized_peer_does_zero_resolver_or_docker_work(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver()
    provisioner = FakeProvisioner()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=registry(resolver, provisioner),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).provision_managed_container(
            managed_instance_id=INSTANCE_ID
        )
    assert resolver.calls == []
    assert provisioner.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"managed_instance_id": "not-a-uuid"},
        {"managed_instance_id": INSTANCE_ID, "image": "mutable:tag"},
        {"managed_instance_id": INSTANCE_ID, "network": "legacy"},
        {"managed_instance_id": INSTANCE_ID, "mount": "/host"},
        {"managed_instance_id": INSTANCE_ID, "privileged": True},
        {"managed_instance_id": INSTANCE_ID, "log_driver": "none"},
        {"managed_instance_id": INSTANCE_ID, "log_config": {"Type": "none"}},
        {"managed_instance_id": INSTANCE_ID, "max-file": "99"},
        {
            "managed_instance_id": INSTANCE_ID,
            "labels": {"io.kodi-lab.log-policy": "old"},
        },
    ],
)
def test_invalid_or_injected_configuration_never_reaches_resolver(
    socket_config: SocketPathConfig,
    run_broker: Any,
    params: dict[str, object],
) -> None:
    resolver = FakeResolver()
    provisioner = FakeProvisioner()
    with run_broker(socket_config, registry=registry(resolver, provisioner)):
        response = raw_round_trip(socket_config.socket_path, params)
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_params"
    assert resolver.calls == []
    assert provisioner.calls == []


def test_client_validates_identity_locally(tmp_path: Path) -> None:
    with pytest.raises(ClientProtocolError):
        BrokerClient(tmp_path / "unused.sock").provision_managed_container(
            managed_instance_id="not-a-uuid"
        )


def test_network_prerequisite_has_typed_sanitized_client_error(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver()
    provisioner = FakeProvisioner(
        ManagedNetworkUnavailableError("PRIVATE-NETWORK-DETAIL")
    )
    with (
        run_broker(socket_config, registry=registry(resolver, provisioner)),
        pytest.raises(ProvisioningPrerequisiteClientError) as raised,
    ):
        BrokerClient(socket_config.socket_path).provision_managed_container(
            managed_instance_id=INSTANCE_ID
        )
    assert raised.value.code == "managed_network_unavailable"
    assert "PRIVATE" not in str(raised.value)


def test_resolver_prerequisite_failure_never_reaches_docker(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(
        PersistentProvisioningUnsupportedError("PRIVATE-PERSISTENT-DETAIL")
    )
    provisioner = FakeProvisioner()
    with (
        run_broker(socket_config, registry=registry(resolver, provisioner)),
        pytest.raises(ProvisioningPrerequisiteClientError) as raised,
    ):
        BrokerClient(socket_config.socket_path).provision_managed_container(
            managed_instance_id=INSTANCE_ID
        )
    assert raised.value.code == "persistent_provisioning_unsupported"
    assert "PRIVATE" not in str(raised.value)
    assert provisioner.calls == []


def test_unexpected_provisioning_handler_failure_is_sanitized(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver()
    provisioner = FakeProvisioner(RuntimeError("PRIVATE-HANDLER-DETAIL"))
    with (
        run_broker(socket_config, registry=registry(resolver, provisioner)),
        pytest.raises(RemoteBrokerError) as raised,
    ):
        BrokerClient(socket_config.socket_path).provision_managed_container(
            managed_instance_id=INSTANCE_ID
        )
    assert raised.value.code == "internal_error"
    assert "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("outcome", ["created", "bad_limits", "identity_drift"])
def test_logging_policy_through_real_broker_adapter_round_trip(
    socket_config: SocketPathConfig, run_broker: Any, outcome: str
) -> None:
    data = container_data()
    if outcome != "created":
        host = data["HostConfig"]
        assert isinstance(host, dict)
        host["LogConfig"] = {"Type": "none", "Config": {}}
    cleanup = container_data(
        container_id="2" * 64 if outcome == "identity_drift" else CONTAINER_ID
    )
    docker_client = FakeProvisioningDockerClient(inspections=[data, cleanup])
    adapter = DockerManagedContainerAdapter(lambda: docker_client)
    resolver = FakeResolver()
    operations = build_foundation_registry(
        provisioning_resolver=resolver, provisioning_provider=adapter
    )
    with run_broker(socket_config, registry=operations):
        client = BrokerClient(socket_config.socket_path)
        if outcome == "created":
            assert client.provision_managed_container(
                managed_instance_id=INSTANCE_ID
            ).created
        else:
            error = (
                PartialProvisioningClientError
                if outcome == "identity_drift"
                else PostCreateVerificationClientError
            )
            with pytest.raises(error):
                client.provision_managed_container(managed_instance_id=INSTANCE_ID)
    assert resolver.calls == [INSTANCE_ID]
    assert docker_client.host_config_calls[0]["log_config"] == {
        "Type": "json-file",
        "Config": {"max-size": "1m", "max-file": "2"},
    }
    assert docker_client.remove_calls == (
        [(CONTAINER_ID, False, False)] if outcome == "bad_limits" else []
    )
    assert docker_client.start_calls == docker_client.stop_calls == 0

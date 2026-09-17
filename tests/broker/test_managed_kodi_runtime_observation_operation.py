import os
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.broker import BrokerAuthorizationError, BrokerClient
from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    KodiProcessState,
    ManagedKodiRuntimeObservationRecord,
)
from kodi_lab.broker.operations import OperationRegistry, build_foundation_registry
from kodi_lab.broker.provisioning import ManagedRuntimeObservationSpec
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.domain import (
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = UUID(int=2)
CONTAINER_ID = "a" * 64


def profile() -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )


class FakeResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, managed_instance_id: str) -> ManagedRuntimeObservationSpec:
        self.calls += 1
        assert managed_instance_id == INSTANCE_ID
        return ManagedRuntimeObservationSpec(
            managed_instance_id=INSTANCE_ID,
            runtime_revision_id=str(REVISION_ID),
            profile=profile(),
        )


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def observe_managed_kodi_runtime(
        self, *, spec: ManagedRuntimeObservationSpec, expected_container_id: str
    ) -> ManagedKodiRuntimeObservationRecord:
        self.calls += 1
        assert spec.profile.jsonrpc_port == 8080
        assert expected_container_id == CONTAINER_ID
        return ManagedKodiRuntimeObservationRecord(
            managed_instance_id=INSTANCE_ID,
            runtime_revision_id=str(REVISION_ID),
            container_id=CONTAINER_ID,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.8",
            jsonrpc_port=8080,
        )


def registry(resolver: FakeResolver, provider: FakeProvider) -> OperationRegistry:
    return build_foundation_registry(
        runtime_observation_resolver=resolver,
        runtime_observation_provider=provider,
    )


def test_typed_client_observes_only_fixed_managed_endpoint(
    socket_config: SocketPathConfig,
    run_broker: Any,
) -> None:
    resolver = FakeResolver()
    provider = FakeProvider()
    with run_broker(socket_config, registry=registry(resolver, provider)):
        result = BrokerClient(socket_config.socket_path).observe_managed_kodi_runtime(
            managed_instance_id=INSTANCE_ID,
            expected_container_id=CONTAINER_ID,
        )
    assert result.ipv4_address == "10.250.0.8"
    assert result.jsonrpc_port == 8080
    assert result.process_state is KodiProcessState.RUNNING
    assert resolver.calls == provider.calls == 1


def test_authorization_occurs_before_runtime_resolution(
    socket_config: SocketPathConfig,
    run_broker: Any,
) -> None:
    resolver = FakeResolver()
    provider = FakeProvider()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=registry(resolver, provider),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).observe_managed_kodi_runtime(
            managed_instance_id=INSTANCE_ID,
            expected_container_id=CONTAINER_ID,
        )
    assert resolver.calls == provider.calls == 0


@pytest.mark.parametrize("extra", [{"network": "legacy"}, {"port": 80}])
def test_schema_has_no_caller_network_or_port_selection(
    socket_config: SocketPathConfig,
    run_broker: Any,
    extra: dict[str, object],
) -> None:
    import json
    import socket
    import struct
    from uuid import uuid4

    resolver = FakeResolver()
    provider = FakeProvider()
    document = {
        "version": 1,
        "request_id": str(uuid4()),
        "operation": "observe_managed_kodi_runtime",
        "params": {
            "managed_instance_id": INSTANCE_ID,
            "expected_container_id": CONTAINER_ID,
            **extra,
        },
    }
    payload = json.dumps(document).encode()
    with run_broker(socket_config, registry=registry(resolver, provider)):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(socket_config.socket_path))
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        connection.shutdown(socket.SHUT_WR)
        size = struct.unpack("!I", connection.recv(4))[0]
        response = json.loads(connection.recv(size))
        connection.close()
    assert response["error"]["code"] == "invalid_params"
    assert resolver.calls == provider.calls == 0

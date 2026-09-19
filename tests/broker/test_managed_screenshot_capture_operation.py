import json
import os
import socket
import struct
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker import BrokerAuthorizationError, BrokerClient
from kodi_lab.broker.operations import OperationRegistry, build_foundation_registry
from kodi_lab.broker.screenshots import (
    CapturedManagedScreenshotRecord,
    ManagedScreenshotCaptureSpec,
)
from kodi_lab.broker.socket_path import SocketPathConfig

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = str(UUID(int=2))
CAPTURE_ID = str(UUID(int=3))
CONTAINER_ID = "a" * 64


class FakeResolver:
    def __init__(self, artifact: Path) -> None:
        self.artifact = artifact
        self.calls = 0

    def resolve(
        self,
        managed_instance_id: str,
        expected_runtime_revision_id: str,
        capture_id: str,
    ) -> ManagedScreenshotCaptureSpec:
        self.calls += 1
        assert (managed_instance_id, expected_runtime_revision_id, capture_id) == (
            INSTANCE_ID,
            REVISION_ID,
            CAPTURE_ID,
        )
        return ManagedScreenshotCaptureSpec(
            managed_instance_id=INSTANCE_ID,
            runtime_revision_id=REVISION_ID,
            capture_id=CAPTURE_ID,
            artifact_path=self.artifact,
            artifact_owner_uid=os.getuid(),
            artifact_owner_gid=os.getgid(),
        )


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def capture_managed_kodi_screenshot(
        self, *, spec: ManagedScreenshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedScreenshotRecord:
        self.calls += 1
        assert expected_container_id == CONTAINER_ID
        assert spec.capture_id == CAPTURE_ID
        return CapturedManagedScreenshotRecord(
            managed_instance_id=INSTANCE_ID,
            runtime_revision_id=REVISION_ID,
            container_id=CONTAINER_ID,
            capture_id=CAPTURE_ID,
            backend="x11_get_image",
            raw_byte_size=3_686_500,
        )


def registry(resolver: FakeResolver, provider: FakeProvider) -> OperationRegistry:
    return build_foundation_registry(
        screenshot_capture_resolver=resolver,
        screenshot_capture_provider=provider,
    )


def test_typed_client_receives_only_safe_capture_metadata(
    tmp_path: Path,
    socket_config: SocketPathConfig,
    run_broker: Any,
) -> None:
    resolver = FakeResolver(tmp_path / "frame.raw")
    provider = FakeProvider()
    with run_broker(socket_config, registry=registry(resolver, provider)):
        result = BrokerClient(
            socket_config.socket_path
        ).capture_managed_kodi_screenshot(
            managed_instance_id=INSTANCE_ID,
            expected_container_id=CONTAINER_ID,
            expected_runtime_revision_id=REVISION_ID,
            capture_id=CAPTURE_ID,
        )

    assert result.backend == "x11_get_image"
    assert result.raw_byte_size == 3_686_500
    assert "path" not in result.model_dump()
    assert "command" not in result.model_dump()
    assert resolver.calls == provider.calls == 1


def test_authorization_precedes_screenshot_resolution(
    tmp_path: Path,
    socket_config: SocketPathConfig,
    run_broker: Any,
) -> None:
    resolver = FakeResolver(tmp_path / "frame.raw")
    provider = FakeProvider()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=registry(resolver, provider),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).capture_managed_kodi_screenshot(
            managed_instance_id=INSTANCE_ID,
            expected_container_id=CONTAINER_ID,
            expected_runtime_revision_id=REVISION_ID,
            capture_id=CAPTURE_ID,
        )
    assert resolver.calls == provider.calls == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "sh"},
        {"path": "/tmp/image"},
        {"display": ":0"},
        {"format": "jpeg"},
        {"timeout": 90},
        {"argv": ["id"]},
    ],
)
def test_schema_rejects_caller_controlled_capture_primitive(
    tmp_path: Path,
    socket_config: SocketPathConfig,
    run_broker: Any,
    extra: dict[str, object],
) -> None:
    resolver = FakeResolver(tmp_path / "frame.raw")
    provider = FakeProvider()
    document = {
        "version": 1,
        "request_id": str(uuid4()),
        "operation": "capture_managed_kodi_screenshot",
        "params": {
            "managed_instance_id": INSTANCE_ID,
            "expected_container_id": CONTAINER_ID,
            "expected_runtime_revision_id": REVISION_ID,
            "capture_id": CAPTURE_ID,
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

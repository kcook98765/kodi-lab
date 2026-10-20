from __future__ import annotations

import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentSpec,
    DevelopmentSourceDeploymentStateError,
    DevelopmentSourceDestinationVerificationSpec,
)
from kodi_lab.broker.docker_inventory import (
    AppliedDevelopmentSourceDeploymentRecord,
    VerifiedDevelopmentSourceDestinationRecord,
)
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    DevelopmentSourceDeploymentClientError,
    DockerDevelopmentSourceDeploymentError,
)
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    OperationRegistry,
    VerifyAttachedDevelopmentSourceDestinationResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame

INSTANCE_ID = str(UUID(int=101))
SOURCE_ID = str(UUID(int=102))
OPERATION_ID = str(UUID(int=103))
REVISION_ID = str(UUID(int=104))
WORKSPACE_ID = str(UUID(int=105))
CONTAINER_ID = "a" * 64


def _spec(tmp_path: Path) -> DevelopmentSourceDeploymentSpec:
    return DevelopmentSourceDeploymentSpec(
        managed_instance_id=INSTANCE_ID,
        development_source_id=SOURCE_ID,
        deployment_operation_id=OPERATION_ID,
        runtime_revision_id=REVISION_ID,
        runtime_uid=2468,
        runtime_gid=1357,
        development_root=tmp_path / "configured-development-root",
        relative_locator="registered-source",
        addon_id="plugin.video.example",
        observed_name="Example",
        observed_version="1.0.0",
        staging_root=tmp_path / "private-staging",
        prior_evidence=None,
    )


class FakeResolver:
    def __init__(
        self, spec: DevelopmentSourceDeploymentSpec, error: Exception | None = None
    ) -> None:
        self.spec = spec
        self.error = error
        self.calls: list[tuple[str, str, str, str]] = []
        self.verification_calls: list[tuple[str, str, str]] = []
        self.workspace_calls: list[tuple[str, str, int, str, str]] = []
        self.workspace_verification_calls: list[tuple[str, str, str]] = []

    def resolve(
        self,
        managed_instance_id: str,
        development_source_id: str,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec:
        self.calls.append(
            (
                managed_instance_id,
                development_source_id,
                deployment_operation_id,
                expected_container_id,
            )
        )
        if self.error is not None:
            raise self.error
        return self.spec

    def resolve_destination_verification(
        self,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec:
        self.verification_calls.append(
            (managed_instance_id, development_source_id, expected_container_id)
        )
        return DevelopmentSourceDestinationVerificationSpec(
            managed_instance_id=managed_instance_id,
            development_source_id=development_source_id,
            runtime_revision_id=REVISION_ID,
            runtime_uid=2468,
            runtime_gid=1357,
            addon_id="plugin.video.example",
            addon_version="1.0.0",
            verified_destination_digest="sha256:" + "2" * 64,
            trusted_container_id=expected_container_id,
        )

    def resolve_workspace_deployment(
        self,
        managed_instance_id: str,
        workspace_id: str,
        expected_generation: int,
        deployment_operation_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDeploymentSpec:
        self.workspace_calls.append(
            (
                managed_instance_id,
                workspace_id,
                expected_generation,
                deployment_operation_id,
                expected_container_id,
            )
        )
        return self.spec

    def resolve_workspace_reconciliation(
        self,
        deployment_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec:
        return replace(
            self.resolve_workspace_destination_verification(
                deployment_operation_id, managed_instance_id, expected_container_id
            ),
            require_stopped=True,
            requested_source_digest="sha256:" + "1" * 64,
        )

    def resolve_workspace_destination_verification(
        self,
        deployment_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> DevelopmentSourceDestinationVerificationSpec:
        self.workspace_verification_calls.append(
            (deployment_operation_id, managed_instance_id, expected_container_id)
        )
        return DevelopmentSourceDestinationVerificationSpec(
            managed_instance_id=managed_instance_id,
            development_source_id=SOURCE_ID,
            runtime_revision_id=REVISION_ID,
            runtime_uid=2468,
            runtime_gid=1357,
            addon_id="plugin.video.example",
            addon_version="1.0.0",
            verified_destination_digest="sha256:" + "2" * 64,
            trusted_container_id=expected_container_id,
        )


class FakeProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[DevelopmentSourceDeploymentSpec, str]] = []
        self.verification_calls: list[
            tuple[DevelopmentSourceDestinationVerificationSpec, str]
        ] = []

    def copy_sync_attached_development_source(
        self,
        *,
        spec: DevelopmentSourceDeploymentSpec,
        expected_container_id: str,
    ) -> AppliedDevelopmentSourceDeploymentRecord:
        self.calls.append((spec, expected_container_id))
        if self.error is not None:
            raise self.error
        return AppliedDevelopmentSourceDeploymentRecord(
            managed_instance_id=spec.managed_instance_id,
            development_source_id=spec.development_source_id,
            deployment_operation_id=spec.deployment_operation_id,
            runtime_revision_id=spec.runtime_revision_id,
            container_id=expected_container_id,
            result_classification="initial_deployment",
            source_manifest_digest="sha256:" + "1" * 64,
            verified_destination_digest="sha256:" + "2" * 64,
            observed_source_name=spec.observed_name,
            observed_source_version=spec.observed_version,
            new_count=3,
            changed_count=0,
            unchanged_count=0,
            destination_only_count=0,
            transferred_bytes=123,
        )

    def verify_attached_development_source_destination(
        self,
        *,
        spec: DevelopmentSourceDestinationVerificationSpec,
        expected_container_id: str,
    ) -> VerifiedDevelopmentSourceDestinationRecord:
        self.verification_calls.append((spec, expected_container_id))
        return VerifiedDevelopmentSourceDestinationRecord(
            managed_instance_id=spec.managed_instance_id,
            development_source_id=spec.development_source_id,
            runtime_revision_id=spec.runtime_revision_id,
            container_id=expected_container_id,
            addon_id=spec.addon_id,
            addon_version=spec.addon_version,
            matched=True,
            verified_destination_digest=spec.verified_destination_digest,
        )


def _registry(provider: FakeProvider, resolver: FakeResolver) -> OperationRegistry:
    return build_foundation_registry(
        provider,  # type: ignore[arg-type]
        development_source_deployment_resolver=resolver,
        development_source_deployment_provider=provider,
    )


def _call(client: BrokerClient) -> CopySyncAttachedDevelopmentSourceResult:
    return client.copy_sync_attached_development_source(
        managed_instance_id=INSTANCE_ID,
        development_source_id=SOURCE_ID,
        expected_container_id=CONTAINER_ID,
        deployment_operation_id=OPERATION_ID,
    )


def _verify_call(
    client: BrokerClient,
) -> VerifyAttachedDevelopmentSourceDestinationResult:
    return client.verify_attached_development_source_destination(
        managed_instance_id=INSTANCE_ID,
        development_source_id=SOURCE_ID,
        expected_container_id=CONTAINER_ID,
    )


def _workspace_call(client: BrokerClient) -> CopySyncAttachedDevelopmentSourceResult:
    return client.copy_sync_mcp_workspace(
        managed_instance_id=INSTANCE_ID,
        workspace_id=WORKSPACE_ID,
        expected_generation=7,
        expected_container_id=CONTAINER_ID,
        deployment_operation_id=OPERATION_ID,
    )


def _workspace_verify_call(
    client: BrokerClient,
) -> VerifyAttachedDevelopmentSourceDestinationResult:
    return client.verify_mcp_workspace_destination(
        deployment_operation_id=OPERATION_ID,
        managed_instance_id=INSTANCE_ID,
        expected_container_id=CONTAINER_ID,
    )


def _raw(
    path: Path,
    params: object,
    *,
    operation: str = "copy_sync_attached_development_source",
) -> dict[str, object]:
    payload = json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": operation,
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


def test_protocol_resolves_all_filesystem_and_ownership_authority(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        result = _call(BrokerClient(socket_config.socket_path))
    assert resolver.calls == [(INSTANCE_ID, SOURCE_ID, OPERATION_ID, CONTAINER_ID)]
    assert provider.calls == [(_spec(tmp_path), CONTAINER_ID)]
    serialized = result.model_dump()
    assert not {
        "source_path",
        "destination",
        "uid",
        "gid",
        "files",
        "archive",
        "command",
    } & set(serialized)


def test_destination_verification_protocol_accepts_semantic_identities_only(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        result = _verify_call(BrokerClient(socket_config.socket_path))
    assert result.matched is True
    assert resolver.verification_calls == [(INSTANCE_ID, SOURCE_ID, CONTAINER_ID)]
    assert len(provider.verification_calls) == 1
    assert not {
        "path",
        "digest",
        "files",
        "archive",
        "uid",
        "gid",
    } & set(result.model_dump())


def test_workspace_protocol_accepts_only_semantic_exact_generation_authority(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        client = BrokerClient(socket_config.socket_path)
        deployed = _workspace_call(client)
        verified = _workspace_verify_call(client)
    assert deployed.deployment_operation_id == OPERATION_ID
    assert verified.matched
    assert resolver.workspace_calls == [
        (INSTANCE_ID, WORKSPACE_ID, 7, OPERATION_ID, CONTAINER_ID)
    ]
    assert resolver.workspace_verification_calls == [
        (OPERATION_ID, INSTANCE_ID, CONTAINER_ID)
    ]
    assert len(provider.calls) == len(provider.verification_calls) == 1


def test_workspace_protocol_rejects_paths_commands_and_lifecycle_options(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    params = {
        "managed_instance_id": INSTANCE_ID,
        "workspace_id": WORKSPACE_ID,
        "expected_generation": 7,
        "expected_container_id": CONTAINER_ID,
        "deployment_operation_id": OPERATION_ID,
        "destination": "/home/kodi/.kodi/addons",
        "command": ["rsync", "--delete"],
        "restart": True,
    }
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        response = _raw(
            socket_config.socket_path,
            params,
            operation="copy_sync_mcp_workspace",
        )
    assert response["error"]["code"] == "invalid_params"  # type: ignore[index]
    assert resolver.workspace_calls == []
    assert provider.calls == []


def test_destination_verification_protocol_rejects_path_and_digest_authority(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    params = {
        "managed_instance_id": INSTANCE_ID,
        "development_source_id": SOURCE_ID,
        "expected_container_id": CONTAINER_ID,
        "path": "/home/kodi/.kodi/addons/plugin.video.example",
        "digest": "sha256:" + "2" * 64,
    }
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        response = _raw(
            socket_config.socket_path,
            params,
            operation="verify_attached_development_source_destination",
        )
    assert response["ok"] is False
    assert resolver.verification_calls == []
    assert provider.verification_calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"source_path": "/tmp/source"},
        {"destination": "/tmp/destination"},
        {"uid": 0, "gid": 0},
        {"files": ["addon.xml"]},
        {"archive": "payload"},
        {"command": ["cp", "-a"]},
        {"copy_flags": ["--delete"]},
    ],
)
def test_protocol_cannot_express_generic_filesystem_or_ownership_authority(
    tmp_path: Path,
    socket_config: SocketPathConfig,
    run_broker: Any,
    extra: dict[str, object],
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    params = {
        "managed_instance_id": INSTANCE_ID,
        "development_source_id": SOURCE_ID,
        "expected_container_id": CONTAINER_ID,
        "deployment_operation_id": OPERATION_ID,
        **extra,
    }
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        response = _raw(socket_config.socket_path, params)
    assert response["error"]["code"] == "invalid_params"  # type: ignore[index]
    assert resolver.calls == []
    assert provider.calls == []


def test_unauthorized_peer_performs_no_resolution_or_docker_work(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    with (
        run_broker(
            socket_config,
            expected_uid=os.getuid() + 10_000,
            registry=_registry(provider, resolver),
        ),
        pytest.raises(BrokerAuthorizationError),
    ):
        _call(BrokerClient(socket_config.socket_path))
    assert resolver.calls == []
    assert provider.calls == []


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            DevelopmentSourceDeploymentStateError("private persistence detail"),
            "development_source_deployment_state_invalid",
        ),
        (
            DockerDevelopmentSourceDeploymentError(
                "development_source_replacement_conflict", "safe conflict"
            ),
            "development_source_replacement_conflict",
        ),
    ],
)
def test_typed_failures_are_sanitized(
    tmp_path: Path,
    socket_config: SocketPathConfig,
    run_broker: Any,
    error: Exception,
    code: str,
) -> None:
    resolver = FakeResolver(_spec(tmp_path), error=error)
    provider = FakeProvider()
    with (
        run_broker(socket_config, registry=_registry(provider, resolver)),
        pytest.raises(DevelopmentSourceDeploymentClientError) as captured,
    ):
        _call(BrokerClient(socket_config.socket_path))
    assert captured.value.code == code
    assert "private persistence detail" not in str(captured.value)


def test_stopped_workspace_recovery_wire_is_read_only_and_exact(
    tmp_path: Path, socket_config: SocketPathConfig, run_broker: Any
) -> None:
    resolver = FakeResolver(_spec(tmp_path))
    provider = FakeProvider()
    with run_broker(socket_config, registry=_registry(provider, resolver)):
        proof = BrokerClient(
            socket_config.socket_path
        ).inspect_mcp_workspace_deployment(
            deployment_operation_id=OPERATION_ID,
            managed_instance_id=INSTANCE_ID,
            expected_container_id=CONTAINER_ID,
        )
        rejected = _raw(
            socket_config.socket_path,
            {
                "deployment_operation_id": OPERATION_ID,
                "managed_instance_id": INSTANCE_ID,
                "expected_container_id": CONTAINER_ID,
                "path": "/private",
            },
            operation="inspect_mcp_workspace_deployment",
        )
    assert proof.matches_requested and not proof.matches_prior
    assert proof.deployment_operation_id == OPERATION_ID
    assert provider.calls == []
    assert len(provider.verification_calls) == 1
    assert provider.verification_calls[0][0].require_stopped
    assert rejected["ok"] is False

"""Native API/client/registry contract over a real, private Unix socket."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID
from kodi_lab.broker.addon_zips import AddonZipInstallationSpec
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    AddonZipDestinationRecord,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.errors import BrokerAuthorizationError, RemoteBrokerError
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    EmptyParams,
    OperationRegistry,
    build_foundation_registry,
)
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.storage.models import NativeZipInstallationRow
from tests.application.test_addon_zip_installations import factory as factory_fixture
from tests.application.test_native_zip_installations import Fixture
from tests.application.test_native_zip_installations import native as native_fixture
from tests.broker.test_server_client import raw_request, raw_round_trip
from tests.development.test_repository_zip import REPO_ID, repository_zip
from tests.web.test_native_zip_installations import auth
from tests.web.test_native_zip_installations import client as client_fixture

factory = factory_fixture
native = native_fixture
client = client_fixture

# Public identifiers from the failed acceptance run; no credential is retained.
FAILED_OPERATION_ID = UUID("6f6aab6f-98dc-4253-a90c-d611dc79cde0")
FAILED_BROKER_REQUEST_ID = "97b2f811-2488-4257-a9e8-ddb18998adcb"


@dataclass
class Wire:
    broker: BrokerClient
    registry: OperationRegistry
    observations: list[AddonZipInstallationSpec]


@pytest.fixture
def wire(
    native: Fixture,
    socket_config: SocketPathConfig,
    run_broker: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Wire]:
    observations: list[AddonZipInstallationSpec] = []

    def no_docker() -> Any:
        raise AssertionError("Contract tests must never contact Docker")

    provider = DockerManagedContainerAdapter(client_factory=no_docker)

    def observe(
        *, spec: AddonZipInstallationSpec, expected_container_id: str
    ) -> AddonZipDestinationRecord:
        observations.append(spec)
        return AddonZipDestinationRecord(
            managed_instance_id=spec.managed_instance_id,
            artifact_id=spec.artifact_id,
            installation_operation_id=spec.installation_operation_id,
            runtime_revision_id=spec.runtime_revision_id,
            container_id=expected_container_id,
            state=native.broker.destination,
            matched=native.broker.destination == "exact_payload_present",
        )

    # Only Docker readback is simulated. The operation schema, handler and
    # database/artifact resolver are the production implementations.
    monkeypatch.setattr(provider, "observe_addon_zip_destination", observe)
    registry = build_foundation_registry(
        container_provider=provider,
        addon_zip_installation_resolver=native.broker.resolver,
        addon_zip_installation_provider=provider,
    )
    broker = BrokerClient(socket_config.socket_path)
    monkeypatch.setattr(
        native.broker,
        "observe_native_zip_installation",
        broker.observe_native_zip_installation,
    )
    with run_broker(socket_config, registry=registry):
        yield Wire(broker, registry, observations)


def test_exact_failed_wire_request_and_complete_caller_contract(
    wire: Wire,
    native: Fixture,
    client: TestClient,
    socket_config: SocketPathConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setattr(
        "kodi_lab.application.native_zip_installations.uuid4",
        lambda: FAILED_OPERATION_ID,
    )
    response = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    )
    assert response.status_code == 202
    state = response.json()
    assert state["operation_id"] == str(FAILED_OPERATION_ID)
    assert state["state"] == "requested" and state["source_url"] is not None
    payload = json.dumps(
        {
            "version": 1,
            "request_id": FAILED_BROKER_REQUEST_ID,
            "operation": "observe_native_zip_installation",
            "params": {"operation_id": str(FAILED_OPERATION_ID)},
        }
    ).encode()
    result = raw_round_trip(socket_config.socket_path, payload)
    assert result["ok"] is True
    assert result["request_id"] == FAILED_BROKER_REQUEST_ID
    body = result["result"]
    assert isinstance(body, dict)
    assert body["installation_operation_id"] == str(FAILED_OPERATION_ID)
    observed = wire.broker.observe_native_zip_installation(FAILED_OPERATION_ID)
    assert observed.managed_instance_id == str(native.request.target_id)
    assert observed.artifact_id == str(native.request.artifact_id)
    assert observed.destination_state == "absent"
    assert len(wire.observations) == 3
    with native.sessions() as session:
        row = session.get(NativeZipInstallationRow, str(FAILED_OPERATION_ID))
        assert row is not None and row.stage_ready and row.zip_get_requested_at is None


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"operation_id": "not-a-uuid"},
        {"operation_id": str(FAILED_OPERATION_ID).upper()},
        {"operation_id": FAILED_OPERATION_ID.hex},
        {"operation_id": "{" + str(FAILED_OPERATION_ID) + "}"},
        {"operation_id": " " + str(FAILED_OPERATION_ID)},
        {"operation_id": None},
        {"operation_id": True},
        {"operation_id": 1},
        {"operation_id": 1.0},
        {"operation_id": []},
        {"operation_id": {}},
        {"operation_id": [str(FAILED_OPERATION_ID)]},
        {"operation_id": {"id": str(FAILED_OPERATION_ID)}},
        {"operation_id": str(FAILED_OPERATION_ID), "path": "/tmp/elsewhere"},
        {"operation_id": str(FAILED_OPERATION_ID), "target_id": str(uuid4())},
        {"operation_id": str(FAILED_OPERATION_ID), "expected_container_id": "b" * 64},
    ],
)
def test_invalid_native_parameters_fail_before_resolver(
    wire: Wire, socket_config: SocketPathConfig, params: dict[str, object]
) -> None:
    response = raw_round_trip(
        socket_config.socket_path,
        raw_request("observe_native_zip_installation", params),
    )
    assert response["ok"] is False
    assert response["error"] == {
        "code": "invalid_params",
        "message": "Operation parameters do not match the required schema.",
    }
    assert wire.observations == []


def test_unknown_operation_uuid_reaches_resolver_and_propagates_error(
    wire: Wire,
) -> None:
    with pytest.raises(RemoteBrokerError) as caught:
        wire.broker.observe_native_zip_installation(uuid4())
    assert caught.value.code == "native_zip_observation_unavailable"
    assert caught.value.request_id is not None
    assert wire.observations == []


def test_unauthorized_unix_peer_is_rejected(
    wire: Wire, native: Fixture, run_broker: Any, socket_config: SocketPathConfig
) -> None:
    # Use a separate socket while the authorized fixture remains listening.
    from dataclasses import replace

    denied = replace(
        socket_config, socket_path=socket_config.runtime_directory / "denied.sock"
    )
    with (
        run_broker(denied, registry=wire.registry, expected_uid=os.getuid() + 1),
        pytest.raises(BrokerAuthorizationError) as caught,
    ):
        BrokerClient(denied.socket_path).observe_native_zip_installation(uuid4())
    assert caught.value.code == "unauthorized_peer"
    assert wire.observations == [] and native.broker.calls == []


@pytest.mark.parametrize("mutation", ["malformed_uuid", "missing_field", "extra_field"])
def test_invalid_api_input_is_422_before_broker(
    wire: Wire, native: Fixture, client: TestClient, mutation: str
) -> None:
    body = native.request.model_dump(mode="json")
    if mutation == "malformed_uuid":
        body["target_id"] = "invalid"
    elif mutation == "missing_field":
        del body["target_id"]
    else:
        body["path"] = "/tmp/elsewhere"
    response = client.post(
        "/api/native-zip-installations", json=body, headers=auth(native)
    )
    assert response.status_code == 422
    assert wire.observations == [] and native.broker.calls == []


@pytest.mark.parametrize(
    "denial", ["grant", "client_permission", "target_permission", "cross_target"]
)
def test_target_authorization_precedes_native_broker_calls(
    wire: Wire, native: Fixture, client: TestClient, denial: str
) -> None:
    body = native.request.model_dump(mode="json")
    if denial == "grant":
        native.admin.remove_target_grant(
            native.principal.client_id, native.request.target_id
        )
    elif denial == "cross_target":
        body["target_id"] = str(uuid4())
    else:
        preset = native.admin.create_custom_preset(
            display_name="Inspection without installation",
            template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
            permissions=frozenset({"target.inspect"}),
        )
        if denial == "client_permission":
            native.admin.assign_client_preset(native.principal.client_id, preset.id)
        else:
            native.admin.assign_target_preset(native.request.target_id, preset.id)
    response = client.post(
        "/api/native-zip-installations", json=body, headers=auth(native)
    )
    assert response.status_code == 403
    assert wire.observations == [] and native.broker.calls == []
    with native.sessions() as session:
        assert session.scalar(select(NativeZipInstallationRow)) is None


def test_cross_target_peer_cannot_download_staged_zip(
    wire: Wire, native: Fixture, client: TestClient
) -> None:
    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    package = state["source_url"] + f"{REPO_ID}-1.0.0.zip"
    with TestClient(client.app, client=("10.250.0.11", 1234)) as other:
        assert other.get(package).status_code == 404
    assert client.get(package).content == repository_zip()
    assert wire.observations


def test_broker_invalid_params_propagates_to_api_without_delivery(
    wire: Wire, native: Fixture, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_request(_operation_id: UUID) -> AddonZipDestinationResult:
        return wire.broker._call(
            "observe_native_zip_installation", EmptyParams(), AddonZipDestinationResult
        )

    monkeypatch.setattr(
        native.broker, "observe_native_zip_installation", invalid_request
    )
    response = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    )
    # Existing Web conventions classify internal broker contract failures as
    # unavailable, but preserve their safe machine-readable code.
    assert response.status_code == 503
    assert response.json() == {"detail": "invalid_params"}
    assert wire.observations == []
    with native.sessions() as session:
        row = session.scalar(select(NativeZipInstallationRow))
        assert row is not None and row.state == "failed" and not row.stage_ready
        assert row.zip_get_requested_at is None

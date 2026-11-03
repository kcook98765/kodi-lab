"""Minimal proof transport is redacted and never downgraded to legacy admission."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr, field_validator

from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    OperationRegistry,
    StartManagedContainerParams,
    StartManagedContainerResult,
)
from kodi_lab.broker.protocol import StrictModel
from kodi_lab.broker.socket_path import SocketPathConfig


class LegacyStart(StrictModel):
    managed_instance_id: str
    expected_container_id: str
    expected_runtime_state: ContainerRuntimeState

    @field_validator("expected_runtime_state", mode="before")
    @classmethod
    def parse_state(cls, value: object) -> object:
        return ContainerRuntimeState(value) if isinstance(value, str) else value


def test_proof_crosses_private_wire_but_is_not_exposed_by_models_or_audit(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proof = "s" * 43
    target = str(uuid4())
    seen: list[SecretStr | None] = []
    registry = OperationRegistry()

    def handler(params: StartManagedContainerParams) -> StartManagedContainerResult:
        seen.append(params.ordinary_start_proof)
        return StartManagedContainerResult(
            managed_instance_id=params.managed_instance_id,
            container_id=params.expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            start_performed=True,
        )

    registry.register(
        name="start_managed_container",
        params_model=StartManagedContainerParams,
        result_model=StartManagedContainerResult,
        handler=handler,
    )
    params = StartManagedContainerParams(
        managed_instance_id=target,
        expected_container_id="a" * 64,
        expected_runtime_state=ContainerRuntimeState.STOPPED,
        ordinary_start_proof=SecretStr(proof),
    )
    assert proof not in repr(params) and proof not in params.model_dump_json()
    with run_broker(socket_config, registry=registry):
        BrokerClient(socket_config.socket_path).start_managed_container(
            managed_instance_id=target,
            expected_container_id="a" * 64,
            expected_runtime_state=ContainerRuntimeState.STOPPED,
            ordinary_start_proof=SecretStr(proof),
        )
    assert len(seen) == 1 and seen[0] is not None
    assert seen[0].get_secret_value() == proof
    assert proof not in caplog.text


@pytest.mark.parametrize("ordinary", [False, True])
def test_legacy_broker_accepts_legacy_request_but_refuses_proof_without_fallback(
    socket_config: SocketPathConfig,
    run_broker: Any,
    ordinary: bool,
) -> None:
    calls: list[str] = []
    registry = OperationRegistry()

    def handler(params: LegacyStart) -> StartManagedContainerResult:
        calls.append(params.managed_instance_id)
        return StartManagedContainerResult(
            managed_instance_id=params.managed_instance_id,
            container_id=params.expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            start_performed=True,
        )

    registry.register(
        name="start_managed_container",
        params_model=LegacyStart,
        result_model=StartManagedContainerResult,
        handler=handler,
    )
    with run_broker(socket_config, registry=registry):
        client = BrokerClient(socket_config.socket_path)
        if ordinary:
            with pytest.raises(ClientError):
                client.start_managed_container(
                    managed_instance_id=str(uuid4()),
                    expected_container_id="a" * 64,
                    expected_runtime_state=ContainerRuntimeState.STOPPED,
                    ordinary_start_proof=SecretStr("s" * 43),
                )
            assert calls == []
        else:
            client.start_managed_container(
                managed_instance_id=str(uuid4()),
                expected_container_id="a" * 64,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            assert len(calls) == 1

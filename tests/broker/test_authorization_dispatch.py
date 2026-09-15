"""Peer identity, exact-UID authorization, and dispatch tests."""

from __future__ import annotations

import os
import socket
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.broker.authorization import (
    ExactUidAuthorizationPolicy,
    PeerCredentials,
    extract_peer_credentials,
)
from kodi_lab.broker.errors import HandlerExecutionError, RequestError
from kodi_lab.broker.operations import (
    BrokerInfoResult,
    CapabilitiesResult,
    EmptyParams,
    OperationRegistry,
    PingResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import Request, StrictModel


def request(operation: str, params: dict[str, object] | None = None) -> Request:
    return Request(
        version=1,
        request_id=str(uuid4()),
        operation=operation,
        params={} if params is None else params,
    )


def test_real_unix_peer_credentials_match_current_process() -> None:
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        peer = extract_peer_credentials(server)
    finally:
        client.close()
        server.close()
    assert peer == PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())


def test_exact_uid_policy_accepts_only_uid() -> None:
    policy = ExactUidAuthorizationPolicy(expected_uid=1234)
    assert policy.is_authorized(PeerCredentials(pid=99, uid=1234, gid=8888))
    assert not policy.is_authorized(PeerCredentials(pid=1234, uid=55, gid=1234))
    assert not policy.is_authorized(PeerCredentials(pid=0, uid=0, gid=1234))


def test_foundation_registry_contains_exact_operation_set() -> None:
    registry = build_foundation_registry()
    assert registry.names == (
        "broker_info",
        "capabilities",
        "managed_container_inventory",
        "ping",
        "start_managed_container",
        "stop_managed_container",
    )
    assert registry.dispatch(request("ping")) == PingResult(
        status="healthy", protocol_version=1
    )
    assert isinstance(registry.dispatch(request("broker_info")), BrokerInfoResult)
    assert registry.dispatch(request("capabilities")) == CapabilitiesResult(
        operations=[
            "broker_info",
            "capabilities",
            "managed_container_inventory",
            "ping",
            "start_managed_container",
            "stop_managed_container",
        ],
        protocol_version=1,
    )
    assert all(
        registry.definition(name).requires_authorized_peer  # type: ignore[union-attr]
        for name in registry.names
    )


@pytest.mark.parametrize("name", ["serve_forever", "execute", "docker", "command"])
def test_unknown_operation_cannot_reach_attribute_or_escape_hatch(name: str) -> None:
    with pytest.raises(RequestError) as raised:
        build_foundation_registry().dispatch(request(name))
    assert raised.value.code == "unknown_operation"


@pytest.mark.parametrize(
    "name", ["ping", "broker_info", "capabilities", "managed_container_inventory"]
)
def test_empty_param_operations_reject_all_unexpected_params(name: str) -> None:
    with pytest.raises(RequestError) as raised:
        build_foundation_registry().dispatch(request(name, {"uid": os.getuid()}))
    assert raised.value.code == "invalid_params"


class TypedParams(StrictModel):
    label: str


class TypedResult(StrictModel):
    observed_type: Literal["TypedParams"]


def test_handler_receives_validated_typed_params() -> None:
    registry = OperationRegistry()

    def typed_handler(params: TypedParams) -> TypedResult:
        assert isinstance(params, TypedParams)
        return TypedResult(observed_type="TypedParams")

    registry.register(
        name="typed",
        params_model=TypedParams,
        result_model=TypedResult,
        handler=typed_handler,
    )
    assert registry.dispatch(request("typed", {"label": "safe"})) == TypedResult(
        observed_type="TypedParams"
    )


def test_handler_exception_is_replaced_by_safe_error() -> None:
    registry = OperationRegistry()

    def failing_handler(_params: EmptyParams) -> PingResult:
        raise RuntimeError("sensitive internal detail")

    registry.register(
        name="failing",
        params_model=EmptyParams,
        result_model=PingResult,
        handler=failing_handler,
    )
    with pytest.raises(HandlerExecutionError) as raised:
        registry.dispatch(request("failing"))
    assert raised.value.code == "internal_error"
    assert "sensitive" not in raised.value.safe_message


def test_request_identity_fields_are_not_part_of_schema() -> None:
    base: dict[str, object] = {
        "version": 1,
        "request_id": str(uuid4()),
        "operation": "ping",
        "params": {},
        "uid": os.getuid(),
        "gid": os.getgid(),
    }
    with pytest.raises(ValidationError):
        Request.model_validate(base)


def test_registry_handlers_are_explicit_callables_not_names() -> None:
    definition = build_foundation_registry().definition("ping")
    assert definition is not None
    assert callable(definition.handler)
    assert not isinstance(definition.handler, str)

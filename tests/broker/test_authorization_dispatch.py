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
    build_reviewed_runtime_registry,
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


def test_read_only_registry_discovery_and_peer_policy() -> None:
    registry = build_reviewed_runtime_registry()
    assert registry.dispatch(request("ping")) == PingResult(
        status="healthy", protocol_version=1
    )
    assert isinstance(registry.dispatch(request("broker_info")), BrokerInfoResult)
    for name in registry.names:
        operation = registry.definition(name)
        assert operation is not None and operation.requires_authorized_peer


REVIEWED_RUNTIME_OPERATIONS = [
    "active_reviewed_runtime_installation",
    "broker_info",
    "capabilities",
    "launch_reviewed_runtime_installation",
    "observe_reviewed_runtime_installation",
    "ping",
    "reviewed_runtime_image_present",
]


def test_capabilities_reports_exact_reviewed_runtime_operations() -> None:
    registry = build_reviewed_runtime_registry()
    assert list(registry.names) == REVIEWED_RUNTIME_OPERATIONS
    assert registry.dispatch(request("capabilities")) == CapabilitiesResult(
        operations=REVIEWED_RUNTIME_OPERATIONS, protocol_version=1
    )


@pytest.mark.parametrize("name", ["serve_forever", "execute", "docker", "command"])
def test_unknown_operation_cannot_reach_attribute_or_escape_hatch(name: str) -> None:
    with pytest.raises(RequestError) as raised:
        build_reviewed_runtime_registry().dispatch(request(name))
    assert raised.value.code == "unknown_operation"


@pytest.mark.parametrize(
    "name",
    ["ping", "broker_info", "capabilities", "active_reviewed_runtime_installation"],
)
def test_empty_param_operations_reject_all_unexpected_params(name: str) -> None:
    with pytest.raises(RequestError) as raised:
        build_reviewed_runtime_registry().dispatch(request(name, {"uid": os.getuid()}))
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
    definition = build_reviewed_runtime_registry().definition("ping")
    assert definition is not None
    assert callable(definition.handler)
    assert not isinstance(definition.handler, str)

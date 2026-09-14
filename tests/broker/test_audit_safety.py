"""Safe audit fields and non-leaking error behavior."""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.broker.operations import EmptyParams, OperationRegistry, PingResult
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.server import AUDIT_LOGGER_NAME
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import receive_frame, send_frame


def raw_request(operation: str = "ping") -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": operation,
            "params": {},
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


def audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == AUDIT_LOGGER_NAME
        and record.getMessage() == "broker request completed"
    ]


def test_unauthorized_request_produces_bounded_audit_event(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    with run_broker(socket_config, expected_uid=os.getuid() + 1):
        raw_round_trip(socket_config.socket_path, raw_request())

    records = audit_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.broker_peer_uid == os.getuid()  # type: ignore[attr-defined]
    assert record.broker_result_code == "unauthorized_peer"  # type: ignore[attr-defined]
    assert record.broker_request_id is None  # type: ignore[attr-defined]


def test_malformed_request_body_is_never_logged(
    socket_config: SocketPathConfig,
    run_broker: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
    marker = "DO-NOT-LOG-THIS-RAW-PAYLOAD"
    with run_broker(socket_config):
        raw_round_trip(socket_config.socket_path, ("{" + marker).encode())
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert marker not in combined
    assert audit_records(caplog)[0].broker_result_code == "malformed_request"  # type: ignore[attr-defined]


def test_handler_exception_does_not_leak_through_protocol(
    socket_config: SocketPathConfig,
    run_broker: Any,
) -> None:
    registry = OperationRegistry()

    def fail(_params: EmptyParams) -> PingResult:
        raise RuntimeError("PRIVATE-HANDLER-DETAIL")

    registry.register(
        name="failing",
        params_model=EmptyParams,
        result_model=PingResult,
        handler=fail,
    )
    with run_broker(socket_config, registry=registry):
        response = raw_round_trip(socket_config.socket_path, raw_request("failing"))

    serialized = json.dumps(response)
    assert "PRIVATE-HANDLER-DETAIL" not in serialized
    assert "Traceback" not in serialized
    error = response["error"]
    assert isinstance(error, dict)
    assert error == {
        "code": "internal_error",
        "message": "The broker could not complete the request.",
    }


def test_remote_error_type_does_not_expose_internal_exception() -> None:
    error = RemoteBrokerError("internal_error", "Safe.", request_id=None)
    assert "PRIVATE" not in str(error)

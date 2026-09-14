"""Protocol and framing security tests."""

from __future__ import annotations

import json
import socket
import struct
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.broker.errors import (
    FrameTooLargeError,
    RequestError,
    TrailingFrameDataError,
    TruncatedFrameError,
)
from kodi_lab.broker.operations import PingResult
from kodi_lab.broker.protocol import (
    ErrorBody,
    ErrorResponse,
    Request,
    SuccessResponse,
    decode_request,
    encode_model,
    error_response,
    success_response,
)
from kodi_lab.broker.transport import (
    MAX_FRAME_PAYLOAD,
    receive_frame,
    reject_buffered_trailing_data,
    send_frame,
)


def request_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "version": 1,
        "request_id": str(uuid4()),
        "operation": "ping",
        "params": {},
    }
    document.update(overrides)
    return document


def encoded_request(**overrides: object) -> bytes:
    return json.dumps(request_document(**overrides)).encode()


def test_valid_framed_request_round_trip() -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        request = Request.model_validate(request_document())
        send_frame(sender, encode_model(request))
        assert decode_request(receive_frame(receiver)) == request
    finally:
        sender.close()
        receiver.close()


def test_valid_success_response() -> None:
    request_id = str(uuid4())
    response = success_response(
        request_id, PingResult(status="healthy", protocol_version=1)
    )
    assert SuccessResponse.model_validate_json(encode_model(response)).ok is True


def test_structured_error_response() -> None:
    request_id = str(uuid4())
    response = error_response(
        RequestError("unknown_operation", "Unavailable.", request_id=request_id)
    )
    decoded = ErrorResponse.model_validate_json(encode_model(response))
    assert decoded == ErrorResponse(
        version=1,
        request_id=request_id,
        ok=False,
        error=ErrorBody(code="unknown_operation", message="Unavailable."),
    )


def test_oversized_advertised_frame_rejected_before_payload_read() -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sender.sendall(struct.pack("!I", MAX_FRAME_PAYLOAD + 1))
        with pytest.raises(FrameTooLargeError):
            receive_frame(receiver)
    finally:
        sender.close()
        receiver.close()


def test_trailing_bytes_after_single_frame_are_rejected() -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        send_frame(sender, b"{}")
        sender.sendall(b"second-frame-or-garbage")
        assert receive_frame(receiver) == b"{}"
        with pytest.raises(TrailingFrameDataError):
            reject_buffered_trailing_data(receiver)
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize(
    "wire",
    [b"\x00\x00", struct.pack("!I", 10) + b"short"],
)
def test_truncated_frame(wire: bytes) -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sender.sendall(wire)
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(TruncatedFrameError):
            receive_frame(receiver)
    finally:
        sender.close()
        receiver.close()


@pytest.mark.parametrize("payload", [b"\xff", b"{", b"[]", b'{"x":NaN}'])
def test_invalid_utf8_or_json_is_malformed(payload: bytes) -> None:
    with pytest.raises(RequestError) as raised:
        decode_request(payload)
    assert raised.value.code == "malformed_request"


def test_duplicate_json_key_is_rejected() -> None:
    payload = b'{"version":1,"version":1}'
    with pytest.raises(RequestError) as raised:
        decode_request(payload)
    assert raised.value.code == "malformed_request"


def test_unsupported_protocol_version_preserves_safe_request_id() -> None:
    request_id = str(uuid4())
    with pytest.raises(RequestError) as raised:
        decode_request(encoded_request(version=2, request_id=request_id))
    assert raised.value.code == "unsupported_version"
    assert raised.value.request_id == request_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": "not-a-uuid"},
        {"request_id": str(uuid4()).upper()},
        {"unexpected": True},
        {"operation": ""},
        {"operation": "Ping"},
        {"operation": "a" * 65},
        {"operation": "../ping"},
        {"version": True},
    ],
)
def test_invalid_request_schema(overrides: dict[str, object]) -> None:
    with pytest.raises(RequestError) as raised:
        decode_request(encoded_request(**overrides))
    assert raised.value.code == "malformed_request"


@pytest.mark.parametrize("missing", ["version", "request_id", "operation", "params"])
def test_missing_request_field(missing: str) -> None:
    document = request_document()
    del document[missing]
    with pytest.raises(RequestError) as raised:
        decode_request(json.dumps(document).encode())
    assert raised.value.code == "malformed_request"


def test_response_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SuccessResponse.model_validate(
            {
                "version": 1,
                "request_id": str(uuid4()),
                "ok": True,
                "result": {},
                "extra": "forbidden",
            }
        )

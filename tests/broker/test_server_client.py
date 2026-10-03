"""Real Unix broker/client integration and hostile-response tests."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.broker.authorization import PeerCredentials
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.errors import (
    BrokerAuthorizationError,
    BrokerUnavailableError,
    ClientProtocolError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    BrokerInfoResult,
    CapabilitiesResult,
    PingResult,
)
from kodi_lab.broker.protocol import decode_json_object
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.broker.transport import MAX_FRAME_PAYLOAD, receive_frame, send_frame


def raw_request(operation: str = "ping", params: object = None) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "request_id": str(uuid4()),
            "operation": operation,
            "params": {} if params is None else params,
        }
    ).encode()


def raw_round_trip(path: Path, payload: bytes) -> dict[str, object]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(2)
        connection.connect(str(path))
        try:
            send_frame(connection, payload)
        except (BrokenPipeError, ConnectionResetError):
            # Authorization precedes request reads, so a denied peer may have a
            # complete response waiting after the broker closes its read side.
            pass
        else:
            connection.shutdown(socket.SHUT_WR)
        return decode_json_object(receive_frame(connection))
    finally:
        connection.close()


def test_real_temporary_unix_server_client_round_trip(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    with run_broker(socket_config):
        client = BrokerClient(socket_config.socket_path)
        assert client.ping() == PingResult(status="healthy", protocol_version=1)
        assert isinstance(client.broker_info(), BrokerInfoResult)
        assert client.capabilities() == CapabilitiesResult(
            operations=[
                "active_reviewed_runtime_installation",
                "apply_addon_zip_installation",
                "apply_assigned_fixture",
                "apply_managed_instance_snapshot",
                "apply_repository_replacement",
                "broker_info",
                "capabilities",
                "capture_managed_instance_snapshot",
                "capture_managed_kodi_screenshot",
                "capture_promotion_kodi_log_cursor",
                "capture_smoke_log_cursor",
                "copy_sync_attached_development_source",
                "copy_sync_mcp_workspace",
                "copy_sync_promoted_development_source",
                "delete_managed_snapshot_artifact",
                "deprovision_managed_container",
                "inspect_repository_replacement",
                "launch_reviewed_runtime_installation",
                "managed_container_inventory",
                "observe_addon_zip_destination",
                "observe_managed_kodi_runtime",
                "observe_reviewed_runtime_installation",
                "ping",
                "provision_managed_container",
                "read_promotion_kodi_log_window",
                "read_smoke_log_window",
                "reviewed_runtime_image_present",
                "start_managed_container",
                "stop_managed_container",
                "verify_addon_zip_enablement_destination",
                "verify_addon_zip_smoke_destination",
                "verify_attached_development_source_destination",
                "verify_mcp_workspace_destination",
            ],
            protocol_version=1,
        )
    assert not socket_config.socket_path.exists()


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"{", "malformed_request"),
        (b"\xff", "malformed_request"),
        (raw_request("unknown_safe_name"), "unknown_operation"),
        (raw_request("ping", {"unexpected": True}), "invalid_params"),
        (
            json.dumps(
                {
                    "version": 2,
                    "request_id": str(uuid4()),
                    "operation": "ping",
                    "params": {},
                }
            ).encode(),
            "unsupported_version",
        ),
    ],
)
def test_server_returns_structured_safe_errors(
    socket_config: SocketPathConfig,
    run_broker: Any,
    payload: bytes,
    expected_code: str,
) -> None:
    with run_broker(socket_config):
        response = raw_round_trip(socket_config.socket_path, payload)
    assert response["ok"] is False
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == expected_code
    assert "traceback" not in json.dumps(response).lower()


def test_oversized_request_header_gets_error_without_body_read(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    with run_broker(socket_config):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(socket_config.socket_path))
            connection.sendall(struct.pack("!I", MAX_FRAME_PAYLOAD + 1))
            response = decode_json_object(receive_frame(connection))
        finally:
            connection.close()
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "request_too_large"


def test_server_rejects_trailing_data_after_request_frame(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    payload = raw_request()
    with run_broker(socket_config):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(socket_config.socket_path))
            connection.sendall(struct.pack("!I", len(payload)) + payload + b"trailing")
            connection.shutdown(socket.SHUT_WR)
            response = decode_json_object(receive_frame(connection))
        finally:
            connection.close()
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "malformed_request"


def test_unauthorized_uid_is_rejected_before_fake_identity_body(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    with run_broker(socket_config, expected_uid=os.getuid() + 10_000):
        response = raw_round_trip(
            socket_config.socket_path,
            raw_request("ping", {"uid": os.getuid() + 10_000, "gid": os.getgid()}),
        )
    assert response["request_id"] is None
    error = response["error"]
    assert isinstance(error, dict)
    assert error["code"] == "unauthorized_peer"


def test_peer_pid_and_gid_cannot_grant_authorization(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    def wrong_uid(_connection: socket.socket) -> PeerCredentials:
        return PeerCredentials(pid=os.getpid(), uid=os.getuid() + 1, gid=os.getuid())

    with (
        run_broker(socket_config, credential_extractor=wrong_uid),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).ping()


def test_peer_credential_failure_denies_connection(
    socket_config: SocketPathConfig, run_broker: Any
) -> None:
    def unavailable(_connection: socket.socket) -> PeerCredentials:
        raise OSError("unavailable")

    with (
        run_broker(socket_config, credential_extractor=unavailable),
        pytest.raises(BrokerAuthorizationError),
    ):
        BrokerClient(socket_config.socket_path).ping()


def test_broker_unavailable(tmp_path: Path) -> None:
    with pytest.raises(BrokerUnavailableError):
        BrokerClient(tmp_path / "absent.sock", connect_timeout=0.1).ping()


type ResponseWireBuilder = Callable[[dict[str, object]], bytes]


@contextmanager
def fake_broker(path: Path, builder: ResponseWireBuilder) -> Iterator[None]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                request = decode_json_object(receive_frame(connection))
                connection.sendall(builder(request))
        except BaseException as error:
            failures.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=2)
        listener.close()
        if path.exists():
            path.unlink()
        assert not thread.is_alive()
        assert not failures


def framed_json(document: dict[str, object]) -> bytes:
    payload = json.dumps(document).encode()
    return struct.pack("!I", len(payload)) + payload


def test_client_rejects_malformed_response(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"
    with (
        fake_broker(path, lambda _request: struct.pack("!I", 1) + b"{"),
        pytest.raises(ClientProtocolError, match="valid JSON"),
    ):
        BrokerClient(path).ping()


def test_client_rejects_mismatched_response_id(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"

    def response(_request: dict[str, object]) -> bytes:
        return framed_json(
            {
                "version": 1,
                "request_id": str(uuid4()),
                "ok": True,
                "result": {"status": "healthy", "protocol_version": 1},
            }
        )

    with (
        fake_broker(path, response),
        pytest.raises(ClientProtocolError, match="request ID"),
    ):
        BrokerClient(path).ping()


def test_client_rejects_unsupported_response_version(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"

    def response(request: dict[str, object]) -> bytes:
        return framed_json(
            {
                "version": 2,
                "request_id": request["request_id"],
                "ok": True,
                "result": {},
            }
        )

    with (
        fake_broker(path, response),
        pytest.raises(ClientProtocolError, match="unsupported version"),
    ):
        BrokerClient(path).ping()


@pytest.mark.parametrize(
    "builder",
    [
        lambda _request: struct.pack("!I", 10) + b"{}",
        lambda _request: struct.pack("!I", MAX_FRAME_PAYLOAD + 1),
        lambda _request: (
            framed_json(
                {
                    "version": 1,
                    "request_id": str(uuid4()),
                    "ok": True,
                    "result": {"status": "healthy", "protocol_version": 1},
                }
            )
            + b"trailing"
        ),
    ],
)
def test_client_rejects_truncated_or_oversized_response(
    tmp_path: Path, builder: ResponseWireBuilder
) -> None:
    path = tmp_path / "fake.sock"
    with fake_broker(path, builder), pytest.raises(ClientProtocolError):
        BrokerClient(path).ping()


def test_client_translates_structured_authorization_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"

    def response(_request: dict[str, object]) -> bytes:
        return framed_json(
            {
                "version": 1,
                "request_id": None,
                "ok": False,
                "error": {
                    "code": "unauthorized_peer",
                    "message": "The local peer is not authorized.",
                },
            }
        )

    with fake_broker(path, response), pytest.raises(BrokerAuthorizationError):
        BrokerClient(path).ping()


def test_client_translates_other_remote_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"

    def response(request: dict[str, object]) -> bytes:
        return framed_json(
            {
                "version": 1,
                "request_id": request["request_id"],
                "ok": False,
                "error": {"code": "internal_error", "message": "Safe failure."},
            }
        )

    with fake_broker(path, response), pytest.raises(RemoteBrokerError) as raised:
        BrokerClient(path).ping()
    assert raised.value.code == "internal_error"


def test_client_rejects_missing_recoverable_error_request_id(tmp_path: Path) -> None:
    path = tmp_path / "fake.sock"

    def response(_request: dict[str, object]) -> bytes:
        return framed_json(
            {
                "version": 1,
                "request_id": None,
                "ok": False,
                "error": {"code": "internal_error", "message": "Safe failure."},
            }
        )

    with (
        fake_broker(path, response),
        pytest.raises(ClientProtocolError, match="recoverable request ID"),
    ):
        BrokerClient(path).ping()

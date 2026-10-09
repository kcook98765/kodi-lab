"""Real disposable sockets exercise the root-denial health contract and deadline."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kodi_lab import broker_health as health
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.operations import PingResult
from kodi_lab.broker.socket_path import SocketPathConfig


def _denial(request_id: str | None = None) -> dict[str, object]:
    return {
        "version": 1,
        "request_id": request_id,
        "ok": False,
        "error": {"code": "unauthorized_peer", "message": "The peer is denied."},
    }


def _frame(payload: bytes) -> bytes:
    return struct.pack("!I", len(payload)) + payload


@contextmanager
def _peer(
    path: Path,
    builder: Callable[[dict[str, Any]], bytes],
    *,
    stall: bool = False,
    drip: bool = False,
) -> Iterator[Path]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(3)
    done = threading.Event()
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                header = health._receive_exact(connection, 4, time.monotonic() + 3)
                (size,) = struct.unpack("!I", header)
                body = health._receive_exact(connection, size, time.monotonic() + 3)
                request = json.loads(body)
                assert set(request) == {"version", "request_id", "operation", "params"}
                assert request["version"] == 1
                assert request["operation"] == "ping" and request["params"] == {}
                if stall:
                    done.wait(3)
                    return
                response = builder(request)
                if drip:
                    for byte in response:
                        if done.wait(0.025):
                            break
                        connection.sendall(bytes([byte]))
                else:
                    connection.sendall(response)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the probe rejects an oversized or timed-out peer.
        except BaseException as error:
            failures.append(error)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        done.set()
        thread.join(timeout=4)
        listener.close()
        path.unlink(missing_ok=True)
        assert not thread.is_alive()
        assert not failures


def _check(path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    # Only the local probe's UID guard is simulated. Broker SO_PEERCRED is real.
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(health, "SOCKET_PATH", path)
    monkeypatch.setattr(sys, "argv", ["kodi-lab-broker-healthcheck"])
    return health.main()


def test_real_unauthorized_peer_is_live_health(
    socket_config: SocketPathConfig, run_broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with run_broker(socket_config, expected_uid=os.getuid() + 10_000):
        assert _check(socket_config.socket_path, monkeypatch) == 0


def test_authorized_ping_is_unchanged_but_root_success_is_unhealthy(
    socket_config: SocketPathConfig, run_broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with run_broker(socket_config):
        assert BrokerClient(socket_config.socket_path).ping() == PingResult(
            status="healthy", protocol_version=1
        )
        assert _check(socket_config.socket_path, monkeypatch) == 1


@pytest.mark.parametrize("matching_id", [False, True])
def test_complete_denial_accepts_only_null_or_matching_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching_id: bool
) -> None:
    with _peer(
        tmp_path / "peer.sock",
        lambda request: _frame(
            json.dumps(_denial(request["request_id"] if matching_id else None)).encode()
        ),
    ) as path:
        assert _check(path, monkeypatch) == 0


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "boolean-version",
        "float-version",
        "response-id",
        "ok-integer",
        "root-success",
        "wrong-error",
        "extra-field",
        "missing-field",
        "error-extra",
        "empty-message",
        "long-message",
        "message-type",
        "error-type",
        "not-object",
        "duplicate-key",
        "duplicate-error-key",
        "nan",
        "infinity",
        "utf8",
        "json",
        "oversized",
        "truncated-header",
        "truncated-body",
        "missing-response",
        "trailing",
    ],
)
def test_hostile_response_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    def build(request: dict[str, Any]) -> bytes:
        document: dict[str, Any] = _denial()
        if case == "version":
            document["version"] = 2
        elif case == "boolean-version":
            document["version"] = True
        elif case == "float-version":
            document["version"] = 1.0
        elif case == "response-id":
            document["request_id"] = "00000000-0000-0000-0000-000000000000"
        elif case == "ok-integer":
            document["ok"] = 0
        elif case == "root-success":
            document = {
                "version": 1,
                "request_id": request["request_id"],
                "ok": True,
                "result": {"status": "healthy", "protocol_version": 1},
            }
        elif case == "wrong-error":
            document["error"]["code"] = "internal_error"
        elif case == "extra-field":
            document["secret"] = "must never be logged"
        elif case == "missing-field":
            del document["request_id"]
        elif case == "error-extra":
            document["error"]["extra"] = 1
        elif case == "empty-message":
            document["error"]["message"] = ""
        elif case == "long-message":
            document["error"]["message"] = "x" * 257
        elif case == "message-type":
            document["error"]["message"] = 1
        elif case == "error-type":
            document["error"] = []
        elif case == "not-object":
            return _frame(b"[]")
        elif case == "duplicate-key":
            return _frame(
                json.dumps(document)
                .replace('"version": 1', '"version": 2, "version": 1')
                .encode()
            )
        elif case == "duplicate-error-key":
            return _frame(
                json.dumps(document)
                .replace(
                    '"code": "unauthorized_peer"',
                    '"code": "internal_error", "code": "unauthorized_peer"',
                )
                .encode()
            )
        elif case in ("nan", "infinity"):
            document["error"]["message"] = float("nan" if case == "nan" else "inf")
        elif case == "utf8":
            return _frame(b"\xff")
        elif case == "json":
            return _frame(b"{")
        elif case == "oversized":
            return struct.pack("!I", health.MAX_FRAME_PAYLOAD + 1)
        elif case == "truncated-header":
            return b"\x00\x00"
        elif case == "truncated-body":
            return struct.pack("!I", 100) + b"{}"
        elif case == "missing-response":
            return b""
        response = _frame(json.dumps(document).encode())
        return response + b"trailing" if case == "trailing" else response

    with _peer(tmp_path / "peer.sock", build) as path:
        started = time.monotonic()
        assert _check(path, monkeypatch) == 1
        assert time.monotonic() - started < 1.5


def test_missing_and_refused_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "absent.sock"
    assert _check(path, monkeypatch) == 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
    assert _check(path, monkeypatch) == 1


def test_stalled_listener_obeys_real_two_second_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _peer(tmp_path / "peer.sock", lambda _request: b"", stall=True) as path:
        started = time.monotonic()
        assert _check(path, monkeypatch) == 1
        elapsed = time.monotonic() - started
        assert 1.8 <= elapsed < 2.8


def test_drip_feed_cannot_extend_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "RESPONSE_DEADLINE_SECONDS", 0.15)
    with _peer(
        tmp_path / "peer.sock",
        lambda _request: _frame(json.dumps(_denial()).encode()),
        drip=True,
    ) as path:
        started = time.monotonic()
        assert _check(path, monkeypatch) == 1
        assert 0.1 <= time.monotonic() - started < 0.65


def test_connect_and_reads_share_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowConnectSocket(socket.socket):
        def connect(self, address: Any) -> None:
            time.sleep(0.1)
            super().connect(address)

    monkeypatch.setattr(health, "RESPONSE_DEADLINE_SECONDS", 0.2)
    with _peer(tmp_path / "peer.sock", lambda _request: b"", stall=True) as path:
        monkeypatch.setattr(socket, "socket", SlowConnectSocket)
        started = time.monotonic()
        assert _check(path, monkeypatch) == 1
        assert 0.16 <= time.monotonic() - started < 0.27


def test_nonroot_and_arguments_fail_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_path: Path) -> None:
        pytest.fail("identity/argument failure must not contact the broker")

    monkeypatch.setattr(health, "_probe", unexpected)
    monkeypatch.setattr(sys, "argv", ["kodi-lab-broker-healthcheck"])
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    assert health.main() == 1
    monkeypatch.setattr(sys, "argv", ["kodi-lab-broker-healthcheck", "--socket=x"])
    assert health.main() == 2


def test_repeated_checks_close_all_descriptors(
    socket_config: SocketPathConfig, run_broker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = set(Path("/proc/self/fd").iterdir())
    with run_broker(socket_config, expected_uid=os.getuid() + 10_000):
        for _ in range(12):
            assert _check(socket_config.socket_path, monkeypatch) == 0
    # Join the server before sampling; its last accepted connection also owns an fd.
    assert set(Path("/proc/self/fd").iterdir()) == before


def test_fresh_import_avoids_broker_and_application_dependencies() -> None:
    code = """import sys
from kodi_lab import broker_health
assert 'kodi_lab.broker' not in sys.modules
assert not any(n.startswith('kodi_lab.broker.') for n in sys.modules)
assert not {'sqlalchemy', 'pydantic', 'docker'} & {n.split('.')[0] for n in sys.modules}
"""
    subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=3)

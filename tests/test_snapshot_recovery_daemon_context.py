from __future__ import annotations

import json
import multiprocessing
import os
import socket
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.transport import UnixHTTPAdapter  # type: ignore[import-untyped]

from kodi_lab import snapshot_recovery_host as host
from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.application.snapshot_recovery_maintenance import discover_state_consumers
from tests.snapshot_recovery_support import Case, ConsumerEngine
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case

RESPONSE = b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK"
SENDER = 191


def _activated_responder(listener: socket.socket, done: Any, split: bool) -> None:
    # Listener was created by the parent, while this child writes the response.
    for _ in range(2):
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(5)
            request = bytearray()
            while not request.endswith(b"\r\n\r\n") and len(request) < 4096:
                request.extend(connection.recv(4096))
            assert bytes(request) == b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n"
            if split:
                connection.sendall(RESPONSE[:-2])
                time.sleep(0.02)
                connection.sendall(RESPONSE[-2:])
            else:
                connection.sendall(RESPONSE)
    done.wait(5)


@pytest.mark.parametrize("split", [False, True])
def test_actual_unix_response_credentials_distinguish_listener_creator(
    tmp_path: Path, split: bool
) -> None:
    path = str(tmp_path / "s")
    context = multiprocessing.get_context("fork")
    done = context.Event()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(2)
        process = context.Process(
            target=_activated_responder, args=(listener, done, split)
        )
        process.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(path)
                creator, _, _ = struct.unpack(
                    "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                assert creator == os.getpid() != process.pid
                client.sendall(b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
                while client.recv(4096):
                    pass
            sender, observed = host._ping_response_sender(path)
            assert sender == (process.pid, os.getuid(), os.getgid())
            assert observed["pid"] == process.pid
            assert observed["root_inode"] == Path("/").stat().st_ino
            assert observed["pid_namespace"] == os.readlink("/proc/self/ns/pid")
            assert observed["mount_namespace"] == os.readlink("/proc/self/ns/mnt")
        finally:
            done.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        assert process.exitcode == 0


class EndpointEngine(ConsumerEngine):
    def __init__(self, root: Path, endpoint: str) -> None:
        super().__init__(root)
        self.base_url = "http+docker://localhost"
        self.adapter = UnixHTTPAdapter("http+unix://" + endpoint)
        self.adapters: dict[str, Any] = {"http+docker://": self.adapter}
        self.info_calls = 0
        self.fault = "none"
        self.closed = False

    def get_adapter(self, _url: str) -> Any:
        return self.adapters["http+docker://"]

    def info(self) -> dict[str, str]:
        self.info_calls += 1
        if self.fault == "engine" or (
            self.fault == "engine_drift" and self.info_calls > 1
        ):
            return {"ID": "wrong-engine"}
        return super().info()


def _synthetic_observation(pid: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "start": "123456",
        "executable": "/usr/bin/dockerd" if pid == SENDER else "/usr/bin/python3",
        "pid_namespace": "pid:[123]",
        "mount_namespace": "mnt:[456]",
        "root_device": 20,
        "root_inode": 2,
        "mount_table_digest": "a" * 64,
    }


def _install_observation_inputs(
    monkeypatch: pytest.MonkeyPatch, sdk: EndpointEngine, path: Path, fault: str
) -> list[bytes]:
    """Replace transport/proc observations only, never the rejecting guard."""
    requests: list[bytes] = []
    observations: dict[int, int] = {}

    def process_view(pid: int) -> dict[str, Any]:
        observations[pid] = observations.get(pid, 0) + 1
        row = _synthetic_observation(pid)
        if pid == SENDER:
            changes = {
                "pid_namespace": ("pid_namespace", "pid:[other]"),
                "mount_namespace": ("mount_namespace", "mnt:[other]"),
                "root_device": ("root_device", 99),
                "root_inode": ("root_inode", 99),
                "mount_table": ("mount_table_digest", "b" * 64),
                "proxy": ("executable", "/usr/bin/docker-proxy"),
            }
            if fault in changes:
                key, value = changes[fault]
                row[key] = value
            if fault == "unreadable_proc":
                raise PermissionError("synthetic inaccessible proc")
            if observations[pid] > 1:
                if fault == "process_start_drift":
                    row["start"] = "reused-pid"
                if fault == "daemon_mount_drift":
                    row["mount_namespace"] = "mnt:[changed]"
                if fault == "daemon_topology_drift":
                    row["mount_table_digest"] = "b" * 64
        elif fault == "helper_root_drift" and observations[pid] > 1:
            row["root_inode"] = 99
        elif fault == "helper_topology_drift" and observations[pid] > 1:
            row["mount_table_digest"] = "b" * 64
        return row

    class ResponseSocket:
        def __init__(self, *_args: Any) -> None:
            self.index = 0

        def __enter__(self) -> ResponseSocket:
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 5

        def setsockopt(self, level: int, option: int, value: int) -> None:
            assert (level, option, value) == (socket.SOL_SOCKET, socket.SO_PASSCRED, 1)

        def connect(self, endpoint: str) -> None:
            assert endpoint == str(path)

        def sendall(self, request: bytes) -> None:
            requests.append(request)
            if fault == "socket_replaced":
                path.unlink()
                path.write_text("changed endpoint")
            if fault == "endpoint_drift":
                sdk.adapter.socket_path = str(path) + "-changed"

        def recvmsg(self, _size: int, _control_size: int) -> Any:
            if fault == "timeout":
                raise TimeoutError("synthetic no response")
            self.index += 1
            if self.index > 1 and fault not in {
                "sender_changed",
                "oversized",
                "fragments",
            }:
                return b"", [], 0, None
            uid = 1000 if fault == "nonroot" else 0
            pid = 0 if fault == "zero_pid" else SENDER
            if fault == "sender_changed" and self.index > 1:
                pid += 1
            credential = (
                socket.SOL_SOCKET,
                socket.SCM_CREDENTIALS,
                struct.pack("3i", pid, uid, 0),
            )
            ancillary = [credential]
            flags = 0
            if fault == "no_credentials":
                ancillary = []
            if fault == "duplicate_credentials":
                ancillary *= 2
            if fault == "wrong_control":
                ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, credential[2])]
            if fault == "short_credentials":
                ancillary = [(credential[0], credential[1], b"short")]
            if fault == "truncated_control":
                flags = socket.MSG_CTRUNC
            if fault == "truncated_data":
                flags = socket.MSG_TRUNC
            data = RESPONSE
            if fault == "oversized":
                data = b"x" * 4096
            if fault == "fragments":
                data = b"x"
            if fault == "incomplete_http":
                data = RESPONSE[:-1]
            if fault == "chunked":
                data = RESPONSE.replace(
                    b"Content-Length: 2", b"Transfer-Encoding: chunked"
                )
            if fault == "duplicate_length":
                data = RESPONSE.replace(
                    b"Content-Length: 2", b"Content-Length: 2\r\nContent-Length: 2"
                )
            return data, ancillary, flags, None

    monkeypatch.setattr(host, "_process_filesystem_view", process_view)
    monkeypatch.setattr("socket.socket", ResponseSocket)
    return requests


@pytest.mark.parametrize("inspect", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "pid_namespace",
        "mount_namespace",
        "root_device",
        "root_inode",
        "mount_table",
        "proxy",
        "nonroot",
        "unreadable_proc",
        "process_start_drift",
        "daemon_mount_drift",
        "daemon_topology_drift",
        "helper_root_drift",
        "helper_topology_drift",
        "tcp",
        "ssh",
        "adapter",
        "regular_file",
        "engine",
        "engine_drift",
        "socket_replaced",
        "endpoint_drift",
        "no_credentials",
        "duplicate_credentials",
        "wrong_control",
        "short_credentials",
        "zero_pid",
        "truncated_control",
        "truncated_data",
        "sender_changed",
        "incomplete_http",
        "chunked",
        "duplicate_length",
        "oversized",
        "fragments",
        "timeout",
    ],
)
def test_installed_cli_refuses_context_before_classification_or_stop(
    recovery_case: Case, monkeypatch: pytest.MonkeyPatch, fault: str, inspect: bool
) -> None:
    case = recovery_case
    path = case.root / "daemon.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        sdk = EndpointEngine(case.root, str(path))
        sdk.fault = fault
        if fault in {"tcp", "ssh"}:
            sdk.base_url = (
                "tcp://localhost:2375" if fault == "tcp" else "http+docker://ssh"
            )
        if fault == "adapter":
            sdk.adapters["http+docker://"] = object()
        if fault == "regular_file":
            path.unlink()
            path.write_text("not a socket")
        requests = _install_observation_inputs(monkeypatch, sdk, path, fault)
        client = SimpleNamespace(api=sdk, close=lambda: setattr(sdk, "closed", True))
        monkeypatch.setattr(
            "kodi_lab.snapshot_recovery_host.docker.from_env", lambda: client
        )
        plan = host.InstallationPlan(
            installation="owned-qa",
            bundle_root=str(case.root),
            compose_file=str(case.root / "compose.yml"),
            engine_id="separate-synthetic-engine",
            state_volume="qa-state",
            web_container_id="c" * 64,
            broker_container_id="d" * 64,
            candidate_package_digest=case.seal.candidate_package_digest,
            review=case.review,
        )
        plan_path = case.root / "plan.json"
        plan_path.write_text(plan.model_dump_json())
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "maintenance",
                str(plan_path),
                *(["--inspect-consumers"] if inspect else []),
            ],
        )
        classifications = []
        actual = discover_state_consumers

        def classify(*args: Any, **kwargs: Any) -> Any:
            classifications.append(True)
            return actual(*args, **kwargs)

        monkeypatch.setattr(host, "discover_state_consumers", classify)
        with pytest.raises(RecoveryRefused):
            host.main()
        assert classifications == []
        assert sdk.stops == [] and case.provider.calls == []
        assert sdk.closed
        assert all(
            request == b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n"
            for request in requests
        )


@pytest.mark.parametrize("inspect", [False, True])
def test_installed_cli_derives_context_and_retains_seal_without_caller_override(
    recovery_case: Case,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inspect: bool,
) -> None:
    case = recovery_case
    path = case.root / "daemon.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        sdk = EndpointEngine(case.root, str(path))
        sdk.records = {
            "c" * 64: sdk.record("c" * 64, "web", running=True),
            "d" * 64: sdk.record("d" * 64, "broker", running=True),
        }
        requests = _install_observation_inputs(monkeypatch, sdk, path, "none")
        client = SimpleNamespace(api=sdk, close=lambda: setattr(sdk, "closed", True))
        monkeypatch.setattr(
            "kodi_lab.snapshot_recovery_host.docker.from_env", lambda: client
        )
        plan = host.InstallationPlan(
            installation="owned-qa",
            bundle_root=str(case.root),
            compose_file=str(case.root / "compose.yml"),
            engine_id="separate-synthetic-engine",
            state_volume="qa-state",
            web_container_id="c" * 64,
            broker_container_id="d" * 64,
            candidate_package_digest=case.seal.candidate_package_digest,
            review=case.review,
        )
        plan_path = case.root / "plan.json"
        plan_path.write_text(plan.model_dump_json())
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "maintenance",
                str(plan_path),
                *(["--inspect-consumers"] if inspect else []),
            ],
        )
        host.main()
        result = json.loads(capsys.readouterr().out)
        if inspect:
            assert len(result["consumers"]) == 2 and sdk.stops == []
        else:
            assert sdk.stops == ["c" * 64, "d" * 64]
            proof = result["daemon_filesystem_context"]
            assert proof["sender_uid"] == 0 and proof["daemon"]["pid"] == SENDER
            assert "daemon.sock" not in json.dumps(proof)
            assert (
                proof["daemon"]["mount_namespace"] == proof["helper"]["mount_namespace"]
            )
        assert sdk.closed and len(requests) == 2

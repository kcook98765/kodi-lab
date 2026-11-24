"""Offline host observation of two exact application services; never managed Kodi.

The approved local plan is infrastructure authority, NOT a Web API. Service
control uses the same verified SDK Engine and exact CIDs. No force/kill timeout,
container creation, native profile access, archive extraction or cleanup exists.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import math
import os
import re
import socket
import stat
import struct
import tarfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

import docker  # type: ignore[import-untyped]
from docker.transport import UnixHTTPAdapter  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from kodi_lab.application.snapshot_recovery_evidence import (
    LegacyCaptureReview,
    RecoveryRefused,
    broker_event,
    digest,
)
from kodi_lab.application.snapshot_recovery_maintenance import (
    UVICORN_0530_SOURCES,
    InertStateConsumer,
    RecoveryDrainSeal,
    ReviewedStateConsumer,
    ServiceRetirement,
    StateConsumerProof,
    WebGracefulSigtermProof,
    consumer_metadata_digest,
    discover_state_consumers,
    require_inert_consumer,
    service_binding,
    volume_identity,
)


class InstallationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation: str = Field(min_length=1, max_length=256)
    bundle_root: str
    compose_file: str
    engine_id: str
    state_volume: str
    web_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    broker_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review: LegacyCaptureReview
    owners: dict[str, str] = Field(default_factory=dict, max_length=2)
    ancillary_consumers: tuple[ReviewedStateConsumer, ...] = Field(
        default=(), max_length=32
    )


def _process_filesystem_view(pid: int) -> dict[str, Any]:
    """Kernel observations in this proc view, with PID-reuse protection."""
    proc = Path("/proc/self" if pid == os.getpid() else f"/proc/{pid}")
    identity = (proc / "stat").read_text()
    if identity.split(" ", 1)[0] != str(pid):
        raise RecoveryRefused("recovery_daemon_pid_view_unproven")
    before = identity.rsplit(")", 1)[1].split()[19]
    root = (proc / "root").stat()
    with (proc / "mountinfo").open("rb") as stream:
        mounts = stream.read(1024 * 1024 + 1)
    if not mounts or len(mounts) > 1024 * 1024:
        raise RecoveryRefused("recovery_daemon_mount_view_unproven")
    observed = {
        "pid": pid,
        "start": before,
        "executable": os.readlink(proc / "exe"),
        "pid_namespace": os.readlink(proc / "ns/pid"),
        "mount_namespace": os.readlink(proc / "ns/mnt"),
        "root_device": root.st_dev,
        "root_inode": root.st_ino,
        "mount_table_digest": hashlib.sha256(mounts).hexdigest(),
    }
    if (proc / "stat").read_text().rsplit(")", 1)[1].split()[19] != before:
        raise RecoveryRefused("recovery_daemon_process_changed")
    return observed


def _ping_response_sender(path: str) -> tuple[tuple[int, int, int], dict[str, Any]]:
    """Read only /_ping; SCM_CREDENTIALS names the writer, not listener creator.

    SO_PEERCRED can name systemd PID1 for socket-activated dockerd. Require one
    consistent kernel credential on every response fragment, bounded bytes/time,
    and a stable process view across response acquisition instead.
    """
    credentials: tuple[int, int, int] | None = None
    observed: dict[str, Any] | None = None
    raw = bytearray()
    credential_size = struct.calcsize("3i")
    deadline = time.monotonic() + 5
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        connection.connect(path)
        connection.sendall(b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
        for _ in range(64):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
            connection.settimeout(remaining)
            data, ancillary, flags, _ = connection.recvmsg(
                4096, socket.CMSG_SPACE(credential_size)
            )
            if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
                raise RecoveryRefused("recovery_daemon_sender_unproven")
            if not data:
                break
            if (
                len(ancillary) != 1
                or ancillary[0][:2] != (socket.SOL_SOCKET, socket.SCM_CREDENTIALS)
                or len(ancillary[0][2]) != credential_size
            ):
                raise RecoveryRefused("recovery_daemon_sender_unproven")
            pid, uid, gid = struct.unpack("3i", ancillary[0][2])
            sender = (pid, uid, gid)
            if pid <= 0 or uid < 0 or gid < 0:
                raise RecoveryRefused("recovery_daemon_sender_unproven")
            if credentials is None:
                credentials = sender
                observed = _process_filesystem_view(pid)
            elif sender != credentials:
                raise RecoveryRefused("recovery_daemon_sender_changed")
            raw.extend(data)
            if len(raw) > 16384:
                raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
        else:
            raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
    headers, separator, body = bytes(raw).partition(b"\r\n\r\n")
    lines = headers.split(b"\r\n")
    if (
        not separator
        or lines[0] not in (b"HTTP/1.0 200 OK", b"HTTP/1.1 200 OK")
        or body != b"OK"
        or credentials is None
        or observed is None
    ):
        raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
    lengths = []
    for line in lines[1:]:
        key, colon, value = line.partition(b":")
        if not colon or key.lower() == b"transfer-encoding":
            raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
        if key.lower() == b"content-length":
            lengths.append(value.strip())
    if lengths not in ([], [b"2"]):
        raise RecoveryRefused("recovery_daemon_ping_unconfirmed")
    if _process_filesystem_view(credentials[0]) != observed:
        raise RecoveryRefused("recovery_daemon_process_changed")
    return credentials, observed


def _daemon_filesystem_context(client: Any, engine_id: str) -> dict[str, Any]:
    """Derive local direct-dockerd context from the exact SDK Unix endpoint.

    This is only a CLI observation, never caller-supplied authority. Bind-alias
    resolution requires the daemon's filesystem root and PID/mount namespaces.
    """
    try:
        if client.base_url != "http+docker://localhost" or set(client.adapters) != {
            "http+docker://"
        }:
            raise RecoveryRefused("recovery_daemon_endpoint_unsupported")
        adapter = client.get_adapter(client.base_url + "/")
        if type(adapter) is not UnixHTTPAdapter:
            raise RecoveryRefused("recovery_daemon_endpoint_unsupported")
        endpoint = adapter.socket_path
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            raise RecoveryRefused("recovery_daemon_endpoint_unsupported")
        path = Path(endpoint)
        before = path.stat()
        if not stat.S_ISSOCK(before.st_mode):
            raise RecoveryRefused("recovery_daemon_endpoint_unsupported")
        helper = _process_filesystem_view(os.getpid())
        if client.info()["ID"] != engine_id:
            raise RecoveryRefused("recovery_installation_identity_mismatch")
        (pid, uid, gid), daemon = _ping_response_sender(endpoint)
        if (
            uid != 0
            or daemon["pid"] != pid
            or daemon["executable"]
            not in {"/usr/bin/dockerd", "/usr/local/bin/dockerd"}
            or any(
                daemon[key] != helper[key]
                for key in (
                    "pid_namespace",
                    "mount_namespace",
                    "root_device",
                    "root_inode",
                    "mount_table_digest",
                )
            )
        ):
            raise RecoveryRefused("recovery_daemon_filesystem_context_unproven")
        after = path.stat()
        if (
            (before.st_dev, before.st_ino, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_mode)
            or client.base_url != "http+docker://localhost"
            or set(client.adapters) != {"http+docker://"}
            or adapter.socket_path != endpoint
            or client.get_adapter(client.base_url + "/") is not adapter
            or client.info()["ID"] != engine_id
            or _process_filesystem_view(pid) != daemon
            or _process_filesystem_view(os.getpid()) != helper
        ):
            raise RecoveryRefused("recovery_daemon_context_changed")
        return {
            "engine_id": engine_id,
            "socket_identity_digest": digest(
                {"endpoint": endpoint, "device": before.st_dev, "inode": before.st_ino}
            ),
            "sender_uid": uid,
            "sender_gid": gid,
            "daemon": daemon,
            "helper": helper,
        }
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as error:
        raise RecoveryRefused("recovery_daemon_filesystem_context_unproven") from error


LEGACY_303_BROKER_SOURCES = {
    "authorization.py": (
        "7e0a36821437f12c1798f0b0268adba62fb352db9e48474102459a2122e4defe"
    ),
    "docker_inventory.py": (
        "841438315e9a44306be05fba368739b57d2549bc923913237d7554cc8b716741"
    ),
    "managed_capacity.py": (
        "d4ec41e6867b240106f3396a471e17b6fb49d887412eb2c52210fa218c24822a"
    ),
    "operations.py": "91e1d27f0bb04d0e40458463be2c95bea53476f9fa229e051dcabb8c52da0b3c",
    "run.py": "1555e705c433c633a99d469ac1b9a5f73664f25b8420c18fda81286554cb84cc",
    "server.py": "ea46e41d0217585c90e9e84cdcc4d47a34c72bfa0104f8f5bcdcf0d1e0a8795f",
}


class BrokerLogPolicy(BaseModel):
    """Actual read-only source/entrypoint/passwd identity, not an allow-errors flag."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    dedicated_uid: int
    dedicated_gid: int
    entrypoint: tuple[str, ...]
    image_id: str
    passwd_digest: str
    source_digests: dict[str, str]

    def require_supported(self) -> None:
        current = {
            name: hashlib.sha256(
                resources.files("kodi_lab").joinpath("broker", name).read_bytes()
            ).hexdigest()
            for name in LEGACY_303_BROKER_SOURCES
        }
        if (
            self.dedicated_uid != 10001
            or self.dedicated_gid != 10001
            or self.entrypoint != ("kodi-lab-broker",)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", self.passwd_digest) is None
            or self.source_digests not in (LEGACY_303_BROKER_SOURCES, current)
        ):
            raise RecoveryRefused("recovery_broker_log_policy_unproven")


def _metadata_file(client: Any, cid: str, path: str) -> bytes:
    chunks, _ = client.get_archive(cid, path)
    data = bytearray()
    for chunk in chunks:
        data.extend(chunk)
        if len(data) > 2 * 1024 * 1024:
            raise RecoveryRefused("recovery_broker_policy_metadata_too_large")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        if (
            len(members) != 1
            or not members[0].isfile()
            or members[0].name != path.rsplit("/", 1)[1]
        ):
            raise RecoveryRefused("recovery_broker_policy_metadata_invalid")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise RecoveryRefused("recovery_broker_policy_metadata_invalid")
        return stream.read(1024 * 1024)


_LEGACY_WEB_APPLICATION = {
    "admin_cli.py": "f5a5d4830c3194434c8e0e78d78a6a0131677dd2b4f91b1adca4e966f7024f56",
    "web/run.py": "28c156e335bd8415c1f81240fc2f671dca4f21f739357a65417e8f5295250eed",
}
_WEB_LAUNCHER = "09ac5a9a8dc0a91eb605b969318de0696fccd6960f5034dd5daefe9f22c0611c"


def _console_wrapper(raw: bytes, module: str) -> None:
    # Only fixed packaging-generated wrappers, not arbitrary code that happens
    # to import the right function. Ignore syntax locations, not statements.
    if not raw.startswith(b"#!/usr/local/bin/python\n"):
        raise RecoveryRefused("recovery_web_shutdown_stack_unproven")
    templates = [
        f"import sys\nfrom {module} import main\nif __name__ == '__main__':\n"
        "    sys.exit(main())\n",
        f"import sys\nfrom {module} import main\nif __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('.exe'):\n        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n",
        f"import re\nimport sys\nfrom {module} import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n",
    ]
    if ast.dump(ast.parse(raw)) not in {
        ast.dump(ast.parse(text)) for text in templates
    }:
        raise RecoveryRefused("recovery_web_shutdown_stack_unproven")


def _web_stack(client: Any, state: dict[str, Any]) -> dict[str, str]:
    config, host = state["Config"], state["HostConfig"]
    env = dict(value.split("=", 1) for value in config.get("Env", []) if "=" in value)
    if (
        config.get("Entrypoint") != ["/usr/local/bin/kodi-lab-remote-web"]
        or config.get("Cmd") not in (None, [])
        or config.get("WorkingDir") not in (None, "", "/")
        or config.get("StopSignal") not in (None, "", "SIGTERM", "15")
        or state.get("Path") != "/usr/local/bin/kodi-lab-remote-web"
        or state.get("Args") != []
        or host.get("ReadonlyRootfs") is not True
        or host.get("Init") is not True
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or state["RestartCount"] != 0
        or re.fullmatch(r"sha256:[0-9a-f]{64}", state["Image"]) is None
        or env.get("PATH")
        != "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        or any(
            key.startswith(("LD_", "UVICORN_"))
            or key
            in {
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONSTARTUP",
                "PYTHONUSERBASE",
                "PYTHONINSPECT",
                "WEB_CONCURRENCY",
            }
            for key in env
        )
        or any(
            mount["Destination"] == "/"
            or any(
                mount["Destination"] == path
                or path.startswith(mount["Destination"].rstrip("/") + "/")
                or mount["Destination"].startswith(path + "/")
                for path in ("/usr", "/bin", "/lib", "/etc")
            )
            for mount in state["Mounts"]
        )
    ):
        raise RecoveryRefused("recovery_web_shutdown_stack_unproven")
    paths = {
        "launcher": "/usr/local/bin/kodi-lab-remote-web",
        "admin_console": "/usr/local/bin/kodi-lab-admin",
        "web_console": "/usr/local/bin/kodi-lab-web",
        **{
            name: "/usr/local/lib/python3.12/site-packages/" + name
            for name in UVICORN_0530_SOURCES
        },
        **{
            name: "/usr/local/lib/python3.12/site-packages/kodi_lab/" + name
            for name in _LEGACY_WEB_APPLICATION
        },
    }
    files = {
        name: _metadata_file(client, state["Id"], path) for name, path in paths.items()
    }
    observed = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    current = {
        name: hashlib.sha256(
            resources.files("kodi_lab").joinpath(name).read_bytes()
        ).hexdigest()
        for name in _LEGACY_WEB_APPLICATION
    }
    if (
        observed["launcher"] != _WEB_LAUNCHER
        or any(observed[name] != value for name, value in UVICORN_0530_SOURCES.items())
        or {name: observed[name] for name in _LEGACY_WEB_APPLICATION}
        not in (_LEGACY_WEB_APPLICATION, current)
    ):
        raise RecoveryRefused("recovery_web_shutdown_stack_unproven")
    _console_wrapper(files["admin_console"], "kodi_lab.admin_cli")
    _console_wrapper(files["web_console"], "kodi_lab.web.run")
    return observed


def _utc_ns(value: str) -> int:
    matched = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z", value)
    if matched is None:
        raise RecoveryRefused("recovery_web_shutdown_time_unproven")
    seconds = int(
        datetime.strptime(matched[1], "%Y-%m-%dT%H:%M:%S")
        .replace(tzinfo=UTC)
        .timestamp()
    )
    return seconds * 1_000_000_000 + int((matched[2] or "").ljust(9, "0"))


def _web_log(raw: bytes, started_at: str) -> list[tuple[int, str]]:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > 4 * 1024 * 1024
        or not raw.endswith(b"\n")
    ):
        raise RecoveryRefused("recovery_web_shutdown_log_unproven")
    boot = _utc_ns(started_at)
    records: list[tuple[int, str]] = []
    for line in raw.decode().splitlines():
        stamp, separator, message = line.partition(" ")
        if not separator:
            raise RecoveryRefused("recovery_web_shutdown_log_unproven")
        when = _utc_ns(stamp)
        if when < boot or (records and when < records[-1][0]):
            raise RecoveryRefused("recovery_web_shutdown_log_unproven")
        if re.match(r"(?:ERROR|WARNING|CRITICAL):|Traceback", message):
            raise RecoveryRefused("recovery_web_shutdown_log_unproven")
        records.append((when, message))
    return records


def _web_start_pid(raw: bytes, started_at: str) -> int:
    records = _web_log(raw, started_at)
    starts = [
        (index, re.fullmatch(r"INFO:\s+Started server process \[(\d+)\]", message))
        for index, (_, message) in enumerate(records)
        if "server process" in message
    ]
    if len(starts) != 1 or starts[0][1] is None:
        raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
    index, matched = starts[0]
    assert matched is not None
    if (
        int(matched[1]) <= 1
        or [
            re.sub(r"^INFO:\s+", "", message)
            for _, message in records[index + 1 : index + 3]
        ]
        != ["Waiting for application startup.", "Application startup complete."]
        or any(
            "Shutting down" in message or "shutdown" in message.lower()
            for _, message in records
        )
    ):
        raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
    return int(matched[1])


def _web_shutdown_suffix(
    before: bytes,
    final: bytes,
    started_at: str,
    namespace_pid: int,
    stop_started_ns: int,
    finished_ns: int,
) -> tuple[int, int]:
    if not final.startswith(before):
        raise RecoveryRefused("recovery_web_shutdown_log_changed")
    records = _web_log(final, started_at)
    messages = [re.sub(r"^INFO:\s+", "", message) for _, message in records]
    suffix = [
        "Shutting down",
        "Waiting for application shutdown.",
        "Application shutdown complete.",
        f"Finished server process [{namespace_pid}]",
    ]
    if (
        messages[-4:] != suffix
        or sum("Started server process" in message for message in messages) != 1
        or sum("Finished server process" in message for message in messages) != 1
        or any(
            "shutdown" in message.lower() or "Shutting down" in message
            for message in messages[:-4]
        )
        or not stop_started_ns <= records[-4][0] <= records[-1][0] <= finished_ns
    ):
        raise RecoveryRefused("recovery_web_shutdown_completion_unproven")
    return records[-4][0], records[-1][0]


@dataclass(frozen=True)
class _WebStopEventEvidence:
    digest: str
    signal_ns: int
    stop_ns: int
    die_ns: int
    out_of_window_records: int
    noncontrol_records: int


def _web_stop_events(
    events: Any, cid: str, stop_started_ns: int, observation_until_ns: int
) -> _WebStopEventEvidence:
    """Exact-window lifecycle proof, NOT an absence-of-exec/dispatch claim.

    The API query is second-expanded. Preserve/digest it in full, but classify
    only the precise observed window. Exec/health observations establish no
    worker outcome here; physical retirement and final broker audit are separate
    mandatory proofs. Unknown lifecycle controls still refuse.
    """
    if (
        not isinstance(events, list)
        or len(events) > 128
        or len(json.dumps(events)) > 256 * 1024
    ):
        raise RecoveryRefused("recovery_web_stop_events_unproven")
    controls: dict[str, int] = {}
    outside = noncontrol = 0
    for entry in events:
        if not isinstance(entry, dict) or type(entry.get("timeNano")) is not int:
            raise RecoveryRefused("recovery_web_stop_events_unproven")
        stamp = entry["timeNano"]
        if not stop_started_ns <= stamp <= observation_until_ns:
            outside += 1
            continue
        if entry.get("Type") != "container" or entry.get("Actor", {}).get("ID") != cid:
            raise RecoveryRefused("recovery_web_stop_events_unproven")
        action = entry.get("Action")
        if not isinstance(action, str):
            raise RecoveryRefused("recovery_web_stop_events_unproven")
        if action in {
            "exec_create",
            "exec_start",
            "exec_die",
            "exec_detach",
        } or action.startswith(("exec_create: ", "exec_start: ", "health_status: ")):
            # Deliberately out of this control-only proof, NOT declared safe,
            # successful, readonly, complete or absent concurrent dispatch.
            noncontrol += 1
            continue
        if action not in {"kill", "stop", "die"} or action in controls:
            raise RecoveryRefused("recovery_web_stop_events_unproven")
        attributes = entry["Actor"].get("Attributes", {})
        if (action == "kill" and attributes.get("signal") != "15") or (
            action == "die" and attributes.get("exitCode") != "143"
        ):
            raise RecoveryRefused("recovery_web_stop_events_unproven")
        controls[action] = stamp
    if set(controls) != {"kill", "stop", "die"} or controls["kill"] > min(
        controls["stop"], controls["die"]
    ):
        raise RecoveryRefused("recovery_web_stop_events_unproven")
    return _WebStopEventEvidence(
        digest(events),
        controls["kill"],
        controls["stop"],
        controls["die"],
        outside,
        noncontrol,
    )


@dataclass(frozen=True)
class _WebStopObservation:
    state: dict[str, Any]
    sources: dict[str, str]
    raw_log: bytes
    host_pid: int
    namespace_pid: int
    start: str
    boot_id: str
    init_start: str


def _observe_web_stop(client: Any, state: dict[str, Any]) -> _WebStopObservation:
    sources = _web_stack(client, state)
    raw = client.logs(state["Id"], stdout=True, stderr=True, timestamps=True)
    namespace_pid = _web_start_pid(raw, state["State"]["StartedAt"])
    matches = [
        row
        for row in _application_processes(state["State"]["Pid"], state["Id"])
        if row[2] == namespace_pid
    ]
    if len(matches) != 1:
        raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
    pid, start, _ = matches[0]
    if (
        os.readlink(f"/proc/{pid}/exe") != "/usr/local/bin/python3.12"
        or Path(f"/proc/{pid}/cmdline").read_bytes()
        != b"/usr/local/bin/python\0/usr/local/bin/kodi-lab-web\0"
        or _cgroup(pid, state["Id"])[1] != start
    ):
        raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
    return _WebStopObservation(
        state,
        sources,
        raw,
        pid,
        namespace_pid,
        start,
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        _cgroup(state["State"]["Pid"], state["Id"])[1],
    )


def _prove_web_stop(
    client: Any,
    observed: _WebStopObservation,
    after: dict[str, Any],
    stop_started_ns: int,
    stop_completed_ns: int,
) -> WebGracefulSigtermProof:
    before, status = observed.state, after["State"]
    if (
        any(
            before[key] != after[key]
            for key in ("Id", "Image", "Config", "HostConfig", "Mounts", "RestartCount")
        )
        or after["RestartCount"] != 0
        or status["ExitCode"] != 143
        or status["Pid"] != 0
        or status["Status"] != "exited"
        or any(
            status.get(key) is not False
            for key in ("Running", "Restarting", "OOMKilled", "Dead", "Paused")
        )
        or status.get("Error") != ""
        or status["StartedAt"] != before["State"]["StartedAt"]
        or _web_stack(client, after) != observed.sources
        or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        != observed.boot_id
    ):
        raise RecoveryRefused("recovery_web_shutdown_identity_changed")
    final = client.logs(before["Id"], stdout=True, stderr=True, timestamps=True)
    finished_ns = _utc_ns(status["FinishedAt"])
    first, last = _web_shutdown_suffix(
        observed.raw_log,
        final,
        status["StartedAt"],
        observed.namespace_pid,
        stop_started_ns,
        finished_ns,
    )
    # This bounded observation may legitimately extend past the actual SDK Stop
    # return. Never substitute its cutoff/completion time for that return time.
    observation_until_ns = stop_completed_ns + 2_000_000_000
    stream = client.events(
        since=stop_started_ns // 1_000_000_000,
        until=(observation_until_ns + 999_999_999) // 1_000_000_000,
        filters={"container": before["Id"], "type": "container"},
        decode=True,
    )
    events = []
    try:
        for entry in stream:
            events.append(entry)
            if len(events) > 128:
                raise RecoveryRefused("recovery_web_stop_events_unproven")
    finally:
        stream.close()
    observation_completed_ns = time.time_ns()
    if (
        not observation_until_ns
        <= observation_completed_ns
        <= stop_completed_ns + 10_000_000_000
    ):
        raise RecoveryRefused("recovery_web_stop_event_observation_unproven")
    event_proof = _web_stop_events(
        events, before["Id"], stop_started_ns, observation_until_ns
    )
    return WebGracefulSigtermProof(
        kind="uvicorn_0530_graceful_sigterm",
        container_id=before["Id"],
        image_id=before["Image"],
        boot_id=observed.boot_id,
        source_digests=observed.sources,
        init_pid=before["State"]["Pid"],
        init_start=observed.init_start,
        server_host_pid=observed.host_pid,
        server_namespace_pid=observed.namespace_pid,
        server_start=observed.start,
        stop_timeout=-1,
        signal=15,
        raw_exit_code=143,
        stop_started_ns=stop_started_ns,
        shutdown_started_ns=first,
        shutdown_finished_ns=last,
        container_finished_ns=finished_ns,
        container_finished_at=status["FinishedAt"],
        stop_completed_ns=stop_completed_ns,
        event_proof_scope="lifecycle_controls_only",
        events_observation_until_ns=observation_until_ns,
        events_observation_completed_ns=observation_completed_ns,
        signal_event_ns=event_proof.signal_ns,
        stop_event_ns=event_proof.stop_ns,
        die_event_ns=event_proof.die_ns,
        out_of_window_event_records=event_proof.out_of_window_records,
        noncontrol_event_records=event_proof.noncontrol_records,
        before_log_digest=hashlib.sha256(observed.raw_log).hexdigest(),
        final_log_digest=hashlib.sha256(final).hexdigest(),
        engine_events_digest=event_proof.digest,
    )


def inspect_broker_log_policy(client: Any, cid: str) -> BrokerLogPolicy:
    before = client.inspect_container(cid)
    config = before["Config"]
    env = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    if (
        before["Id"] != cid
        or config.get("Entrypoint") != ["kodi-lab-broker"]
        or config.get("Cmd") not in (None, [])
        or env.get("PYTHONPATH")
        or env.get("PYTHONHOME")
    ):
        raise RecoveryRefused("recovery_broker_log_policy_unproven")
    passwd = _metadata_file(client, cid, "/etc/passwd")
    users = [
        line.split(":")
        for line in passwd.decode().splitlines()
        if line.startswith("kodi-lab:")
    ]
    groups = [
        line.split(":")
        for line in _metadata_file(client, cid, "/etc/group").decode().splitlines()
        if line.startswith("kodi-lab:")
    ]
    if len(users) != 1 or len(groups) != 1 or int(groups[0][2]) != int(users[0][3]):
        raise RecoveryRefused("recovery_broker_log_policy_unproven")
    digests = {
        name: hashlib.sha256(
            _metadata_file(
                client,
                cid,
                "/usr/local/lib/python3.12/site-packages/kodi_lab/broker/" + name,
            )
        ).hexdigest()
        for name in LEGACY_303_BROKER_SOURCES
    }
    after = client.inspect_container(cid)
    if any(
        before[key] != after[key]
        for key in ("Id", "Image", "Config", "Mounts", "RestartCount")
    ):
        raise RecoveryRefused("recovery_broker_policy_identity_changed")
    policy = BrokerLogPolicy(
        dedicated_uid=int(users[0][2]),
        dedicated_gid=int(users[0][3]),
        entrypoint=tuple(config["Entrypoint"]),
        image_id=before["Image"],
        passwd_digest=hashlib.sha256(passwd).hexdigest(),
        source_digests=digests,
    )
    policy.require_supported()
    return policy


def validate_drain_log(
    raw: bytes, policy: BrokerLogPolicy | None = None
) -> dict[str, int]:
    """Classify supported diagnostics separately; none counts as an audit reply."""
    if not isinstance(raw, bytes) or len(raw) > 4 * 1024 * 1024:
        raise RecoveryRefused("recovery_complete_broker_drain_log_unavailable")
    counts = {"audits": 0, "alembic_setup": 0, "completed_measurements": 0}
    pending_review = False
    plugins = {
        "alembic.autogenerate." + name
        for name in (
            "schemas",
            "tables",
            "types",
            "constraints",
            "defaults",
            "comments",
        )
    }
    plugins.add("alembic.ext.checkconstraint_byname")
    try:
        for original in raw.decode().splitlines():
            line = re.sub(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ", "", original, count=1)
            if line.startswith("{"):
                entry = broker_event(line)
                if pending_review:
                    if (
                        entry.get("event") != "broker request completed"
                        or entry.get("broker_operation") != "review_source_additions"
                        or entry.get("broker_result_code") != "success"
                        or entry.get("broker_protocol_version") != 1
                        or type(entry.get("broker_protocol_version")) is not int
                        or entry.get("broker_peer_uid") != 10001
                        or entry.get("broker_peer_gid") != 10001
                        or type(entry.get("broker_peer_pid")) is not int
                        or entry["broker_peer_pid"] < 0
                        or not isinstance(entry.get("broker_request_id"), str)
                        or str(UUID(entry["broker_request_id"]))
                        != entry["broker_request_id"]
                    ):
                        raise RecoveryRefused(
                            "recovery_unconfirmed_deployment_diagnostic"
                        )
                    elapsed = entry.get("broker_elapsed_seconds")
                    if (
                        not isinstance(elapsed, (int, float))
                        or isinstance(elapsed, bool)
                        or not math.isfinite(elapsed)
                        or elapsed < 0
                    ):
                        raise RecoveryRefused(
                            "recovery_unconfirmed_deployment_diagnostic"
                        )
                    pending_review = False
                    counts["read_only_review_diagnostics"] = (
                        counts.get("read_only_review_diagnostics", 0) + 1
                    )
                if entry.get("broker_result_code") == "response_transport_error":
                    if (
                        entry.get("event") == "broker request completed"
                        and all(
                            key in entry and entry[key] is None
                            for key in (
                                "broker_operation",
                                "broker_request_id",
                                "broker_protocol_version",
                            )
                        )
                        and type(entry.get("broker_peer_uid")) is int
                        and entry["broker_peer_uid"] == 0
                        and type(entry.get("broker_peer_gid")) is int
                        and entry["broker_peer_gid"] == 0
                        and type(entry.get("broker_peer_pid")) is int
                        and entry["broker_peer_pid"] > 0
                    ):
                        elapsed = entry.get("broker_elapsed_seconds")
                        if (
                            not isinstance(elapsed, (int, float))
                            or isinstance(elapsed, bool)
                            or not math.isfinite(elapsed)
                            or elapsed < 0
                        ):
                            raise RecoveryRefused(
                                "recovery_uncertain_prior_broker_dispatch"
                            )
                        if policy is None:
                            raise RecoveryRefused("recovery_log_policy_required")
                        policy.require_supported()
                        counts["predecode_denial_reply_losses"] = (
                            counts.get("predecode_denial_reply_losses", 0) + 1
                        )
                        counts["audits"] += 1
                        continue  # Raw result remains response_transport_error.
                    raise RecoveryRefused("recovery_uncertain_prior_broker_dispatch")
                if entry.get("event") != "broker request completed" or entry.get(
                    "broker_result_code"
                ) not in {
                    "success",
                    "unauthorized_peer",
                    "development_session_identity_changed",
                }:
                    raise RecoveryRefused("recovery_uncertain_prior_broker_dispatch")
                counts["audits"] += 1
                continue
            if pending_review:
                raise RecoveryRefused("recovery_unconfirmed_deployment_diagnostic")
            record = re.fullmatch(
                r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO ([a-z_.]+) (.+)", line
            )
            if record is None:
                raise RecoveryRefused("recovery_unparsed_broker_diagnostic")
            logger, message = record.groups()
            if logger == "alembic.runtime.plugins" and message in {
                "setup plugin " + plugin for plugin in plugins
            }:
                counts["alembic_setup"] += 1
                continue
            if logger != "kodi_lab.deployment.measurement" or not message.startswith(
                "deployment measurement "
            ):
                raise RecoveryRefused("recovery_unparsed_broker_diagnostic")
            measured = json.loads(message.removeprefix("deployment measurement "))
            if (
                set(measured)
                != {
                    "schema_version",
                    "operation_id",
                    "layer",
                    "completed",
                    "total_seconds",
                    "phases_seconds",
                    "counts",
                }
                or measured["schema_version"] != 1
                or type(measured["completed"]) is not bool
                or measured["layer"] not in {"copy_sync", "readback"}
                or len(measured["counts"]) > 32
                or len(measured["phases_seconds"]) > 32
            ):
                raise RecoveryRefused("recovery_unconfirmed_deployment_diagnostic")
            if measured["layer"] == "copy_sync":
                if str(UUID(measured["operation_id"])) != measured["operation_id"]:
                    raise ValueError
            elif measured["operation_id"] != "destination_verification":
                raise ValueError
            if any(
                type(value) is not int or value < 0
                for value in measured["counts"].values()
            ):
                raise ValueError
            durations = [
                measured["total_seconds"],
                *measured["phases_seconds"].values(),
            ]
            if any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in durations
            ):
                raise ValueError
            if measured["completed"] is False:
                if (
                    measured["layer"] != "copy_sync"
                    or set(measured["phases_seconds"])
                    != {
                        "stage_create",
                        "source_scan_hash",
                        "destination_before",
                        "review_confirm",
                        "stage_cleanup",
                    }
                    or set(measured["counts"])
                    != {
                        "source_entries",
                        "source_files",
                        "source_scan_bytes",
                        "destination_before_archive_bytes",
                        "review_confirm_archive_bytes",
                    }
                ):
                    raise RecoveryRefused("recovery_unconfirmed_deployment_diagnostic")
                if policy is None:
                    raise RecoveryRefused("recovery_log_policy_required")
                policy.require_supported()
                pending_review = True
                continue  # Preserve false; require exact following readonly audit.
            counts["completed_measurements"] += 1
    except (UnicodeError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise RecoveryRefused("recovery_unparsed_broker_diagnostic") from error
    if counts["audits"] == 0:
        raise RecoveryRefused("recovery_complete_broker_drain_log_unavailable")
    if pending_review:
        raise RecoveryRefused("recovery_unconfirmed_deployment_diagnostic")
    return counts


def _cgroup(pid: int, container_id: str) -> tuple[str, str]:
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    entries = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    unified = [line.removeprefix("0::") for line in entries if line.startswith("0::/")]
    if (
        len(unified) != 1
        or unified[0] == "/"
        or ".." in Path(unified[0]).parts
        or not any(
            part in {container_id, f"docker-{container_id}.scope"}
            for part in Path(unified[0]).parts
        )
    ):
        raise RecoveryRefused("recovery_exact_service_cgroup_unavailable")
    path = Path("/sys/fs/cgroup") / unified[0].lstrip("/")
    if (
        "populated 1" not in (path / "cgroup.events").read_text().splitlines()
        or str(pid) not in (path / "cgroup.procs").read_text().splitlines()
        or start != Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    ):
        raise RecoveryRefused("recovery_daemon_process_context_unproven")
    return unified[0], start


def _application_processes(init_pid: int, cid: str) -> list[tuple[int, str, int]]:
    pending = [init_pid]
    seen: set[int] = set()
    result = []
    while pending:
        pid = pending.pop()
        if pid in seen or len(seen) >= 128:
            raise RecoveryRefused("recovery_service_process_tree_unproven")
        seen.add(pid)
        _, start = _cgroup(pid, cid)
        status = Path(f"/proc/{pid}/status").read_text().splitlines()
        namespace_pid = next(
            (line.split()[-1] for line in status if line.startswith("NSpid:")), None
        )
        if namespace_pid is None or not namespace_pid.isdecimal():
            raise RecoveryRefused("recovery_application_owner_unproven")
        result.append((pid, start, int(namespace_pid)))
        pending.extend(
            int(value)
            for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        )
    return result


def _application_owner(init_pid: int, cid: str, owner_json: str) -> dict[str, Any]:
    owner = json.loads(owner_json)
    _, init_start = _cgroup(init_pid, cid)
    if (
        owner["init_start"] != init_start
        or not cid.startswith(owner["hostname"])
        or owner["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ):
        raise RecoveryRefused("recovery_application_init_mismatch")
    matches = [
        pid
        for pid, start, namespace_pid in _application_processes(init_pid, cid)
        if start == owner["start"] and namespace_pid == owner["pid"]
    ]
    if len(matches) != 1:
        raise RecoveryRefused("recovery_application_owner_unproven")
    return {
        "application_start": owner["start"],
        "application_namespace_pid": owner["pid"],
        "application_owner_digest": digest(owner),
    }


def _empty_cgroup(cgroup: str) -> bool:
    path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
    try:
        return "populated 0" in (path / "cgroup.events").read_text().splitlines()
    except FileNotFoundError:
        # Kernel removal of the exact previously observed service cgroup proves
        # no remaining process there. It is not a newly asserted generic path.
        return not path.exists()


def _retained_consumers(
    plan: InstallationPlan,
    client: Any,
    initial: dict[str, tuple[dict[str, Any], str, str]],
) -> tuple[tuple[InertStateConsumer, ...], dict[str, str | None], str, str, str]:
    pair = {plan.web_container_id, plan.broker_container_id}
    reviewed = {
        row.container_id: row.metadata_digest for row in plan.ancillary_consumers
    }
    if len(reviewed) != len(plan.ancillary_consumers) or pair & set(reviewed):
        raise RecoveryRefused("recovery_consumer_review_invalid")
    patterns = {
        path.replace(state["Id"], "{cid}") for state, path, _ in initial.values()
    }
    parents = {
        state["HostConfig"].get("CgroupParent") for state, _, _ in initial.values()
    }
    if (
        len(patterns) != 1
        or len(parents) != 1
        or not all(isinstance(parent, str) for parent in parents)
    ):
        raise RecoveryRefused("recovery_consumer_cgroup_context_unproven")
    pattern, parent = next(iter(patterns)), next(iter(parents))
    consumers, aliases, backing = discover_state_consumers(client, plan.state_volume)
    if set(consumers) != pair | set(reviewed):
        raise RecoveryRefused("recovery_unapproved_state_consumer")
    incident = plan.review.normalized()["receipt"]["requested_at"]
    result = []
    for cid, expected in sorted(reviewed.items()):
        inspected = consumers[cid]
        if consumer_metadata_digest(inspected) != expected:
            raise RecoveryRefused("recovery_retained_consumer_changed")
        require_inert_consumer(inspected, incident, parent)
        cgroup = pattern.format(cid=cid)
        if not _empty_cgroup(cgroup):
            raise RecoveryRefused("recovery_retained_consumer_kernel_live")
        after = client.inspect_container(cid)
        if consumer_metadata_digest(after) != expected:
            raise RecoveryRefused("recovery_retained_consumer_changed")
        require_inert_consumer(after, incident, parent)
        result.append(
            InertStateConsumer(
                container_id=cid,
                metadata_digest=expected,
                cgroup=cgroup,
                finished_at=after["State"]["FinishedAt"],
                exit_code=after["State"]["ExitCode"],
            )
        )
    return tuple(result), aliases, backing, parent, pattern


def drain(plan: InstallationPlan, client: Any) -> RecoveryDrainSeal:
    root = Path(plan.bundle_root)
    compose = Path(plan.compose_file)
    if (
        not root.is_absolute()
        or root.resolve() != root
        or not compose.is_absolute()
        or compose.resolve() != compose
        or not compose.is_relative_to(root)
        or client.info()["ID"] != plan.engine_id
    ):
        raise RecoveryRefused("recovery_installation_identity_mismatch")
    reviewed = plan.review.normalized()
    if set(plan.owners) - {"web", "broker"}:
        raise RecoveryRefused("recovery_owner_role_invalid")
    state_volume_digest = volume_identity(client, plan.state_volume)
    initial: dict[str, tuple[dict[str, Any], str, str]] = {}
    applications: dict[str, dict[str, Any]] = {}
    for role, cid in (
        ("web", plan.web_container_id),
        ("broker", plan.broker_container_id),
    ):
        state = client.inspect_container(cid)
        service_binding(client, cid, plan.installation, plan.state_volume, role)
        labels = state["Config"]["Labels"]
        if (
            state["Id"] != cid
            or not state["State"]["Running"]
            or state["State"].get("Restarting")
            or state["State"].get("Paused")
            or labels.get("com.docker.compose.project") != plan.installation
            or labels.get("com.docker.compose.service") != role
            or labels.get("com.docker.compose.project.working_dir") != str(root)
        ):
            raise RecoveryRefused("recovery_application_service_mismatch")
        cgroup, start = _cgroup(state["State"]["Pid"], cid)
        initial[role] = state, cgroup, start
        applications[role] = (
            _application_owner(state["State"]["Pid"], cid, plan.owners[role])
            if role in plan.owners
            else {}
        )
    # Reject unknown/error records before stopping either application service.
    # Recheck the full final capture after the sequential graceful drain too.
    log_policy: BrokerLogPolicy | None = None

    def verify_log(raw: bytes) -> None:
        nonlocal log_policy
        try:
            validate_drain_log(raw, log_policy)
        except RecoveryRefused as error:
            if str(error) != "recovery_log_policy_required":
                raise
            log_policy = inspect_broker_log_policy(client, plan.broker_container_id)
            validate_drain_log(raw, log_policy)

    verify_log(client.logs(plan.broker_container_id, stdout=True, stderr=True))
    _retained_consumers(plan, client, initial)
    web_observation = None
    # Optional only for the unchanged zero-exit contract. Missing proof can
    # NEVER authorize143 after Stop or be supplied by an installation plan.
    with suppress(
        RecoveryRefused,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        docker.errors.DockerException,
    ):
        web_observation = _observe_web_stop(client, initial["web"][0])
    retirements: dict[str, ServiceRetirement] = {}
    for role, cid in (
        ("web", plan.web_container_id),
        ("broker", plan.broker_container_id),
    ):
        before, cgroup, start = initial[role]
        _retained_consumers(plan, client, initial)
        current = client.inspect_container(cid)
        if (
            client.info()["ID"] != plan.engine_id
            or current["Id"] != cid
            or current["State"]["Pid"] != before["State"]["Pid"]
            or _cgroup(current["State"]["Pid"], cid) != (cgroup, start)
        ):
            raise RecoveryRefused("recovery_service_changed_before_stop")
        # Same SDK connection/Engine, full approved CID, no ambient CLI/context.
        # Docker stop is manual stop; -1 permits no timeout-to-SIGKILL fallback.
        if (
            role == "web"
            and web_observation is not None
            and _cgroup(web_observation.host_pid, cid)[1] != web_observation.start
        ):
            raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
        stop_started_ns = time.time_ns()
        client.stop(cid, timeout=-1)
        stop_completed_ns = time.time_ns()
        after = client.inspect_container(cid)
        status = after["State"]
        if (
            after["Id"] != cid
            or status["Running"]
            or status.get("Restarting")
            or status.get("OOMKilled")
            or status.get("Error")
            or not _empty_cgroup(cgroup)
        ):
            raise RecoveryRefused("recovery_complete_service_exit_unproven")
        web_proof = None
        if status["ExitCode"] != 0:
            if role != "web" or status["ExitCode"] != 143 or web_observation is None:
                raise RecoveryRefused("recovery_nonzero_service_exit_unproven")
            web_proof = _prove_web_stop(
                client, web_observation, after, stop_started_ns, stop_completed_ns
            )
            if applications[role] and (
                applications[role]["application_start"] != web_proof.server_start
                or applications[role]["application_namespace_pid"]
                != web_proof.server_namespace_pid
            ):
                raise RecoveryRefused("recovery_web_shutdown_pid_unproven")
            applications[role].update(
                application_start=web_proof.server_start,
                application_namespace_pid=web_proof.server_namespace_pid,
            )
        retirements[role] = ServiceRetirement(
            container_id=cid,
            initial_pid=before["State"]["Pid"],
            initial_start=start,
            cgroup=cgroup,
            final_pid=status["Pid"],
            final_status=status["Status"],
            exit_code=status["ExitCode"],
            restart_count_before=before["RestartCount"],
            restart_count_after=after["RestartCount"],
            cgroup_populated=0,
            finished_at=status["FinishedAt"],
            web_graceful_sigterm=web_proof,
            **applications[role],
        )
    # After complete broker process exit a successful synchronous request cannot
    # remain in flight. Unknown failure/transport outcomes still refuse sealing.
    raw = client.logs(plan.broker_container_id, stdout=True, stderr=True)
    verify_log(raw)
    if (
        client.info()["ID"] != plan.engine_id
        or volume_identity(client, plan.state_volume) != state_volume_digest
    ):
        raise RecoveryRefused("recovery_installation_identity_mismatch")
    for retired in retirements.values():
        final = client.inspect_container(retired.container_id)
        if (
            final["State"]["Pid"] != 0
            or final["State"]["Running"]
            or final["State"]["FinishedAt"] != retired.finished_at
            or final["RestartCount"] != retired.restart_count_after
        ):
            raise RecoveryRefused("recovery_service_restarted_during_drain")
    ancillary, aliases, backing, parent, pattern = _retained_consumers(
        plan, client, initial
    )
    consumer_proof = StateConsumerProof(
        incident_requested_at=reviewed["receipt"]["requested_at"],
        backing_path=backing,
        bind_aliases=aliases,
        cgroup_parent=parent,
        cgroup_pattern=pattern,
        retired_services={
            cid: consumer_metadata_digest(client.inspect_container(cid))
            for cid in (plan.web_container_id, plan.broker_container_id)
        },
        ancillary=ancillary,
    )
    seal = RecoveryDrainSeal(
        version=1,
        installation=plan.installation,
        engine_id=plan.engine_id,
        state_volume=plan.state_volume,
        state_volume_digest=state_volume_digest,
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        candidate_package_digest=plan.candidate_package_digest,
        incident_id=reviewed["receipt"]["id"],
        web=retirements["web"],
        broker=retirements["broker"],
        state_consumers=consumer_proof,
        broker_capture_digest=reviewed["primary_digests"]["broker"],
        sequential_handler_completion="clean_service_exit_after_web_retirement",
        drain_audit_digest=hashlib.sha256(raw).hexdigest(),
        broker_log_policy=None if log_policy is None else log_policy.model_dump(),
    )
    seal.validate_chain()
    return seal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("approved_plan", type=Path)
    parser.add_argument(
        "--inspect-consumers",
        action="store_true",
        help="read-only consumer IDs/digests for independent review; adopts nothing",
    )
    arguments = parser.parse_args()
    plan = InstallationPlan.model_validate_json(arguments.approved_plan.read_bytes())
    client = docker.from_env()
    try:
        context = _daemon_filesystem_context(client.api, plan.engine_id)
        if arguments.inspect_consumers:
            if client.api.info()["ID"] != plan.engine_id:
                raise RecoveryRefused("recovery_installation_identity_mismatch")
            volume_identity(client.api, plan.state_volume)
            consumers, _, _ = discover_state_consumers(client.api, plan.state_volume)
            if _daemon_filesystem_context(client.api, plan.engine_id) != context:
                raise RecoveryRefused("recovery_daemon_context_changed")
            print(
                json.dumps(
                    {
                        "consumers": [
                            {
                                "container_id": cid,
                                "metadata_digest": consumer_metadata_digest(row),
                                "review_required": True,
                            }
                            for cid, row in sorted(consumers.items())
                        ]
                    },
                    sort_keys=True,
                )
            )
            return
        seal = drain(plan, client.api)
        if _daemon_filesystem_context(client.api, plan.engine_id) != context:
            raise RecoveryRefused("recovery_daemon_context_changed")
        seal.daemon_filesystem_context = context
        print(seal.model_dump_json())
    finally:
        client.close()

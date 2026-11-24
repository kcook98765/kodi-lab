"""Offline host observation of two exact application services; never managed Kodi.

The approved local plan is infrastructure authority, NOT a Web API. Service
control uses the same verified SDK Engine and exact CIDs. No force/kill timeout,
container creation, native profile access, archive extraction or cleanup exists.
"""

from __future__ import annotations

import argparse
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
    InertStateConsumer,
    RecoveryDrainSeal,
    ReviewedStateConsumer,
    ServiceRetirement,
    StateConsumerProof,
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


def _application_owner(init_pid: int, cid: str, owner_json: str) -> dict[str, Any]:
    owner = json.loads(owner_json)
    _, init_start = _cgroup(init_pid, cid)
    if (
        owner["init_start"] != init_start
        or not cid.startswith(owner["hostname"])
        or owner["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    ):
        raise RecoveryRefused("recovery_application_init_mismatch")
    pending = [init_pid]
    seen: set[int] = set()
    matches: list[int] = []
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
        if start == owner["start"] and namespace_pid == str(owner["pid"]):
            matches.append(pid)
        pending.extend(
            int(value)
            for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        )
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
        client.stop(cid, timeout=-1)
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

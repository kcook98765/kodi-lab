"""Exact offline service-retirement seal and conservative process-owner binding.

All durable normalized proof is SQLite data covered by existing application
backup. Raw captures remain independently retained; no new data-root layout.
"""

from __future__ import annotations

import json
import os
import posixpath
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_recovery_evidence import (
    LegacyCaptureReview,
    RecoveryRefused,
    canonical,
    digest,
)
from kodi_lab.build_identity import package_sha256
from kodi_lab.storage.models import (
    SnapshotRecoveryEpochRow,
    SnapshotRecoveryRow,
    SnapshotRestoreOperationRow,
)
from kodi_lab.storage.snapshot_recovery import (
    active_epoch,
    require_idle,
    require_retained_attribution,
    stamp,
    values,
)

UVICORN_0530_SOURCES = {
    "uvicorn/__init__.py": (
        "6330ecb843b39a91aaedb219f3d2ad68a7fe623ce49e8be6cda2257a4f5f903b"
    ),
    "uvicorn/server.py": (
        "c1be9d133467a42ab88d7168e408ef5a90257661a901899426214a3d1fc2d026"
    ),
}


class WebGracefulSigtermProof(BaseModel):
    """Derived fresh helper observations; raw143 is never rewritten to zero."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["uvicorn_0530_graceful_sigterm"]
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    boot_id: str = Field(min_length=36, max_length=36)
    source_digests: dict[str, str] = Field(min_length=7, max_length=7)
    init_pid: int = Field(gt=0)
    init_start: str = Field(min_length=1, max_length=32)
    server_host_pid: int = Field(gt=0)
    server_namespace_pid: int = Field(gt=1)
    server_start: str = Field(min_length=1, max_length=32)
    stop_timeout: Literal[-1]
    signal: Literal[15]
    raw_exit_code: Literal[143]
    stop_started_ns: int = Field(gt=0)
    shutdown_started_ns: int = Field(gt=0)
    shutdown_finished_ns: int = Field(gt=0)
    container_finished_ns: int = Field(gt=0)
    container_finished_at: str = Field(min_length=20, max_length=40)
    stop_completed_ns: int = Field(gt=0)
    event_proof_scope: Literal["lifecycle_controls_only"]
    events_observation_until_ns: int = Field(gt=0)
    events_observation_completed_ns: int = Field(gt=0)
    signal_event_ns: int = Field(gt=0)
    stop_event_ns: int = Field(gt=0)
    die_event_ns: int = Field(gt=0)
    out_of_window_event_records: int = Field(ge=0, le=128)
    noncontrol_event_records: int = Field(ge=0, le=128)
    before_log_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_log_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_events_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            not self.stop_started_ns
            <= self.shutdown_started_ns
            <= self.shutdown_finished_ns
            <= self.container_finished_ns
            <= self.stop_completed_ns
            or not self.stop_completed_ns
            <= self.events_observation_until_ns
            <= self.events_observation_completed_ns
            <= self.stop_completed_ns + 10_000_000_000
            or self.events_observation_until_ns
            != self.stop_completed_ns + 2_000_000_000
            or not self.stop_started_ns
            <= self.signal_event_ns
            <= min(self.stop_event_ns, self.die_event_ns)
            <= max(self.stop_event_ns, self.die_event_ns)
            <= self.events_observation_until_ns
            or any(
                self.source_digests.get(key) != value
                for key, value in UVICORN_0530_SOURCES.items()
            )
            or set(self.source_digests)
            != {
                *UVICORN_0530_SOURCES,
                "launcher",
                "admin_cli.py",
                "web/run.py",
                "admin_console",
                "web_console",
            }
            or any(len(value) != 64 for value in self.source_digests.values())
        ):
            raise ValueError("Web graceful SIGTERM evidence is inconsistent")
        return self


class ServiceRetirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_pid: int = Field(gt=0)
    initial_start: str = Field(min_length=1, max_length=32)
    cgroup: str = Field(min_length=1, max_length=1024)
    final_pid: int = Field(ge=0, le=0)
    final_status: str = Field(pattern="^exited$")
    exit_code: int = Field(ge=0, le=143)
    restart_count_before: int = Field(ge=0)
    restart_count_after: int = Field(ge=0)
    cgroup_populated: int = Field(ge=0, le=0)
    finished_at: str = Field(min_length=20, max_length=40)
    application_start: str | None = None
    application_namespace_pid: int | None = None
    application_owner_digest: str | None = None
    web_graceful_sigterm: WebGracefulSigtermProof | None = None

    @model_validator(mode="after")
    def supported_exit(self) -> Self:
        proof = self.web_graceful_sigterm
        if self.exit_code == 0 and proof is None:
            return self
        if (
            self.exit_code != 143
            or proof is None
            or proof.container_id != self.container_id
            or proof.init_pid != self.initial_pid
            or proof.init_start != self.initial_start
            or proof.server_start != self.application_start
            or proof.server_namespace_pid != self.application_namespace_pid
            or proof.container_finished_at != self.finished_at
            or self.restart_count_before != 0
            or self.restart_count_after != 0
        ):
            raise ValueError("Nonzero service exit lacks exact Web SIGTERM evidence")
        return self


class ReviewedStateConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class InertStateConsumer(ReviewedStateConsumer):
    cgroup: str
    finished_at: str
    exit_code: int


class StateConsumerProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    incident_requested_at: str
    backing_path: str
    bind_aliases: dict[str, str | None] = Field(max_length=256)
    cgroup_parent: str
    cgroup_pattern: str
    retired_services: dict[str, str] = Field(min_length=2, max_length=2)
    ancillary: tuple[InertStateConsumer, ...] = Field(max_length=32)


class RecoveryDrainSeal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=1)
    installation: str = Field(min_length=1, max_length=256)
    engine_id: str = Field(min_length=1, max_length=128)
    state_volume: str = Field(min_length=1, max_length=256)
    state_volume_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    boot_id: str = Field(min_length=36, max_length=36)
    candidate_package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    incident_id: str = Field(min_length=36, max_length=36)
    web: ServiceRetirement
    broker: ServiceRetirement
    state_consumers: StateConsumerProof
    broker_capture_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    drain_audit_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    broker_log_policy: dict[str, Any] | None = None
    daemon_filesystem_context: dict[str, Any] | None = None
    sequential_handler_completion: str = Field(
        pattern="^clean_service_exit_after_web_retirement$"
    )

    def validate_chain(self) -> None:
        if (
            self.web.container_id == self.broker.container_id
            or self.broker.exit_code != 0
            or self.broker.web_graceful_sigterm is not None
            or (
                self.web.web_graceful_sigterm is not None
                and self.web.web_graceful_sigterm.boot_id != self.boot_id
            )
            or set(self.state_consumers.retired_services)
            != {self.web.container_id, self.broker.container_id}
            or len({c.container_id for c in self.state_consumers.ancillary})
            != len(self.state_consumers.ancillary)
            or any(
                c.container_id in self.state_consumers.retired_services
                for c in self.state_consumers.ancillary
            )
            or self.web.finished_at > self.broker.finished_at
            or any(
                s.restart_count_before != s.restart_count_after
                for s in (self.web, self.broker)
            )
        ):
            raise RecoveryRefused("recovery_service_retirement_unproven")


def process_owner() -> str:
    # A PID alone can be reused. Namespace boot + start identity + nonce cannot
    # authorize a prior process's dispatch after restart.
    stat_fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return canonical(
        {
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid": os.getpid(),
            "start": stat_fields[19],
            "init_start": Path("/proc/1/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19],
            "hostname": socket.gethostname(),
            "pid_namespace": os.readlink("/proc/self/ns/pid"),
            "nonce": str(uuid4()),
        }
    )


def volume_identity(client: Any, volume_name: str) -> str:
    volume = client.inspect_volume(volume_name)
    if (
        volume.get("Name") != volume_name
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or not volume.get("CreatedAt")
    ):
        raise RecoveryRefused("recovery_state_volume_unproven")
    return digest(
        {key: volume[key] for key in ("Name", "Driver", "Scope", "CreatedAt")}
    )


def service_binding(
    client: Any, container: str, installation: str, volume: str, role: str
) -> dict[str, Any]:
    """Read only service metadata; never return environment/credential/mount paths."""
    inspected = client.inspect_container(container)
    cid = inspected["Id"]
    labels = inspected["Config"]["Labels"]
    mounts = [
        m for m in inspected["Mounts"] if m.get("Destination") == "/var/lib/kodi-lab"
    ]
    if (
        not isinstance(cid, str)
        or len(cid) != 64
        or not cid.startswith(container)
        or labels.get("com.docker.compose.project") != installation
        or labels.get("com.docker.compose.service") != role
        or inspected.get("HostConfig", {}).get("PidMode") not in {None, "", "private"}
        or len(mounts) != 1
        or mounts[0].get("Type") != "volume"
        or mounts[0].get("Name") != volume
        or mounts[0].get("RW") is not True
    ):
        raise RecoveryRefused("recovery_application_service_mismatch")
    return {
        "container_id": cid,
        "hostname": inspected["Config"]["Hostname"],
        "running": inspected["State"]["Running"],
        "status": inspected["State"]["Status"],
    }


def consumer_metadata_digest(inspected: dict[str, Any]) -> str:
    """Freeze exact configuration/mount/state metadata; emit only the digest."""
    return digest(
        {
            key: inspected[key]
            for key in (
                "Id",
                "Name",
                "Created",
                "Image",
                "Config",
                "HostConfig",
                "Mounts",
                "State",
                "RestartCount",
            )
        }
    )


def _absolute_mount_path(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise RecoveryRefused("recovery_mount_identity_unclassified")
    return posixpath.normpath(value)


def _overlapping_paths(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(right.rstrip("/") + "/")
        or right.startswith(left.rstrip("/") + "/")
    )


def discover_state_consumers(
    client: Any, volume: str, bind_aliases: dict[str, str | None] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, str | None], str]:
    """Exhaustive RO/RW scan, bind ancestors and local bind-volume aliases.

    Host acquisition resolves bind paths in the daemon-host filesystem metadata
    context. Later isolated broker checks use only that sealed mapping, refusing
    a new/unclassified bind source rather than adopting a baseline.
    """
    target = client.inspect_volume(volume)
    if target.get("Options") not in (None, {}):
        raise RecoveryRefused("recovery_state_volume_backing_unsupported")
    backing = _absolute_mount_path(target["Mountpoint"])
    if backing == "/":
        raise RecoveryRefused("recovery_state_volume_unproven")
    aliases = {} if bind_aliases is None else dict(bind_aliases)

    def bind_consumes(value: object) -> bool:
        source = _absolute_mount_path(value)
        if _overlapping_paths(source, backing):
            return True
        if bind_aliases is None:
            try:
                aliases[source] = str(Path(source).resolve(strict=True))
            except OSError:
                # An unresolved source is a POTENTIAL consumer, not a fabricated
                # resolved path or harmless nonconsumer. The full inert proof
                # and exact administrator-reviewed CID/digest are still required.
                aliases[source] = None
        if source not in aliases:
            raise RecoveryRefused("recovery_new_bind_alias_unclassified")
        resolved = aliases[source]
        return resolved is None or _overlapping_paths(
            _absolute_mount_path(resolved), backing
        )

    def consumes(mounts: Any) -> bool:
        if not isinstance(mounts, list):
            raise RecoveryRefused("recovery_mount_inventory_unavailable")
        found = False
        for mount in mounts:
            kind = mount.get("Type")
            if kind == "volume":
                name = mount.get("Name")
                if name == volume:
                    found = True
                    continue
                if not isinstance(name, str) or not name:
                    raise RecoveryRefused("recovery_mount_identity_unclassified")
                other = client.inspect_volume(name)
                source = _absolute_mount_path(other["Mountpoint"])
                found |= _overlapping_paths(source, backing)
                options = other.get("Options") or {}
                if "bind" in str(options.get("o", "")) or options.get("type") == "none":
                    found |= bind_consumes(options.get("device"))
            elif kind == "bind":
                found |= bind_consumes(mount.get("Source"))
            elif kind != "tmpfs":
                raise RecoveryRefused("recovery_mount_identity_unclassified")
        return found

    summaries = client.containers(all=True)
    if not isinstance(summaries, list) or len(summaries) > 4096:
        raise RecoveryRefused("recovery_consumer_inventory_unavailable")
    ids = [row["Id"] for row in summaries]
    if len(set(ids)) != len(ids):
        raise RecoveryRefused("recovery_consumer_inventory_unavailable")
    consumers = {}
    for summary in summaries:
        inspected = None
        mounts = summary.get("Mounts")
        if mounts is None:
            inspected = client.inspect_container(summary["Id"])
            mounts = inspected["Mounts"]
        if consumes(mounts):
            inspected = inspected or client.inspect_container(summary["Id"])
            if inspected["Id"] != summary["Id"] or not consumes(inspected["Mounts"]):
                raise RecoveryRefused("recovery_consumer_inventory_changed")
            consumers[inspected["Id"]] = inspected
    final = client.containers(all=True)
    if sorted(row["Id"] for row in final) != sorted(ids):
        raise RecoveryRefused("recovery_consumer_inventory_changed")
    if len(aliases) > 256:
        raise RecoveryRefused("recovery_bind_alias_inventory_too_large")
    return consumers, aliases, backing


def require_inert_consumer(
    inspected: dict[str, Any], incident: str, cgroup_parent: str
) -> None:
    state, host = inspected["State"], inspected["HostConfig"]
    try:
        finished = datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
        original = datetime.fromisoformat(incident.replace("Z", "+00:00"))
        if (
            state.get("Running") is not False
            or state.get("Restarting") is not False
            or state.get("Paused") is not False
            or state.get("Pid") != 0
            or state.get("Status") != "exited"
            or finished.year < 1970
            or not finished < original
            or host.get("RestartPolicy", {}).get("Name") != "no"
            or host.get("RestartPolicy", {}).get("MaximumRetryCount", 0) != 0
            or host.get("CgroupParent") != cgroup_parent
        ):
            raise RecoveryRefused("recovery_retained_consumer_not_inert")
    except (ValueError, KeyError, TypeError) as error:
        raise RecoveryRefused("recovery_retained_consumer_not_inert") from error


def check_state_consumers(
    client: Any, volume: str, proof: StateConsumerProof, current_pair: set[str]
) -> str:
    consumers, _, backing = discover_state_consumers(client, volume, proof.bind_aliases)
    ancillary = {row.container_id: row for row in proof.ancillary}
    allowed = current_pair | set(proof.retired_services) | set(ancillary)
    if (
        backing != proof.backing_path
        or set(consumers) - allowed
        or not current_pair <= set(consumers)
    ):
        raise RecoveryRefused("recovery_unapproved_state_consumer")
    for cid, row in ancillary.items():
        if (
            cid not in consumers
            or consumer_metadata_digest(consumers[cid]) != row.metadata_digest
        ):
            raise RecoveryRefused("recovery_retained_consumer_changed")
        require_inert_consumer(
            consumers[cid], proof.incident_requested_at, proof.cgroup_parent
        )
        if (
            row.cgroup != proof.cgroup_pattern.format(cid=cid)
            or row.finished_at != consumers[cid]["State"]["FinishedAt"]
            or row.exit_code != consumers[cid]["State"]["ExitCode"]
        ):
            raise RecoveryRefused("recovery_retained_consumer_proof_changed")
    for cid, expected in proof.retired_services.items():
        if cid in current_pair or cid not in consumers:
            continue  # Exact main services may be recreated after this drain.
        state = consumers[cid]["State"]
        if (
            consumer_metadata_digest(consumers[cid]) != expected
            or state.get("Running") is not False
            or state.get("Pid") != 0
            or state.get("Status") != "exited"
        ):
            raise RecoveryRefused("recovery_retired_service_changed")
    return digest(proof.model_dump())


def seal_epoch(
    sessions: sessionmaker[Session],
    review: LegacyCaptureReview,
    seal: RecoveryDrainSeal,
) -> str:
    """Called ONLY by offline installed admin under the lifetime lifecycle lock."""
    evidence = review.normalized()
    seal.validate_chain()
    for retirement in (seal.broker,):
        age = datetime.now(UTC) - datetime.fromisoformat(
            retirement.finished_at.replace("Z", "+00:00")
        )
        if age < timedelta(seconds=-1) or age > timedelta(minutes=5):
            raise RecoveryRefused("recovery_drain_observation_stale")
    if (
        seal.incident_id != evidence["receipt"]["id"]
        or seal.broker_capture_digest != evidence["primary_digests"]["broker"]
        or seal.candidate_package_digest != package_sha256()
        or seal.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        or seal.state_consumers.incident_requested_at
        != evidence["receipt"]["requested_at"]
    ):
        raise RecoveryRefused("recovery_seal_identity_mismatch")
    with sessions.begin() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        if active_epoch(session) is not None:
            raise RecoveryRefused("recovery_epoch_already_active")
        original = values(session.get(SnapshotRestoreOperationRow, seal.incident_id))
        if original != evidence["receipt"]:
            raise RecoveryRefused("recovery_original_receipt_changed")
        require_retained_attribution(session, evidence)
        require_idle(session, original["managed_instance_id"], seal.incident_id)
        epoch_id = str(uuid4())
        session.add(
            SnapshotRecoveryEpochRow(
                id=epoch_id,
                incident_id=seal.incident_id,
                receipt_digest=digest(original),
                evidence_json=canonical(evidence),
                seal_json=canonical(seal.model_dump()),
                state="sealed",
                active=1,
                created_at=stamp(datetime.now(UTC)),
            )
        )
        return epoch_id


def bind_owner(
    sessions: sessionmaker[Session], epoch_id: str, role: str, owner: str
) -> None:
    if role not in {"web", "broker"}:
        raise RecoveryRefused("recovery_owner_role_invalid")
    with sessions.begin() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        boundary = active_epoch(session)
        if boundary is None or boundary.id != epoch_id or boundary.state == "closed":
            raise RecoveryRefused("recovery_epoch_unavailable")
        seal = RecoveryDrainSeal.model_validate_json(boundary.seal_json)
        if seal.candidate_package_digest != package_sha256():
            raise RecoveryRefused("recovery_package_mismatch")
        if seal.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            raise RecoveryRefused("recovery_host_boot_changed")
        prior = getattr(boundary, role + "_owner")
        if (
            prior is not None
            and prior != owner
            and (
                session.scalar(
                    select(SnapshotRecoveryRow)
                    .where(
                        SnapshotRecoveryRow.epoch_id == epoch_id,
                        SnapshotRecoveryRow.consumed_at.is_not(None),
                        SnapshotRecoveryRow.state != "succeeded",
                    )
                    .limit(1)
                )
                is not None
            )
        ):
            boundary.state = "blocked"
        setattr(boundary, role + "_owner", owner)
        if boundary.state != "blocked" and boundary.web_owner and boundary.broker_owner:
            boundary.state = "serving"


def leave_epoch(
    sessions: sessionmaker[Session], epoch_id: str, drain: RecoveryDrainSeal
) -> None:
    """Offline only, after both restricted services have themselves fully exited."""
    with sessions.begin() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        boundary = active_epoch(session)
        if boundary is None or boundary.id != epoch_id:
            raise RecoveryRefused("recovery_epoch_unavailable")
        drain.validate_chain()
        original = RecoveryDrainSeal.model_validate_json(boundary.seal_json)
        if (
            drain.installation != original.installation
            or drain.engine_id != original.engine_id
            or drain.incident_id != boundary.incident_id
            or drain.boot_id != original.boot_id
            or drain.state_volume != original.state_volume
            or drain.state_volume_digest != original.state_volume_digest
            or drain.candidate_package_digest != original.candidate_package_digest
            or drain.state_consumers.ancillary != original.state_consumers.ancillary
            or drain.state_consumers.backing_path
            != original.state_consumers.backing_path
            or drain.state_consumers.bind_aliases
            != original.state_consumers.bind_aliases
            or drain.state_consumers.cgroup_parent
            != original.state_consumers.cgroup_parent
            or drain.state_consumers.cgroup_pattern
            != original.state_consumers.cgroup_pattern
        ):
            raise RecoveryRefused("recovery_leave_identity_changed")
        for role in ("web", "broker"):
            owner_json = getattr(boundary, role + "_owner")
            retirement = getattr(drain, role)
            if owner_json is not None:
                try:
                    owner = json.loads(owner_json)
                except (ValueError, TypeError) as error:
                    raise RecoveryRefused(
                        "recovery_leave_process_exit_unproven"
                    ) from error
                if (
                    owner["boot"] != drain.boot_id
                    or owner["init_start"] != retirement.initial_start
                    or owner["start"] != retirement.application_start
                    or owner["pid"] != retirement.application_namespace_pid
                    or not retirement.container_id.startswith(owner["hostname"])
                    or digest(owner) != retirement.application_owner_digest
                ):
                    raise RecoveryRefused("recovery_leave_process_exit_unproven")
        rows = list(
            session.scalars(
                select(SnapshotRecoveryRow).where(
                    SnapshotRecoveryRow.epoch_id == epoch_id,
                    SnapshotRecoveryRow.consumed_at.is_not(None),
                )
            )
        )
        if any(r.state != "succeeded" for r in rows):
            raise RecoveryRefused("recovery_epoch_has_uncertain_claim")
        boundary.state, boundary.active = "closed", None


def startup_epoch(
    sessions: sessionmaker[Session], *, require_schema: bool = False
) -> str | None:
    with sessions() as session:
        if require_schema and session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all() != ["0019_snapshot_recovery"]:
            raise RecoveryRefused("recovery_matched_schema_required")
        boundary = active_epoch(session)
        configured = os.environ.get("KODI_LAB_SNAPSHOT_RECOVERY_EPOCH")
        if (boundary is None) != (configured is None) or (
            boundary is not None and boundary.id != configured
        ):
            raise RecoveryRefused("recovery_restricted_startup_required")
        return configured

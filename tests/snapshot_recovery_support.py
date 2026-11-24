"""Synthetic legacy incident with real ordinary session/capacity refusal."""

from __future__ import annotations

import copy
import io
import os
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_recovery import SnapshotRecoveryService
from kodi_lab.application.snapshot_recovery_evidence import (
    LegacyCaptureReview,
    RecoveryRefused,
    ReviewedSnapshotIdentity,
    canonical,
    digest,
)
from kodi_lab.application.snapshot_recovery_maintenance import (
    RecoveryDrainSeal,
    ReviewedStateConsumer,
    StateConsumerProof,
    consumer_metadata_digest,
    seal_epoch,
)
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import (
    AppliedManagedInstanceSnapshotRecord,
    ContainerHealthState,
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.managed_capacity import (
    ManagedCapacityAdmission,
    ManagedCapacityError,
)
from kodi_lab.broker.provisioning import ManagedContainerProvisioningSpec
from kodi_lab.broker.snapshot_recovery import (
    RecoveryAttestation,
    RecoveryAttestationParams,
    RecoveryStepExecutor,
    RecoveryStepParams,
)
from kodi_lab.broker.snapshots import SnapshotRestoreSpec
from kodi_lab.build_identity import package_sha256
from kodi_lab.snapshot.storage import (
    SnapshotCaptureContext,
    enumerate_snapshot_source,
    publish_snapshot_from_source,
)
from kodi_lab.snapshot.validation import (
    validate_published_snapshot,
    validate_restored_snapshot_source,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedResourcePolicyRow,
    SnapshotRecoveryEpochRow,
    SnapshotRestoreOperationRow,
    SnapshotRow,
)
from kodi_lab.storage.snapshot_recovery import stamp, values
from tests.application.test_snapshot_restores import (
    IMAGE_ID,
    INSTANCE_ID,
    OLD_ID,
    REVISION_ID,
    SNAPSHOT_ID,
    select_session,
)
from tests.application.test_snapshot_restores import factory as factory

SYNTHETIC_VOLUME = {
    "Name": "qa-state",
    "Driver": "local",
    "Scope": "local",
    "CreatedAt": "2026-01-01T00:00:00Z",
    "Mountpoint": "/synthetic-state",
}
SYNTHETIC_VOLUME_DIGEST = digest(
    {k: SYNTHETIC_VOLUME[k] for k in ("Name", "Driver", "Scope", "CreatedAt")}
)


class ConsumerEngine:
    """Owned synthetic SDK metadata; never connects to a real Engine."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.volume = {**SYNTHETIC_VOLUME, "Mountpoint": str(root / "state-backing")}
        (root / "state-backing").mkdir(exist_ok=True)
        self.records: dict[str, dict[str, Any]] = {}
        self.stops: list[str] = []

    def record(
        self,
        cid: str,
        role: str | None = None,
        *,
        running: bool = False,
        rw: bool = True,
        exit_code: int = 0,
    ) -> dict[str, Any]:
        return {
            "Id": cid,
            "Name": "/synthetic-" + cid[:8],
            "Created": "2026-01-01T00:00:00Z",
            "Image": "sha256:" + "8" * 64,
            "Config": {
                "Entrypoint": ["kodi-lab-broker"]
                if role == "broker"
                else ["kodi-lab-remote-web"],
                "Cmd": None,
                "Hostname": cid[:12],
                "Env": ["SYNTHETIC=1"],
                "Labels": {
                    "com.docker.compose.project": "owned-qa",
                    "com.docker.compose.service": role,
                    "com.docker.compose.project.working_dir": str(self.root),
                },
            },
            "HostConfig": {
                "PidMode": "",
                "CgroupParent": "",
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            },
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "qa-state",
                    "Source": self.volume["Mountpoint"],
                    "Destination": "/var/lib/kodi-lab",
                    "RW": rw,
                }
            ],
            "RestartCount": 0,
            "State": {
                "Running": running,
                "Restarting": False,
                "Paused": False,
                "Pid": 100 if running else 0,
                "Status": "running" if running else "exited",
                "ExitCode": exit_code,
                "StartedAt": "2026-01-02T00:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z"
                if running
                else "2026-01-03T00:00:00Z",
            },
        }

    def info(self) -> dict[str, str]:
        return {"ID": "separate-synthetic-engine"}

    def inspect_volume(self, name: str) -> dict[str, Any]:
        assert name == "qa-state"
        return copy.deepcopy(self.volume)

    def containers(self, *, all: bool) -> list[dict[str, Any]]:
        assert all
        return [
            {"Id": cid, "Mounts": copy.deepcopy(row["Mounts"])}
            for cid, row in self.records.items()
        ]

    def inspect_container(self, cid: str) -> dict[str, Any]:
        matches = [row for key, row in self.records.items() if key.startswith(cid)]
        assert len(matches) == 1, cid
        return copy.deepcopy(matches[0])

    def stop(self, cid: str, *, timeout: int) -> None:
        assert cid in {"c" * 64, "d" * 64} and timeout == -1
        self.stops.append(cid)
        self.records[cid]["State"].update(
            Running=False, Pid=0, Status="exited", FinishedAt=stamp(datetime.now(UTC))
        )

    def logs(self, *_args: Any, **_kwargs: Any) -> bytes:
        return b'{"event":"broker request completed","broker_result_code":"success"}'

    def close(self) -> None:
        pass

    def get_archive(
        self, _cid: str, path: str
    ) -> tuple[list[bytes], dict[str, object]]:
        if path == "/etc/passwd":
            data = (
                b"root:x:0:0::/root:/bin/sh\n"
                b"kodi-lab:x:10001:10001::/nonexistent:/usr/sbin/nologin\n"
            )
        elif path == "/etc/group":
            data = b"root:x:0:\nkodi-lab:x:10001:\n"
        else:
            from kodi_lab.snapshot_recovery_host import LEGACY_303_BROKER_SOURCES

            name = path.rsplit("/", 1)[1]
            assert name in LEGACY_303_BROKER_SOURCES
            data = (
                Path(__file__).parents[1] / "src/kodi_lab/broker" / name
            ).read_bytes()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            member = tarfile.TarInfo(path.rsplit("/", 1)[1])
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return [output.getvalue()], {}


def profile(
    *,
    content: bytes = b"original",
    uid: int = 1000,
    mode: int = 0o600,
    extra: bool = False,
    metadata: bool = False,
) -> bytes:
    output = io.BytesIO()
    entries: list[tuple[str, bytes | None]] = [(".kodi", None)]
    if metadata:
        entries.extend(
            [
                (".kodi/addons", None),
                (".kodi/addons/skin.synthetic", None),
                (
                    ".kodi/addons/skin.synthetic/addon.xml",
                    b'<addon id="skin.synthetic" version="2.1.0"/>',
                ),
            ]
        )
        content = (
            b'<settings><setting id="lookandfeel.skin">'
            b"skin.synthetic</setting></settings>"
        )
    entries.extend(
        [(".kodi/userdata", None), (".kodi/userdata/guisettings.xml", content)]
    )
    if metadata:
        entries.append(
            (
                ".kodi/userdata/profiles.xml",
                (
                    b"<profiles><lastloaded>0</lastloaded><profile><id>0</id>"
                    b"<name>Synthetic master</name></profile></profiles>"
                ),
            )
        )
    if extra:
        entries.append((".kodi/userdata/extra", b"x"))
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path, payload in entries:
            item = tarfile.TarInfo(path)
            item.type = tarfile.DIRTYPE if payload is None else tarfile.REGTYPE
            item.mode = 0o755 if payload is None else mode
            item.uid, item.gid = uid, 1000
            item.size = len(payload) if payload else 0
            archive.addfile(item, None if payload is None else io.BytesIO(payload))
    return output.getvalue()


@dataclass
class Provider(DockerManagedContainerAdapter):
    cid: str | None = None
    calls: list[str] = field(default_factory=list)
    payload: bytes = field(default_factory=profile)
    fault: str | None = None
    consumer_client: Any = None
    authoritative_records: tuple[ManagedContainerRecord, ...] | None = None

    def list_managed_containers(
        self, *, managed_instance_id: str | None = None
    ) -> tuple[ManagedContainerRecord, ...]:
        if self.authoritative_records is not None:
            return self.authoritative_records
        if self.fault == "inventory":
            raise RuntimeError("inventory unavailable")
        return (
            ()
            if self.cid is None
            else (
                ManagedContainerRecord(
                    str(INSTANCE_ID),
                    self.cid,
                    IMAGE_ID,
                    ContainerRuntimeState.STOPPED,
                    ContainerHealthState.NOT_CONFIGURED,
                    str(REVISION_ID),
                ),
            )
        )

    def recovery_engine_id(self) -> str:
        return "separate-synthetic-engine"

    def recovery_environment(
        self,
        installation: str,
        state_volume: str,
        web_hostname: str,
        consumer_proof: StateConsumerProof,
    ) -> dict[str, Any]:
        if self.fault == "dead_broker":
            raise RuntimeError("broker is unavailable")
        if self.consumer_client is not None:
            return DockerManagedContainerAdapter(
                lambda: self.consumer_client
            ).recovery_environment(
                installation, state_volume, web_hostname, consumer_proof
            )
        return {
            "engine_id": self.recovery_engine_id(),
            "state_volume_digest": SYNTHETIC_VOLUME_DIGEST,
            "state_consumers_digest": digest(consumer_proof.model_dump()),
            "broker_container_id": "e" * 64,
            "web_container_id": "f" * 64,
        }

    def recovery_runtime_preflight(self, image_id: str) -> None:
        if self.fault == "runtime_missing":
            raise RecoveryRefused("recovery_runtime_unavailable")

    def provision_managed_container(
        self,
        spec: ManagedContainerProvisioningSpec,
        *,
        recovery_guard: Callable[[], None] | None = None,
    ) -> ProvisionedManagedContainerRecord:
        if recovery_guard is not None:
            recovery_guard()
        self.calls.append("provision")
        assert self.cid is None
        self.cid = "b" * 64
        if self.fault == "create_lost_reply":
            raise TimeoutError("after actual simulated create")
        return ProvisionedManagedContainerRecord(
            spec.managed_instance_id,
            spec.runtime_revision_id,
            self.cid,
            spec.image_id,
            ContainerRuntimeState.STOPPED,
            True,
        )

    def apply_managed_instance_snapshot(
        self,
        *,
        spec: SnapshotRestoreSpec,
        expected_container_id: str,
        recovery_verification: bool = False,
        verify_only: bool = False,
        recovery_guard: Callable[[], None] | None = None,
    ) -> AppliedManagedInstanceSnapshotRecord:
        step = "verify" if verify_only else "apply"
        self.calls.append(step)
        assert recovery_verification and expected_container_id == self.cid
        if recovery_guard is not None:
            recovery_guard()
        if self.fault == step:
            raise TimeoutError(step)
        checked = validate_published_snapshot(
            data_root=spec.data_root,
            context=spec.context,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
        count = validate_restored_snapshot_source(
            source_chunks=[self.payload],
            expected_members=checked.members,
            expected_owner=(1000, 1000),
        )
        return AppliedManagedInstanceSnapshotRecord(
            str(INSTANCE_ID),
            str(REVISION_ID),
            expected_container_id,
            spec.artifact,
            count,
        )


@dataclass
class Case:
    sessions: sessionmaker[Session]
    root: Path
    review: LegacyCaptureReview
    seal: RecoveryDrainSeal
    epoch_id: str
    actor: str
    token: str
    auth: WebAuthenticationService
    provider: Provider
    service: SnapshotRecoveryService = field(init=False)
    executor: RecoveryStepExecutor
    consumer_engine: ConsumerEngine
    wire_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def managed_container_inventory(self, **_kwargs: Any) -> Any:
        raise AssertionError("ordinary broker calls must not run in restricted Web")

    def recovery_inventory(self) -> tuple[ManagedContainerRecord, ...]:
        return self.provider.list_managed_containers()

    def snapshot_recovery_attestation(
        self, epoch_id: str, challenge: str
    ) -> RecoveryAttestation:
        return self.executor.attest(
            RecoveryAttestationParams.model_validate(
                {"epoch_id": epoch_id, "challenge": challenge}
            )
        )

    def snapshot_recovery_step(
        self, recovery_id: str, step: str, attempt_id: str, request_id: str
    ) -> object:
        self.wire_calls.append((step, attempt_id, request_id))
        return self.executor.execute(
            RecoveryStepParams.model_validate(
                {"recovery_id": recovery_id, "step": step, "attempt_id": attempt_id}
            ),
            request_id,
        )

    def claim(self) -> dict[str, Any]:
        preview = self.service.preview(self.epoch_id, self.actor)
        result, dispatch = self.service.confirm(
            preview["recovery_id"], self.actor, preview["fingerprint"], str(uuid4())
        )
        assert dispatch
        return result


@pytest.fixture
def recovery_case(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Case:
    monkeypatch.setattr(
        "kodi_lab.storage.snapshot_recovery.require_managed_runtime",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "kodi_lab.auth.passwords.PasswordHasher",
        lambda **_kw: PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1),
    )
    select_session(factory)
    now = datetime.now(UTC)
    with factory.begin() as session:
        initial_selection = session.get(DevelopmentSessionRow, 55)
        assert initial_selection is not None
        initial_workspace = session.get(
            AgentWorkspaceRow, initial_selection.workspace_id
        )
        assert initial_workspace is not None
        if getattr(request, "param", False) is True:
            initial_workspace.source_observed_version = "2.1.1"
        session.flush()
        attribution = {
            "selected_source": values(
                session.get(DevelopmentSourceRow, initial_selection.source_id)
            ),
            "selected_workspace": values(initial_workspace),
            "policy": [values(session.get(DevelopmentSessionPolicyRow, 1))],
        }
        session.add(ManagedResourcePolicyRow(id=1, max_running=1))
        failed = SnapshotRestoreOperationRow(
            id=str(uuid4()),
            snapshot_id=str(SNAPSHOT_ID),
            managed_instance_id=str(INSTANCE_ID),
            requested_at=stamp(now),
            started_at=stamp(now),
            heartbeat_at=stamp(now),
            completed_at=stamp(now),
            prior_source_runtime_state="stopped",
            old_container_id=OLD_ID,
            state="reconciliation_required",
            phase="provisioning",
            reconciliation_required=True,
            failure_reason=(
                "Snapshot restore could not be completed after source removal."
            ),
        )
        session.add(failed)
        session.flush()
        receipt = values(failed)
        selection = values(session.get(DevelopmentSessionRow, 55))
        snapshot = session.get(SnapshotRow, str(SNAPSHOT_ID))
        assert snapshot is not None
        context = SnapshotCaptureContext(
            SNAPSHOT_ID,
            INSTANCE_ID,
            REVISION_ID,
            "21.3",
            None,
            datetime(2026, 9, 16, tzinfo=UTC),
            "stopped",
        )
        payload = profile(metadata=getattr(request, "param", False) is True)
        artifact = publish_snapshot_from_source(
            data_root=tmp_path,
            context=context,
            expected_members=enumerate_snapshot_source([payload]),
            source_chunks=[payload],
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
        snapshot.archive_digest, snapshot.manifest_digest = (
            artifact.archive_digest,
            artifact.manifest_digest,
        )
        snapshot.archive_size, snapshot.member_count = (
            artifact.archive_size,
            artifact.member_count,
        )
    provider = Provider(payload=payload)
    # Real ordinary before-create refusal with absent bound CID and no bypass.
    capacity = ManagedCapacityAdmission(factory, provider.list_managed_containers)
    with pytest.raises(ManagedCapacityError) as refusal:
        capacity.run_provision(
            instance_id=str(INSTANCE_ID),
            revision_id=str(REVISION_ID),
            dispatch=lambda: pytest.fail("legacy create reached"),
        )
    assert refusal.value.code == "development_session_identity_changed"
    removed_id, refused_id = str(uuid4()), str(uuid4())
    common = {
        "event": "broker request completed",
        "broker_peer_uid": 10001,
        "broker_peer_gid": 10001,
        "broker_peer_pid": 0,
        "broker_protocol_version": 1,
        "broker_managed_instance_id": str(INSTANCE_ID),
    }
    events = [
        {
            **common,
            "broker_operation": "deprovision_managed_container",
            "broker_request_id": removed_id,
            "broker_result_code": "success",
            "broker_container_removed": True,
            "broker_runtime_revision_id": str(REVISION_ID),
        },
        {
            **common,
            "broker_operation": "provision_managed_container",
            "broker_request_id": refused_id,
            "broker_result_code": refusal.value.code,
        },
    ]
    operation = {
        "operation_id": receipt["id"],
        "snapshot_id": str(SNAPSHOT_ID),
        "managed_instance_id": str(INSTANCE_ID),
        "requested_at": receipt["requested_at"],
    }
    captures = {
        "attribution": attribution,
        "receipt": receipt,
        "selection": {"latest_generation": 55, "current": selection},
        "accepted": {"operation": {**operation, "state": "queued"}},
        "terminal": {
            **operation,
            "state": receipt["state"],
            "phase": receipt["phase"],
            "completed_at": receipt["completed_at"],
        },
        "intent": {
            "target": str(INSTANCE_ID),
            "snapshot": str(SNAPSHOT_ID),
            "prior_state": "stopped",
            "confirmed": True,
        },
        "before": {"nonterminal_operations": {}, "active_leases_or_reservations": {}},
        "native_stop": {
            "stop_receipt": {
                "stop_confirmed": True,
                "status": {
                    "target_id": str(INSTANCE_ID),
                    "profile_identity": {
                        "container_id": OLD_ID,
                        "runtime_revision_id": str(REVISION_ID),
                        "image_id": IMAGE_ID,
                    },
                },
            }
        },
    }
    review = LegacyCaptureReview(
        historical_commit="30316b88d20cc5fb4d8ed5a96e2b971cf2978afb",
        removal_request_id=removed_id,
        refusal_request_id=refused_id,
        reviewer="synthetic independent reviewer",
        assessment_digest="e" * 64,
        correlation_method="reviewed_complete_primary_chain_v1",
        reviewed_snapshot=ReviewedSnapshotIdentity(
            snapshot_id=str(SNAPSHOT_ID),
            archive_digest=artifact.archive_digest,
            manifest_digest=artifact.manifest_digest,
            archive_size=artifact.archive_size,
            member_count=artifact.member_count,
            manifest_size=(tmp_path / "snapshots" / str(SNAPSHOT_ID) / "manifest.json")
            .stat()
            .st_size,
        ),
        captures={
            **{key: canonical(value) for key, value in captures.items()},
            "broker": "\n".join(canonical(e) for e in events),
            "web": (
                f'"POST /api/instances/{INSTANCE_ID}/snapshots/'
                f'{SNAPSHOT_ID}/restore HTTP/1.1" 202 Accepted\n'
                f'"GET /api/instances/{INSTANCE_ID}/snapshot-restore-operations/'
                f'{receipt["id"]} HTTP/1.1" 200 OK'
            ),
        },
    )
    from kodi_lab.snapshot_recovery_host import InstallationPlan, drain

    sdk = ConsumerEngine(tmp_path)
    sdk.records = {
        "c" * 64: sdk.record("c" * 64, "web", running=True),
        "d" * 64: sdk.record("d" * 64, "broker", running=True),
    }
    ancillary: tuple[ReviewedStateConsumer, ...] = ()
    if getattr(request, "param", None) in {
        "retained_consumer",
        "retained_bind_consumer",
        "retained_missing_consumer",
    }:
        sdk.records["2" * 64] = sdk.record("2" * 64, rw=False, exit_code=137)
        if request.param == "retained_bind_consumer":
            alias = tmp_path / "retained-state-alias"
            alias.symlink_to(Path(sdk.volume["Mountpoint"]), target_is_directory=True)
            sdk.records["2" * 64]["Mounts"] = [
                {
                    "Type": "bind",
                    "Source": str(alias),
                    "Destination": "/readonly-state",
                    "RW": False,
                }
            ]
        if request.param == "retained_missing_consumer":
            sdk.records["2" * 64]["Mounts"] = [
                {
                    "Type": "bind",
                    "Source": str(tmp_path / "missing-original-bind"),
                    "Destination": "/old-state",
                    "RW": True,
                }
            ]
        ancillary = (
            ReviewedStateConsumer(
                container_id="2" * 64,
                metadata_digest=consumer_metadata_digest(sdk.records["2" * 64]),
            ),
        )
    monkeypatch.setattr(
        "kodi_lab.snapshot_recovery_host._cgroup",
        lambda _pid, cid: ("/docker/" + cid, "10"),
    )
    monkeypatch.setattr(
        "kodi_lab.snapshot_recovery_host._empty_cgroup", lambda _path: True
    )
    plan = InstallationPlan(
        installation="owned-qa",
        bundle_root=str(tmp_path),
        compose_file=str(tmp_path / "compose.yml"),
        engine_id="separate-synthetic-engine",
        state_volume="qa-state",
        web_container_id="c" * 64,
        broker_container_id="d" * 64,
        candidate_package_digest=package_sha256(),
        review=review,
        ancillary_consumers=ancillary,
    )
    seal = drain(plan, sdk)
    sdk.records.update(
        {
            "f" * 64: sdk.record("f" * 64, "web", running=True),
            "e" * 64: sdk.record("e" * 64, "broker", running=True),
        }
    )
    sdk.stops.clear()
    provider.consumer_client = sdk
    monkeypatch.setattr("socket.gethostname", lambda: "e" * 12)
    epoch_id = seal_epoch(factory, review, seal)
    web_owner = canonical(
        {
            "boot": seal.boot_id,
            "start": "11",
            "init_start": "10",
            "pid": 7,
            "hostname": "f" * 12,
            "nonce": str(uuid4()),
        }
    )
    broker_owner = canonical(
        {
            "boot": seal.boot_id,
            "start": "12",
            "init_start": "10",
            "pid": 8,
            "hostname": "e" * 12,
            "nonce": str(uuid4()),
        }
    )
    with factory.begin() as session:
        boundary = session.get(SnapshotRecoveryEpochRow, epoch_id)
        assert boundary is not None
        boundary.web_owner, boundary.broker_owner, boundary.state = (
            web_owner,
            broker_owner,
            "serving",
        )
    auth = WebAuthenticationService(factory)
    auth.set_password("synthetic private administrator")
    login = auth.login("synthetic private administrator")
    assert login is not None
    executor = RecoveryStepExecutor(
        factory, provider, tmp_path, os.getgid(), epoch_id, broker_owner
    )
    case = Case(
        factory,
        tmp_path,
        review,
        seal,
        epoch_id,
        auth.token_digest(login.token),
        login.token,
        auth,
        provider,
        executor,
        sdk,
    )
    case.service = SnapshotRecoveryService(factory, case, tmp_path, os.getgid())
    case.service.owner_uid = os.getuid()
    return case

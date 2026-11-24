"""Exact broker-consumed one-shot steps; no new caller Docker/path authority."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal
from uuid import uuid4

from pydantic import UUID4, BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_recovery_evidence import (
    RecoveryRefused,
    canonical,
    digest,
)
from kodi_lab.application.snapshot_recovery_maintenance import RecoveryDrainSeal
from kodi_lab.broker.docker_inventory import (
    DockerManagedContainerAdapter,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.broker.provisioning import SqliteProvisioningResolver
from kodi_lab.broker.snapshots import SqliteSnapshotRestoreResolver
from kodi_lab.build_identity import package_sha256
from kodi_lab.storage.models import (
    ManagedProvisionReservationRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryEventRow,
    SnapshotRecoveryRow,
)
from kodi_lab.storage.snapshot_recovery import (
    active_epoch,
    check_bindings,
    checked,
    epoch,
    event,
    stamp,
)


class RecoveryAttestationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch_id: UUID4
    challenge: UUID4


class RecoveryInventoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    managed_instance_id: str
    container_id: str
    runtime_revision_id: str | None
    image_id: str
    runtime_state: str


class RecoveryAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch_id: str
    challenge: str
    package_digest: str
    engine_id: str
    boot_id: str
    installation: str
    state_volume: str
    state_volume_digest: str
    state_consumers_digest: str
    broker_owner: str
    containers: tuple[RecoveryInventoryEntry, ...]


def require_mode_operation(
    sessions: sessionmaker[Session], configured_epoch: str | None, operation: str
) -> None:
    with sessions() as session:
        restricted = configured_epoch is not None or active_epoch(session) is not None
        if restricted and operation not in {
            "ping",
            "broker_info",
            "capabilities",
            "managed_container_inventory",
            "inspect_legacy_snapshot_recovery",
            "execute_legacy_snapshot_recovery_step",
        }:
            raise RecoveryRefused("recovery_maintenance_restricted")


class RecoveryStepParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_id: UUID4
    step: Literal["provision", "apply", "verify"]
    attempt_id: UUID4


class RecoveryStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_id: str
    step: str
    state: Literal["completed", "uncertain"]


class RecoveryStepExecutor:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        provider: DockerManagedContainerAdapter,
        data_root: Path,
        owner_gid: int,
        epoch_id: str,
        broker_owner: str,
    ) -> None:
        self.sessions, self.provider = sessions, provider
        self.data_root, self.owner_gid = data_root, owner_gid
        self.epoch_id, self.broker_owner = epoch_id, broker_owner

    def attest(self, params: RecoveryAttestationParams) -> RecoveryAttestation:
        with self.sessions() as session:
            boundary = epoch(session, self.epoch_id)
            seal = RecoveryDrainSeal.model_validate_json(boundary.seal_json)
            owner = json.loads(self.broker_owner)
            web_owner = json.loads(boundary.web_owner or "null")
            if (
                str(params.epoch_id) != self.epoch_id
                or boundary.broker_owner != self.broker_owner
                or package_sha256() != seal.candidate_package_digest
                or seal.boot_id
                != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            ):
                raise RecoveryRefused("recovery_live_broker_unproven")
            environment = self.provider.recovery_environment(
                seal.installation,
                seal.state_volume,
                web_owner["hostname"],
                seal.state_consumers,
            )
            self.provider.recovery_runtime_preflight(
                json.loads(boundary.evidence_json)["native_identity"]["image_id"]
            )
            if (
                environment["engine_id"] != seal.engine_id
                or environment["state_volume_digest"] != seal.state_volume_digest
                or environment["state_consumers_digest"]
                != digest(seal.state_consumers.model_dump())
                or not environment["broker_container_id"].startswith(owner["hostname"])
                or not environment["web_container_id"].startswith(web_owner["hostname"])
            ):
                raise RecoveryRefused("recovery_installation_identity_changed")
            records = self.provider.list_managed_containers()
            return RecoveryAttestation(
                epoch_id=self.epoch_id,
                challenge=str(params.challenge),
                package_digest=seal.candidate_package_digest,
                engine_id=seal.engine_id,
                boot_id=seal.boot_id,
                installation=seal.installation,
                state_volume=seal.state_volume,
                state_volume_digest=seal.state_volume_digest,
                state_consumers_digest=environment["state_consumers_digest"],
                broker_owner=self.broker_owner,
                containers=tuple(
                    RecoveryInventoryEntry(
                        managed_instance_id=r.managed_instance_id,
                        container_id=r.container_id,
                        runtime_revision_id=r.runtime_revision_id,
                        image_id=r.image_id,
                        runtime_state=str(r.runtime_state),
                    )
                    for r in records
                ),
            )

    def _check(self, session: Session, row: SnapshotRecoveryRow) -> dict[str, Any]:
        proof = checked(session, row, datetime.now(UTC))
        if row.epoch_id != self.epoch_id or proof["broker_owner"] != self.broker_owner:
            raise RecoveryRefused("recovery_broker_owner_changed")
        live = self.attest(
            RecoveryAttestationParams.model_validate(
                {"epoch_id": self.epoch_id, "challenge": str(uuid4())}
            )
        )
        check_bindings(session, row, live.containers, self.data_root)
        return proof

    @contextmanager
    def _heartbeat(self, recovery_id: str) -> Iterator[None]:
        stopped, lost = Event(), Event()

        def maintain() -> None:
            while not stopped.wait(5):
                try:
                    with self.sessions.begin() as session:
                        session.execute(text("BEGIN IMMEDIATE"))
                        row = session.get(SnapshotRecoveryRow, recovery_id)
                        if row is None:
                            raise RecoveryRefused("recovery_not_found")
                        checked(session, row, datetime.now(UTC))
                except Exception:
                    lost.set()
                    return

        worker = Thread(target=maintain, name="snapshot-recovery-lease", daemon=False)
        worker.start()
        try:
            yield
            if lost.is_set():
                raise RecoveryRefused("recovery_exact_lease_lost")
        finally:
            stopped.set()
            worker.join()

    def execute(
        self, params: RecoveryStepParams, wire_request_id: str
    ) -> RecoveryStepResult:
        recovery_id, attempt = str(params.recovery_id), str(params.attempt_id)
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(SnapshotRecoveryRow, recovery_id)
            dispatch = session.get(SnapshotRecoveryDispatchRow, attempt)
            if (
                row is None
                or dispatch is None
                or dispatch.recovery_id != recovery_id
                or dispatch.step != params.step
                or dispatch.request_id != wire_request_id
            ):
                raise RecoveryRefused("recovery_dispatch_identity_invalid")
            if dispatch.state != "prepared":
                return RecoveryStepResult(
                    recovery_id=recovery_id,
                    step=params.step,
                    state="completed" if dispatch.state == "completed" else "uncertain",
                )
            expected = {
                "provision": "claimed",
                "apply": "provisioned",
                "verify": "applied",
            }[params.step]
            if (
                row.state != expected
                or dispatch.deadline <= stamp(datetime.now(UTC))
                or dispatch.intent_digest
                != digest(
                    {
                        "fingerprint": row.fingerprint,
                        "step": params.step,
                        "container": row.container_id,
                    }
                )
            ):
                raise RecoveryRefused("recovery_dispatch_stale")
            proof = self._check(session, row)
            target, cid = row.target_id, row.container_id
            snapshot = proof["receipt"]["snapshot_id"]
            dispatch.state = "consumed"
            event(
                session,
                row.id,
                f"{params.step}_consumed",
                datetime.now(UTC),
                {"attempt": attempt, "wire_request": wire_request_id},
            )

        def native_guard(*, provision_reserved: bool = False) -> None:
            with self.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                current = session.get(SnapshotRecoveryRow, recovery_id)
                intent = session.get(SnapshotRecoveryDispatchRow, attempt)
                if (
                    current is None
                    or intent is None
                    or intent.state != "consumed"
                    or intent.deadline <= stamp(datetime.now(UTC))
                ):
                    raise RecoveryRefused("recovery_native_entry_expired")
                self._check(session, current)

                reservation = session.get(
                    ManagedProvisionReservationRow, current.target_id
                )
                if provision_reserved and reservation is None:
                    raise RecoveryRefused("recovery_provision_admission_missing")
                if params.step == "provision" and reservation is not None:
                    if (
                        reservation.operation_id != current.id
                        or reservation.session_generation
                        != current.successor_generation
                        or reservation.runtime_revision_id
                        != proof["selection"]["runtime_revision_id"]
                        or reservation.phase != "reserved"
                    ):
                        raise RecoveryRefused("recovery_provision_admission_changed")
                    admission = {
                        "generation": current.successor_generation,
                        "target": current.target_id,
                        "revision": proof["selection"]["runtime_revision_id"],
                        "source": proof["selection"]["source_id"],
                        "workspace": proof["selection"]["workspace_id"],
                        "lease_digest": current.lease_digest,
                        "attempt": attempt,
                        "wire_request": wire_request_id,
                    }
                    previous = session.scalar(
                        select(SnapshotRecoveryEventRow).where(
                            SnapshotRecoveryEventRow.recovery_id == current.id,
                            SnapshotRecoveryEventRow.event == "provision_admitted",
                        )
                    )
                    if previous is None:
                        event(
                            session,
                            current.id,
                            "provision_admitted",
                            datetime.now(UTC),
                            admission,
                        )
                    elif previous.detail_json != canonical(admission):
                        raise RecoveryRefused("recovery_provision_admission_changed")

        observed_create: dict[str, Any] | None = None
        try:
            with self._heartbeat(recovery_id):
                native_guard()
                outcome: dict[str, Any]
                if params.step == "provision":

                    def bind_created(
                        session: Session, result: ProvisionedManagedContainerRecord
                    ) -> None:
                        nonlocal observed_create
                        observed_create = {
                            "container": result.container_id,
                            "image": result.image_id,
                            "revision": result.runtime_revision_id,
                            "created": result.created,
                            "state": str(result.runtime_state),
                        }
                        current = session.get(SnapshotRecoveryRow, recovery_id)
                        if current is None:
                            raise RecoveryRefused("recovery_not_found")
                        checked(session, current, datetime.now(UTC))
                        records = self.provider.list_managed_containers()
                        selected = [
                            r for r in records if r.managed_instance_id == target
                        ]
                        check_bindings(
                            session,
                            current,
                            [r for r in records if r.managed_instance_id != target],
                            self.data_root,
                        )
                        if (
                            len(selected) != 1
                            or selected[0].container_id != result.container_id
                            or selected[0].runtime_revision_id
                            != result.runtime_revision_id
                            or selected[0].image_id != result.image_id
                            or selected[0].runtime_state != "stopped"
                            or not result.created
                            or result.container_id
                            == proof["receipt"]["old_container_id"]
                        ):
                            raise RecoveryRefused("recovery_create_result_unproven")
                        current.container_id = result.container_id

                    spec = SqliteProvisioningResolver(self.sessions).resolve(target)
                    capacity = ManagedCapacityAdmission(
                        self.sessions, self.provider.list_managed_containers
                    )
                    result = capacity.run_provision(
                        instance_id=target,
                        revision_id=spec.runtime_revision_id,
                        dispatch=lambda: self.provider.provision_managed_container(
                            spec,
                            recovery_guard=lambda: native_guard(
                                provision_reserved=True
                            ),
                        ),
                        parent_operation_id=recovery_id,
                        before_dispatch=lambda: native_guard(provision_reserved=True),
                        validate_completion=bind_created,
                    )
                    if (
                        not result.created
                        or result.container_id == proof["receipt"]["old_container_id"]
                        or result.image_id != proof["bindings"]["artifact"]["image_id"]
                        or result.runtime_state != "stopped"
                    ):
                        raise RecoveryRefused("recovery_create_result_unproven")
                    outcome = {
                        "container": result.container_id,
                        "image": result.image_id,
                        "revision": result.runtime_revision_id,
                        "created": True,
                        "state": "stopped",
                    }
                else:
                    if cid is None:
                        raise RecoveryRefused("recovery_successor_missing")
                    spec_restore = SqliteSnapshotRestoreResolver(
                        self.sessions,
                        data_root=self.data_root,
                        owner_gid=self.owner_gid,
                    ).resolve(snapshot, target)
                    # The provider independently validates, reads fixed destination
                    # and checks full inventory, ownership, modes and DBs.
                    applied = self.provider.apply_managed_instance_snapshot(
                        spec=spec_restore,
                        expected_container_id=cid,
                        recovery_verification=True,
                        verify_only=params.step == "verify",
                        recovery_guard=native_guard,
                    )
                    if (
                        applied.container_id != cid
                        or applied.artifact != spec_restore.artifact
                        or applied.managed_instance_id != target
                        or applied.runtime_revision_id
                        != proof["bindings"]["instance"]["runtime_revision_id"]
                        or applied.recognized_databases_checked
                        != proof["artifact_proof"]["recognized_databases"]
                    ):
                        raise RecoveryRefused("recovery_apply_proof_mismatch")
                    outcome = {
                        "container": cid,
                        "archive": applied.artifact.archive_digest,
                        "manifest": applied.artifact.manifest_digest,
                        "length": applied.artifact.archive_size,
                        "manifest_length": proof["artifact_proof"]["manifest_length"],
                        "count": applied.artifact.member_count,
                        "databases": applied.recognized_databases_checked,
                        "inventory_digest": proof["artifact_proof"]["inventory_digest"],
                        "ownership": [1000, 1000],
                        "safe_modes": True,
                        "state": "stopped",
                    }
                with self.sessions.begin() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    current = session.get(SnapshotRecoveryRow, recovery_id)
                    dispatch = session.get(SnapshotRecoveryDispatchRow, attempt)
                    if (
                        current is None
                        or dispatch is None
                        or dispatch.state != "consumed"
                    ):
                        raise RecoveryRefused("recovery_dispatch_changed")
                    if params.step == "provision":
                        current.container_id = outcome["container"]
                    self._check(session, current)
                    current.state = {
                        "provision": "provisioned",
                        "apply": "applied",
                        "verify": "verified",
                    }[params.step]
                    dispatch.state, dispatch.result_json = (
                        "completed",
                        canonical(outcome),
                    )
                    event(
                        session,
                        recovery_id,
                        f"{params.step}_completed",
                        datetime.now(UTC),
                        outcome,
                    )
        except BaseException:
            with self.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                dispatch = session.get(SnapshotRecoveryDispatchRow, attempt)
                row = session.get(SnapshotRecoveryRow, recovery_id)
                if dispatch is not None and dispatch.state == "consumed":
                    dispatch.state = "uncertain"
                    if observed_create is not None:
                        dispatch.result_json = canonical(
                            {"unaccepted_create_observation": observed_create}
                        )
                if row is not None and row.state not in {
                    "succeeded",
                    "reconciliation_required",
                }:
                    row.state, row.failure_code = (
                        "reconciliation_required",
                        "recovery_native_result_uncertain",
                    )
                    event(session, row.id, "reconciliation_required", datetime.now(UTC))
            raise
        return RecoveryStepResult(
            recovery_id=recovery_id, step=params.step, state="completed"
        )

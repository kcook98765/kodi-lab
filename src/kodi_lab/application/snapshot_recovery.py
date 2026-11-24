"""New one-use recovery of a reviewed legacy failure, with no destructive replay."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tarfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5
from xml.etree import ElementTree

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_recovery_evidence import (
    RecoveryRefused,
    canonical,
    digest,
)
from kodi_lab.application.snapshot_restore_sessions import RestoreSessionBinding
from kodi_lab.broker.snapshots import SqliteSnapshotRestoreResolver
from kodi_lab.domain import GlobalHeavyOperationKind, ManagedInstanceMutationKind
from kodi_lab.snapshot.storage import SnapshotStorageError, snapshot_directory
from kodi_lab.snapshot.validation import validate_published_snapshot
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryResolutionRow,
    SnapshotRecoveryRow,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.storage.retained_development import RetainedContainer
from kodi_lab.storage.snapshot_recovery import (
    LEASE_DURATION,
    check_bindings,
    checked,
    epoch,
    event,
    require_actor,
    reviewed_state,
    stamp,
    verified_resolution,
)


class RecoveryBroker(Protocol):
    def recovery_inventory(self) -> Sequence[RetainedContainer]: ...

    def snapshot_recovery_attestation(
        self, epoch_id: str, challenge: str
    ) -> object: ...

    def snapshot_recovery_step(
        self, recovery_id: str, step: str, attempt_id: str, request_id: str
    ) -> Any: ...


class SnapshotRecoveryService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: RecoveryBroker,
        data_root: Path,
        owner_gid: int,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions, self.broker = sessions, broker
        self.data_root, self.owner_gid, self.clock = data_root, owner_gid, clock
        self.owner_uid = 0

    def _inventory(
        self, session: Session, epoch_id: str
    ) -> Sequence[RetainedContainer]:
        from kodi_lab.application.snapshot_recovery_maintenance import RecoveryDrainSeal
        from kodi_lab.broker.snapshot_recovery import RecoveryAttestation
        from kodi_lab.build_identity import package_sha256

        boundary = epoch(session, epoch_id)
        seal = RecoveryDrainSeal.model_validate_json(boundary.seal_json)
        challenge = str(uuid4())
        try:
            reply = self.broker.snapshot_recovery_attestation(epoch_id, challenge)
            if not isinstance(reply, RecoveryAttestation):
                reply = RecoveryAttestation.model_validate(reply)
        except Exception as error:
            raise RecoveryRefused("recovery_live_broker_unproven") from error
        if (
            reply.epoch_id != epoch_id
            or reply.challenge != challenge
            or reply.broker_owner != boundary.broker_owner
            or reply.package_digest != package_sha256()
            or reply.package_digest != seal.candidate_package_digest
            or reply.engine_id != seal.engine_id
            or reply.boot_id != seal.boot_id
            or reply.installation != seal.installation
            or reply.state_volume != seal.state_volume
            or reply.state_volume_digest != seal.state_volume_digest
            or reply.state_consumers_digest != digest(seal.state_consumers.model_dump())
        ):
            raise RecoveryRefused("recovery_live_broker_unproven")
        return reply.containers

    def _artifact(self, snapshot: str, target: str) -> dict[str, Any]:
        spec = SqliteSnapshotRestoreResolver(
            self.sessions,
            data_root=self.data_root,
            owner_gid=self.owner_gid,
            owner_uid=self.owner_uid,
        ).resolve(snapshot, target)
        try:
            verified = validate_published_snapshot(
                data_root=self.data_root,
                context=spec.context,
                owner_gid=self.owner_gid,
                owner_uid=self.owner_uid,
            )
        except SnapshotStorageError as error:
            raise RecoveryRefused(error.code) from error
        if verified.artifact != spec.artifact:
            raise RecoveryRefused("recovery_artifact_changed")
        directory = snapshot_directory(self.data_root, UUID(snapshot))
        profile_metadata: dict[str, str | None] = {
            "selected_skin": None,
            "selected_skin_version": None,
            "active_profile": None,
            "metadata": "not_recorded",
        }
        with os.fdopen(
            os.open(directory / "state.tar", os.O_RDONLY | os.O_NOFOLLOW), "rb"
        ) as raw:
            with tarfile.open(fileobj=raw, mode="r:") as archive:

                def xml(path: str) -> ElementTree.Element | None:
                    try:
                        member = archive.getmember(path)
                        if not member.isfile() or member.size > 512 * 1024:
                            return None
                        stream = archive.extractfile(member)
                        if stream is None:
                            return None
                        data = stream.read(512 * 1024 + 1)
                        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                            return None
                        return ElementTree.fromstring(data)
                    except (KeyError, ElementTree.ParseError):
                        return None

                settings = xml("userdata/guisettings.xml")
                skin = (
                    None
                    if settings is None
                    else settings.findtext(".//setting[@id='lookandfeel.skin']")
                )
                if skin and re.fullmatch(r"[a-z0-9._-]{1,128}", skin):
                    profile_metadata["selected_skin"] = skin
                    addon = xml(f"addons/{skin}/addon.xml")
                    if addon is not None and addon.get("id") == skin:
                        version = addon.get("version")
                        if version and len(version) <= 64 and version.isprintable():
                            profile_metadata["selected_skin_version"] = version
                profiles = xml("userdata/profiles.xml")
                if profiles is not None:
                    last = profiles.findtext("lastloaded")
                    entries = profiles.findall("profile")
                    for index, item in enumerate(entries):
                        if (item.findtext("id") or str(index)) == last:
                            name = item.findtext("name")
                            if name and len(name) <= 128 and name.isprintable():
                                profile_metadata["active_profile"] = name
                if any(
                    profile_metadata[key] is not None
                    for key in (
                        "selected_skin",
                        "selected_skin_version",
                        "active_profile",
                    )
                ):
                    profile_metadata["metadata"] = "read_from_exact_snapshot"
            raw.seek(0)
            if (
                "sha256:" + hashlib.file_digest(raw, "sha256").hexdigest()
                != spec.artifact.archive_digest
            ):
                raise RecoveryRefused("recovery_artifact_changed")
        return {
            "archive": spec.artifact.archive_digest,
            "manifest": spec.artifact.manifest_digest,
            "length": spec.artifact.archive_size,
            "manifest_length": (directory / "manifest.json")
            .stat(follow_symlinks=False)
            .st_size,
            "count": spec.artifact.member_count,
            "recognized_databases": verified.recognized_databases_checked,
            "ownership": [1000, 1000],
            "snapshot_profile": profile_metadata,
            "inventory_digest": digest(
                [
                    {
                        "path": m.path,
                        "type": m.member_type,
                        "size": m.size,
                        "mode": m.mode,
                        "sha256": m.sha256,
                    }
                    for m in verified.members
                ]
            ),
        }

    def preview(self, epoch_id: str, actor: str) -> dict[str, Any]:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            now = self.clock()
            require_actor(session, actor, now)
            boundary = epoch(session, epoch_id)
            proof = reviewed_state(
                session, boundary, self._inventory(session, epoch_id), self.data_root
            )
            proof["artifact_proof"] = self._artifact(
                proof["receipt"]["snapshot_id"], proof["receipt"]["managed_instance_id"]
            )
            if (
                proof["artifact_proof"]["manifest_length"]
                != proof["reviewed_manifest_size"]
            ):
                raise RecoveryRefused("recovery_original_snapshot_changed")
            row = SnapshotRecoveryRow(
                id=str(uuid4()),
                epoch_id=epoch_id,
                incident_id=boundary.incident_id,
                target_id=proof["receipt"]["managed_instance_id"],
                actor_digest=actor,
                fingerprint=digest(proof),
                proof_json=canonical(proof),
                created_at=stamp(now),
                expires_at=stamp(now + timedelta(minutes=5)),
                state="preview",
            )
            session.add(row)
            session.flush()
            event(session, row.id, "preview", now)
            return self.status_row(row)

    def revoke(self, recovery_id: str, actor: str) -> None:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(SnapshotRecoveryRow, recovery_id)
            if row is None or row.actor_digest != actor or row.state != "preview":
                raise RecoveryRefused("recovery_review_not_revocable")
            require_actor(session, actor, self.clock())
            row.state = "revoked"
            event(session, row.id, "revoked", self.clock())

    def confirm(
        self, recovery_id: str, actor: str, fingerprint: str, request_id: str
    ) -> tuple[dict[str, Any], bool]:
        try:
            with self.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                now = self.clock()
                require_actor(session, actor, now)
                row = session.get(SnapshotRecoveryRow, recovery_id)
                if (
                    row is None
                    or row.actor_digest != actor
                    or row.fingerprint != fingerprint
                ):
                    raise RecoveryRefused("recovery_review_mismatch")
                if row.consumed_at is not None:
                    if row.request_id != request_id:
                        raise RecoveryRefused("recovery_request_conflict")
                    return self.status_row(
                        row, verified_resolution(session, row.incident_id)
                    ), False
                if row.state != "preview" or row.expires_at <= stamp(now):
                    raise RecoveryRefused("recovery_consent_expired_or_revoked")
                boundary = epoch(session, row.epoch_id)
                proof = reviewed_state(
                    session,
                    boundary,
                    self._inventory(session, row.epoch_id),
                    self.data_root,
                )
                proof["artifact_proof"] = self._artifact(
                    proof["receipt"]["snapshot_id"], row.target_id
                )
                if digest(proof) != fingerprint:
                    raise RecoveryRefused("recovery_preview_stale")
                now = self.clock()
                require_actor(session, actor, now)
                if row.expires_at <= stamp(now):
                    raise RecoveryRefused("recovery_consent_expired_or_revoked")
                row.request_id, row.consumed_at, row.state = (
                    request_id,
                    stamp(now),
                    "claimed",
                )
                row.lease_digest = digest(secrets.token_urlsafe(32))
                mutations = ManagedInstanceMutationLeaseRepository(session)
                heavy = GlobalHeavyOperationLeaseRepository(session)
                mutations.reserve(
                    UUID(row.target_id),
                    kind=ManagedInstanceMutationKind.SNAPSHOT_RECOVERY,
                    operation_id=UUID(row.id),
                )
                heavy.reserve(
                    kind=GlobalHeavyOperationKind.SNAPSHOT_RECOVERY,
                    operation_id=UUID(row.id),
                )
                if not (
                    mutations.claim(
                        UUID(row.target_id),
                        operation_id=UUID(row.id),
                        token_digest=row.lease_digest,
                        now=now,
                        expires_at=now + LEASE_DURATION,
                    )
                    and heavy.claim(
                        operation_id=UUID(row.id),
                        token_digest=row.lease_digest,
                        now=now,
                        expires_at=now + LEASE_DURATION,
                    )
                ):
                    raise RecoveryRefused("recovery_claim_lease_conflict")
                # This is new reviewed admission; no historical capture() is invented.
                self._inventory(session, row.epoch_id)
                successor = RestoreSessionBinding.read(session).handoff(
                    session, UUID(row.id), now
                )
                if successor.selection is None:
                    raise RecoveryRefused("recovery_selected_session_required")
                row.successor_generation = successor.selection.generation
                self._invalidate_confidence(session, row.target_id)
                event(
                    session,
                    row.id,
                    "claimed_successor_reserved",
                    now,
                    {
                        "generation": row.successor_generation,
                        "actor": row.actor_digest,
                        "fingerprint": row.fingerprint,
                        "request": row.request_id,
                    },
                )
                session.flush()
                return self.status_row(row), True
        except IntegrityError as error:
            raise RecoveryRefused("recovery_claim_conflict") from error

    def execute(self, recovery_id: str) -> None:
        """Only the successful confirm caller schedules this, never startup/replay."""
        try:
            for step, expected in (
                ("provision", "claimed"),
                ("apply", "provisioned"),
                ("verify", "applied"),
            ):
                with self.sessions.begin() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    row = session.get(SnapshotRecoveryRow, recovery_id)
                    if row is None or row.state != expected:
                        raise RecoveryRefused("recovery_step_reentry_refused")
                    proof = checked(session, row, self.clock())
                    check_bindings(
                        session,
                        row,
                        self._inventory(session, row.epoch_id),
                        self.data_root,
                    )
                    attempt, request = str(uuid4()), str(uuid4())
                    session.add(
                        SnapshotRecoveryDispatchRow(
                            id=attempt,
                            recovery_id=row.id,
                            step=step,
                            request_id=request,
                            intent_digest=digest(
                                {
                                    "fingerprint": row.fingerprint,
                                    "step": step,
                                    "container": row.container_id,
                                }
                            ),
                            deadline=stamp(self.clock() + timedelta(minutes=2)),
                            state="prepared",
                        )
                    )
                    event(
                        session,
                        row.id,
                        f"{step}_prepared",
                        self.clock(),
                        {"attempt": attempt, "request": request},
                    )
                self.broker.snapshot_recovery_step(recovery_id, step, attempt, request)
            with self.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                row = session.get(SnapshotRecoveryRow, recovery_id)
                if row is None or row.state != "verified":
                    raise RecoveryRefused("recovery_verification_incomplete")
                proof = checked(session, row, self.clock())
                check_bindings(
                    session, row, self._inventory(session, row.epoch_id), self.data_root
                )
                if (
                    self._artifact(proof["receipt"]["snapshot_id"], row.target_id)
                    != proof["artifact_proof"]
                ):
                    raise RecoveryRefused("recovery_artifact_changed")
                checked(session, row, self.clock())
                row.state = "succeeded"
                session.flush()
                dispatches = list(
                    session.scalars(
                        select(SnapshotRecoveryDispatchRow).where(
                            SnapshotRecoveryDispatchRow.recovery_id == row.id
                        )
                    )
                )
                dispatches.sort(
                    key=lambda d: ("provision", "apply", "verify").index(d.step)
                )
                if len(dispatches) != 3 or any(
                    d.state != "completed" for d in dispatches
                ):
                    raise RecoveryRefused("recovery_dispatch_proof_incomplete")
                completed = self.clock()
                session.add(
                    SnapshotRecoveryResolutionRow(
                        incident_id=row.incident_id,
                        recovery_id=row.id,
                        relation="verified_original_snapshot_stopped_successor",
                        receipt_digest=digest(proof["receipt"]),
                        proof_digest=digest(
                            [json.loads(d.result_json or "null") for d in dispatches]
                        ),
                        completed_at=stamp(completed),
                    )
                )
                event(session, row.id, "succeeded_stopped", completed)
                self._release(session, row)
                event(session, row.id, "leases_released", completed)
        except Exception as error:
            self.fence(
                recovery_id,
                str(error)
                if isinstance(error, RecoveryRefused)
                else "recovery_dispatch_unconfirmed",
            )

    def fence(self, recovery_id: str, code: str) -> None:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(SnapshotRecoveryRow, recovery_id)
            if row is None or row.state in {
                "preview",
                "revoked",
                "succeeded",
                "reconciliation_required",
            }:
                return
            row.state, row.failure_code = "reconciliation_required", code[:64]
            event(
                session,
                row.id,
                "reconciliation_required",
                self.clock(),
                {"code": row.failure_code},
            )
            # Do not clear uncertain reservations or either ownership fence.

    def interrupted(self) -> None:
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(SnapshotRecoveryRow.id).where(
                        SnapshotRecoveryRow.consumed_at.is_not(None),
                        SnapshotRecoveryRow.state.not_in(
                            ("succeeded", "reconciliation_required")
                        ),
                    )
                )
            )
        for recovery_id in ids:
            self.fence(recovery_id, "recovery_worker_interrupted")

    @staticmethod
    def _invalidate_confidence(session: Session, target_id: str) -> None:
        from kodi_lab.application.addon_zip_installations import (
            invalidate_addon_zip_installation_evidence,
        )
        from kodi_lab.application.development_source_deployments import (
            invalidate_development_source_deployments,
        )
        from kodi_lab.application.managed_bridge_trust import (
            invalidate_managed_bridge_trust,
        )

        invalidate_development_source_deployments(session, UUID(target_id))
        invalidate_addon_zip_installation_evidence(session, UUID(target_id))
        invalidate_managed_bridge_trust(session, UUID(target_id))

    @staticmethod
    def _release(session: Session, row: SnapshotRecoveryRow) -> None:
        leases: tuple[
            ManagedInstanceMutationLeaseRow | GlobalHeavyOperationLeaseRow | None, ...
        ] = (
            session.get(ManagedInstanceMutationLeaseRow, row.target_id),
            session.get(GlobalHeavyOperationLeaseRow, 1),
        )
        for lease in leases:
            if (
                lease is None
                or lease.operation_id != row.id
                or lease.lease_token_digest != row.lease_digest
            ):
                raise RecoveryRefused("recovery_exact_lease_lost")
        assert row.lease_digest is not None
        if not ManagedInstanceMutationLeaseRepository(session).release(
            UUID(row.target_id),
            operation_id=UUID(row.id),
            token_digest=row.lease_digest,
        ) or not GlobalHeavyOperationLeaseRepository(session).release(
            operation_id=UUID(row.id), token_digest=row.lease_digest
        ):
            raise RecoveryRefused("recovery_exact_lease_lost")

    def status(self, recovery_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(SnapshotRecoveryRow, recovery_id)
            if row is None:
                raise RecoveryRefused("recovery_not_found")
            result = self.status_row(row, verified_resolution(session, row.incident_id))
            result["dispatches"] = [
                {
                    "step": d.step,
                    "attempt_id": d.id,
                    "wire_request_id": d.request_id,
                    "state": d.state,
                    "deadline": d.deadline,
                    "result": None
                    if d.result_json is None
                    else json.loads(d.result_json),
                }
                for d in session.scalars(
                    select(SnapshotRecoveryDispatchRow).where(
                        SnapshotRecoveryDispatchRow.recovery_id == row.id
                    )
                )
            ]
            from kodi_lab.storage.models import SnapshotRecoveryEventRow

            result["events"] = [
                {"event": e.event, "recorded_at": e.recorded_at}
                for e in session.scalars(
                    select(SnapshotRecoveryEventRow)
                    .where(SnapshotRecoveryEventRow.recovery_id == row.id)
                    .order_by(
                        SnapshotRecoveryEventRow.recorded_at,
                        SnapshotRecoveryEventRow.id,
                    )
                    .limit(32)
                )
            ]
            return result

    @staticmethod
    def status_row(
        row: SnapshotRecoveryRow, resolution_verified: bool = False
    ) -> dict[str, Any]:
        proof = json.loads(row.proof_json)
        return {
            "recovery_id": row.id,
            "confirmation_request_id": str(
                uuid5(UUID(row.id), "snapshot-recovery-confirmation")
            ),
            "status_url": f"/api/snapshot-recovery/{row.id}",
            "incident_id": row.incident_id,
            "target_id": row.target_id,
            "snapshot_id": proof["receipt"]["snapshot_id"],
            "state": row.state,
            "fingerprint": row.fingerprint,
            "expires_at": row.expires_at,
            "old_generation": proof["selection"]["generation"],
            "successor_generation": row.successor_generation,
            "container_id": row.container_id,
            "failure_code": row.failure_code,
            "artifact": proof["artifact_proof"],
            "final_state": "stopped"
            if row.state == "succeeded" and resolution_verified
            else None,
            "resolution_verified": resolution_verified,
            "intended_final_state": "stopped",
            "reviewed_bindings": {
                "old_container_id": proof["receipt"]["old_container_id"],
                "receipt_digest": digest(proof["receipt"]),
                "runtime_revision_id": proof["bindings"]["instance"][
                    "runtime_revision_id"
                ],
                "image_id": proof["bindings"]["artifact"]["image_id"],
                "fixture_id": proof["bindings"]["instance"]["fixture_definition_id"],
                "source_id": proof["selection"]["source_id"],
                "workspace_id": proof["selection"]["workspace_id"],
                "workspace_generation": proof["bindings"]["workspace"]["generation"],
                "workspace_observed_version": proof["bindings"]["workspace"][
                    "source_observed_version"
                ],
                "workspace_tree_digest": proof["bindings"]["workspace"][
                    "current_tree_digest"
                ],
                "snapshot_label": proof["bindings"]["snapshot"]["label"],
                "policy": proof["bindings"]["policy"],
                "resources": proof["bindings"]["resources"],
                "epoch": proof["epoch"],
                "seal_digest": proof["seal_digest"],
                "evidence_digest": proof["evidence_digest"],
            },
            "profile_claim": "exact reviewed snapshot; native acceptance not asserted",
        }

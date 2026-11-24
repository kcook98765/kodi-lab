"""Transaction-local identity and exact dual-lease checks for snapshot recovery."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_runtime_admission import require_managed_runtime
from kodi_lab.application.snapshot_recovery_evidence import (
    RecoveryRefused,
    canonical,
    digest,
)
from kodi_lab.application.snapshot_restore_sessions import RestoreSessionBinding
from kodi_lab.domain import GlobalHeavyOperationKind, ManagedInstanceMutationKind
from kodi_lab.storage.managed_version_slots import require_unique_version_profile
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedRemoteGuardRow,
    ManagedResourcePolicyRow,
    ManagedStartReservationRow,
    NovncSessionRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    SnapshotRecoveryDispatchRow,
    SnapshotRecoveryEpochRow,
    SnapshotRecoveryEventRow,
    SnapshotRecoveryResolutionRow,
    SnapshotRecoveryRow,
    SnapshotRestoreOperationRow,
    SnapshotRow,
    TargetRow,
    WebSessionRow,
)
from kodi_lab.storage.retained_development import RetainedContainer, retained_inventory

LEASE_DURATION = timedelta(minutes=10)

# Audited closed worker vocabularies, not a name/"active state" heuristic.
# Unknown future states refuse by default. Trace: docs/LEGACY-SNAPSHOT-RECOVERY.md.
_OP_TERMINAL = frozenset({"succeeded", "failed", "reconciliation_required"})
QUIESCENCE_TERMINALS: dict[str, frozenset[str]] = {
    name: _OP_TERMINAL
    for name in (
        "reset_operations",
        "snapshot_create_operations",
        "snapshot_restore_operations",
        "snapshot_delete_operations",
        "managed_instance_delete_operations",
        "managed_instance_lifecycle_operations",
        "managed_runtime_reconstruction_operations",
        "development_source_deployment_operations",
        "development_source_enablement_operations",
        "development_source_smoke_test_operations",
        "addon_zip_import_operations",
        "addon_zip_installation_operations",
        "addon_zip_enablement_operations",
        "addon_zip_smoke_test_operations",
        "addon_repository_refresh_operations",
        "addon_repository_fetch_operations",
        "repository_replacement_operations",
        "agent_workspace_creation_operations",
        "agent_workspace_cleanup_operations",
        "agent_workspace_promotion_operations",
        "agent_workspace_capture_operations",
        "workspace_import_operations",
    )
}
QUIESCENCE_TERMINALS.update(
    {
        "managed_runtime_reconstruction_operations": _OP_TERMINAL | {"resolved"},
        "promotion_verification_operations": _OP_TERMINAL | {"incomplete"},
        "mcp_workspace_deployment_operations": _OP_TERMINAL | {"ambiguous"},
        "mcp_workspace_execution_operations": _OP_TERMINAL | {"ambiguous"},
        "mcp_workspace_launches": frozenset(
            {"accepted", "failed", "ambiguous", "contained"}
        ),
        "mcp_addon_settings_operations": frozenset(
            {"completed", "failed", "ambiguous", "contained"}
        ),
        "workspace_execution_recovery_operations": frozenset({"completed"}),
        "runtime_recipe_installations": frozenset({"ready", "failed"}),
        "agent_workspaces": frozenset(
            {"ready", "promoted", "abandoned", "reconciliation_required"}
        ),
        "agent_projects": frozenset({"seeded"}),
        "agent_packages": frozenset({"accepted", "failed"}),
        "repository_workspace_deployments": frozenset({"succeeded", "failed"}),
        "repository_replacement_plans": frozenset(
            {"expired", "invalidated", "consumed", "cancelled"}
        ),
        "agent_workspace_promotion_plans": frozenset(
            {"reviewable", "conflicted", "invalidated", "expired", "consumed"}
        ),
        "managed_agent_lifecycle_requests": frozenset(
            {"ready", "not_ready", "released", "retained", "failed", "ambiguous"}
        ),
        "native_zip_installations": frozenset({"completed", "failed", "unverified"}),
        "managed_diagnostic_captures": frozenset({"retained", "failed", "discarded"}),
        "mcp_remote_action_receipts": frozenset({"completed", "ambiguous"}),
        "production_transient_dispatch_audits": frozenset({"completed", "ambiguous"}),
        "mcp_production_navigation_audits": frozenset({"completed", "ambiguous"}),
        "external_bridge_deployment_receipts": frozenset(
            {"succeeded", "failed", "ambiguous"}
        ),
        "external_bridge_request_receipts": frozenset(
            {"completed", "ambiguous", "rejected"}
        ),
        "mcp_production_screenshot_receipts": frozenset(
            {"completed", "ambiguous", "rejected"}
        ),
    }
)
QUIESCENCE_CHECKPOINTS: dict[str, tuple[str, frozenset[str]]] = {
    "create_kodi_operations": (
        "checkpoint",
        frozenset({"complete", "needs_attention"}),
    ),
    "managed_bridge_preparations": (
        "checkpoint",
        frozenset({"complete", "needs_attention"}),
    ),
    "managed_clean_resets": ("phase", frozenset({"complete", "blocked", "uncertain"})),
    "managed_version_operations": (
        "phase",
        frozenset({"complete", "blocked", "uncertain"}),
    ),
}


def stamp(now: datetime) -> str:
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def values(row: Any) -> dict[str, Any]:
    if row is None:
        raise RecoveryRefused("recovery_binding_missing")
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def inventory(records: Sequence[RetainedContainer]) -> list[dict[str, str | None]]:
    return sorted(
        (
            {
                "target": r.managed_instance_id,
                "container": r.container_id,
                "revision": r.runtime_revision_id,
                "image": r.image_id,
                "state": str(r.runtime_state),
            }
            for r in records
        ),
        key=lambda r: str(r["container"]),
    )


def epoch(session: Session, epoch_id: str) -> SnapshotRecoveryEpochRow:
    row = session.get(SnapshotRecoveryEpochRow, epoch_id)
    if row is None or row.active != 1 or row.state != "serving":
        raise RecoveryRefused("recovery_epoch_unavailable")
    if not row.web_owner or not row.broker_owner:
        raise RecoveryRefused("recovery_epoch_owners_unproven")
    return row


def active_epoch(session: Session) -> SnapshotRecoveryEpochRow | None:
    return session.scalar(
        select(SnapshotRecoveryEpochRow).where(SnapshotRecoveryEpochRow.active == 1)
    )


def verified_resolution(session: Session, incident_id: str) -> bool:
    """Validate immutable historical closure; current native bytes may change later."""
    if not inspect(session.get_bind()).has_table("snapshot_recovery_resolutions"):
        return False  # Historical read-only observers retain the original fence.
    relation = session.get(SnapshotRecoveryResolutionRow, incident_id)
    if relation is None:
        return False
    row = session.get(SnapshotRecoveryRow, relation.recovery_id)
    original = session.get(SnapshotRestoreOperationRow, incident_id)
    if row is None or original is None:
        return False
    try:
        proof = json.loads(row.proof_json)
        original_values = values(original)
        boundary = session.get(SnapshotRecoveryEpochRow, row.epoch_id)
        if (
            row.state != "succeeded"
            or not row.consumed_at
            or not row.request_id
            or not row.lease_digest
            or not row.container_id
            or not row.successor_generation
            or row.incident_id != incident_id
            or row.target_id != original.managed_instance_id
            or relation.relation != "verified_original_snapshot_stopped_successor"
            or digest(original_values) != relation.receipt_digest
            or proof["receipt"] != original_values
            or digest(proof) != row.fingerprint
            or boundary is None
            or boundary.incident_id != incident_id
            or boundary.receipt_digest != relation.receipt_digest
            or digest(json.loads(boundary.seal_json)) != proof["seal_digest"]
            or digest(json.loads(boundary.evidence_json)) != proof["evidence_digest"]
            or json.loads(boundary.evidence_json)["receipt"] != original_values
            or original.state != "reconciliation_required"
            or original.phase != "provisioning"
            or original.fresh_container_id is not None
            or row.container_id == original.old_container_id
        ):
            return False
        bindings, selected = proof["bindings"], proof["selection"]
        if (
            bindings["snapshot"]["id"] != original.snapshot_id
            or bindings["snapshot"]["source_managed_instance_id"] != row.target_id
            or bindings["snapshot"]["runtime_revision_id"]
            != selected["runtime_revision_id"]
            or bindings["instance"]["runtime_revision_id"]
            != selected["runtime_revision_id"]
            or bindings["snapshot"]["captured_fixture_definition_id"]
            != bindings["instance"]["fixture_definition_id"]
            or row.successor_generation != proof["latest_generation"] + 1
        ):
            return False
        old = session.get(DevelopmentSessionRow, selected["generation"])
        successor = session.get(DevelopmentSessionRow, row.successor_generation)
        if (
            old is None
            or successor is None
            or old.phase != "closed"
            or old.updated_at != row.consumed_at
            or any(
                getattr(old, key) != value
                for key, value in selected.items()
                if key not in {"phase", "updated_at"}
            )
            or successor.request_id
            != str(uuid5(UUID(row.id), "snapshot-restore-successor"))
            or successor.container_id != row.container_id
            or successor.created_at != row.consumed_at
            or successor.phase not in {"bound", "closed", "uncertain"}
            or any(
                getattr(successor, key) != selected[key]
                for key in (
                    "target_id",
                    "runtime_revision_id",
                    "workspace_id",
                    "source_id",
                )
            )
        ):
            return False
        dispatches = {
            d.step: d
            for d in session.scalars(
                select(SnapshotRecoveryDispatchRow).where(
                    SnapshotRecoveryDispatchRow.recovery_id == row.id
                )
            )
        }
        if set(dispatches) != {"provision", "apply", "verify"}:
            return False
        events = list(
            session.scalars(
                select(SnapshotRecoveryEventRow)
                .where(SnapshotRecoveryEventRow.recovery_id == row.id)
                .limit(64)
            )
        )
        if len(events) == 64:
            return False
        claims = [e for e in events if e.event == "claimed_successor_reserved"]
        admitted = [e for e in events if e.event == "provision_admitted"]
        released = [e for e in events if e.event == "leases_released"]
        if (
            len(claims) != 1
            or len(admitted) != 1
            or len(released) != 1
            or claims[0].recorded_at != row.consumed_at
            or json.loads(claims[0].detail_json)
            != {
                "generation": row.successor_generation,
                "actor": row.actor_digest,
                "fingerprint": row.fingerprint,
                "request": row.request_id,
            }
            or json.loads(admitted[0].detail_json)
            != {
                "generation": row.successor_generation,
                "target": row.target_id,
                "revision": selected["runtime_revision_id"],
                "source": selected["source_id"],
                "workspace": selected["workspace_id"],
                "lease_digest": row.lease_digest,
                "attempt": dispatches["provision"].id,
                "wire_request": dispatches["provision"].request_id,
            }
        ):
            return False
        outcomes = []
        for step in ("provision", "apply", "verify"):
            dispatch = dispatches[step]
            if (
                dispatch.state != "completed"
                or not dispatch.result_json
                or UUID(dispatch.id).version != 4
                or UUID(dispatch.request_id).version != 4
                or dispatch.intent_digest
                != digest(
                    {
                        "fingerprint": row.fingerprint,
                        "step": step,
                        "container": None if step == "provision" else row.container_id,
                    }
                )
            ):
                return False
            result = json.loads(dispatch.result_json)
            if result["container"] != row.container_id or result["state"] != "stopped":
                return False
            if step == "provision":
                if (
                    result["created"] is not True
                    or result["image"] != bindings["artifact"]["image_id"]
                    or result["revision"] != selected["runtime_revision_id"]
                ):
                    return False
            else:
                artifact = proof["artifact_proof"]
                if (
                    any(
                        result[key] != artifact[key]
                        for key in (
                            "archive",
                            "manifest",
                            "length",
                            "manifest_length",
                            "count",
                            "inventory_digest",
                        )
                    )
                    or result["databases"] != artifact["recognized_databases"]
                    or result["ownership"] != [1000, 1000]
                    or result["safe_modes"] is not True
                ):
                    return False
            prepared = [e for e in events if e.event == step + "_prepared"]
            consumed = [e for e in events if e.event == step + "_consumed"]
            completed = [e for e in events if e.event == step + "_completed"]
            if len(prepared) != 1 or len(consumed) != 1 or len(completed) != 1:
                return False
            if (
                json.loads(prepared[0].detail_json)
                != {"attempt": dispatch.id, "request": dispatch.request_id}
                or json.loads(consumed[0].detail_json)
                != {"attempt": dispatch.id, "wire_request": dispatch.request_id}
                or json.loads(completed[0].detail_json) != result
                or not prepared[0].recorded_at
                <= consumed[0].recorded_at
                <= completed[0].recorded_at
                or consumed[0].recorded_at >= dispatch.deadline
            ):
                return False
            if (
                step == "provision"
                and not consumed[0].recorded_at
                <= admitted[0].recorded_at
                <= completed[0].recorded_at
            ):
                return False
            outcomes.append(result)
        successes = [e for e in events if e.event == "succeeded_stopped"]
        return (
            digest(outcomes) == relation.proof_digest
            and len(successes) == 1
            and successes[0].recorded_at
            == relation.completed_at
            == released[0].recorded_at
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def event(
    session: Session, recovery_id: str, name: str, now: datetime, detail: object = None
) -> None:
    session.add(
        SnapshotRecoveryEventRow(
            id=str(uuid4()),
            recovery_id=recovery_id,
            event=name,
            recorded_at=stamp(now),
            detail_json=canonical(detail),
        )
    )


def require_idle(
    session: Session, target: str, incident: str, own: str | None = None
) -> None:
    """Refuse queued work; never sweep leases or reinterpret expiry as a drain."""
    for model in (
        ManagedInstanceMutationLeaseRow,
        GlobalHeavyOperationLeaseRow,
        ManagedProvisionReservationRow,
        ManagedStartReservationRow,
    ):
        for row in session.scalars(select(model)):
            reservation = cast(
                ManagedInstanceMutationLeaseRow
                | GlobalHeavyOperationLeaseRow
                | ManagedProvisionReservationRow
                | ManagedStartReservationRow,
                row,
            )
            if own is None or reservation.operation_id != own:
                raise RecoveryRefused("recovery_conflicting_reservation")
    for guard_model in (ManagedRemoteGuardRow, NovncSessionRow):
        if session.scalar(select(guard_model).limit(1)) is not None:
            raise RecoveryRefused("recovery_conflicting_view_or_input_claim")
    for name, terminal in QUIESCENCE_TERMINALS.items():
        table = Base.metadata.tables[name]
        if session.execute(
            select(table.c.state).where(table.c.state.not_in(terminal)).limit(1)
        ).first():
            raise RecoveryRefused("recovery_queued_or_live_work")
    for name, (column, terminal) in QUIESCENCE_CHECKPOINTS.items():
        table = Base.metadata.tables[name]
        if session.execute(
            select(table.c[column]).where(table.c[column].not_in(terminal)).limit(1)
        ).first():
            raise RecoveryRefused("recovery_queued_or_live_work")
    reviews = Base.metadata.tables["development_session_replacement_reviews"]
    if session.execute(
        select(reviews.c.id)
        .where(reviews.c.state == "open", reviews.c.successor_generation.is_(None))
        .limit(1)
    ).first():
        raise RecoveryRefused("recovery_open_replacement_review")
    if (
        session.scalar(
            select(SnapshotRecoveryRow.id)
            .where(
                SnapshotRecoveryRow.consumed_at.is_not(None),
                SnapshotRecoveryRow.state != "succeeded",
                SnapshotRecoveryRow.id != (own or ""),
            )
            .limit(1)
        )
        is not None
    ):
        raise RecoveryRefused("recovery_competing_claim")
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService

    # Share the writer snapshot; only the exact immutable original blocker is
    # accepted. A second unresolved incident is never excluded by caller input.
    status = ManagedInstanceReconciliationService(
        sessionmaker(bind=session.get_bind())
    ).status(UUID(target), read_session=session)
    if (
        not status.blocking
        or str(status.operation_uuid) != incident
        or status.operation_kind != "snapshot_restore"
    ):
        raise RecoveryRefused("recovery_competing_incident")
    if (
        session.scalar(
            select(ManagedInstanceUseRow)
            .where(
                ManagedInstanceUseRow.hold_until.is_not(None),
                ManagedInstanceUseRow.released_at.is_(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise RecoveryRefused("recovery_runtime_hold")


def require_actor(session: Session, actor_digest: str, now: datetime) -> None:
    actor = session.get(WebSessionRow, actor_digest)
    if (
        actor is None
        or actor.admin_key != "local-admin"
        or actor.absolute_expires_at <= stamp(now)
        or actor.last_used_at <= stamp(now - timedelta(hours=12))
    ):
        raise RecoveryRefused("recovery_administrator_revoked")


def require_retained_attribution(session: Session, evidence: dict[str, Any]) -> None:
    from kodi_lab.application.snapshot_recovery_evidence import ReviewedAttribution

    anchor = ReviewedAttribution.model_validate(evidence["attribution"])
    if (
        digest(values(session.get(DevelopmentSourceRow, anchor.source_id)))
        != anchor.source_digest
        or digest(values(session.get(AgentWorkspaceRow, anchor.workspace_id)))
        != anchor.workspace_digest
        or digest(values(session.get(DevelopmentSessionPolicyRow, 1)))
        != anchor.policy_digest
    ):
        raise RecoveryRefused("recovery_retained_attribution_changed")


def require_claim_admission(session: Session, recovery_id: str, target: str) -> str:
    """Only the exact newly consumed, pre-handoff claim may acquire its leases."""
    row = session.get(SnapshotRecoveryRow, recovery_id)
    if (
        row is None
        or row.target_id != target
        or row.state != "claimed"
        or not row.consumed_at
        or not row.request_id
        or not row.lease_digest
        or row.successor_generation is not None
        or row.container_id is not None
        or row.expires_at <= stamp(datetime.now(UTC))
    ):
        raise RecoveryRefused("recovery_claim_admission_unproven")
    boundary = epoch(session, row.epoch_id)
    proof = json.loads(row.proof_json)
    require_actor(session, row.actor_digest, datetime.now(UTC))
    if (
        row.fingerprint != digest(proof)
        or proof["web_owner"] != boundary.web_owner
        or proof["broker_owner"] != boundary.broker_owner
        or proof["receipt"]
        != values(session.get(SnapshotRestoreOperationRow, row.incident_id))
        or boundary.receipt_digest != digest(proof["receipt"])
        or boundary.incident_id != row.incident_id
    ):
        raise RecoveryRefused("recovery_claim_admission_unproven")
    return row.incident_id


def reviewed_state(
    session: Session,
    epoch_row: SnapshotRecoveryEpochRow,
    records: Sequence[RetainedContainer],
    data_root: Path,
) -> dict[str, Any]:
    evidence = json.loads(epoch_row.evidence_json)
    require_retained_attribution(session, evidence)
    old = evidence["receipt"]
    receipt = values(session.get(SnapshotRestoreOperationRow, old["id"]))
    if receipt != old or digest(receipt) != epoch_row.receipt_digest:
        raise RecoveryRefused("recovery_original_receipt_changed")
    target = old["managed_instance_id"]
    instance = session.get(ManagedInstanceRow, target)
    snapshot = session.get(SnapshotRow, old["snapshot_id"])
    if (
        instance is None
        or instance.instance_type != "disposable"
        or snapshot is None
        or snapshot.source_managed_instance_id != target
        or snapshot.source_binding != "attached"
        or snapshot.runtime_revision_id != instance.runtime_revision_id
        or snapshot.captured_fixture_definition_id != instance.fixture_definition_id
        or snapshot.state != "available"
        or snapshot.validation_result != "passed"
    ):
        raise RecoveryRefused("recovery_snapshot_binding_invalid")
    reviewed_snapshot = evidence["reviewed_snapshot"]
    if any(
        getattr(snapshot, key) != reviewed_snapshot[key]
        for key in ("archive_digest", "manifest_digest", "archive_size", "member_count")
    ):
        raise RecoveryRefused("recovery_original_snapshot_changed")
    artifact_row = session.get(
        RuntimeContainerArtifactRow, instance.runtime_revision_id
    )
    if (
        artifact_row is None
        or artifact_row.image_id != evidence["native_identity"]["image_id"]
    ):
        raise RecoveryRefused("recovery_original_image_changed")
    selected = evidence["selection"]
    binding = RestoreSessionBinding.read(session)
    current = session.get(DevelopmentSessionRow, selected["generation"])
    if (
        values(current) != selected
        or binding.selection is None
        or binding.selection.generation != selected["generation"]
        or selected["container_id"] != old["old_container_id"]
        or selected["runtime_revision_id"] != instance.runtime_revision_id
    ):
        raise RecoveryRefused("recovery_selection_changed")
    require_managed_runtime(
        session, UUID(target), UUID(instance.runtime_revision_id), data_root=data_root
    )
    retained_inventory(session, records)
    require_unique_version_profile(
        session, records, target_id=target, revision_id=instance.runtime_revision_id
    )
    if any(
        r.managed_instance_id == target or r.runtime_state != "stopped" for r in records
    ):
        raise RecoveryRefused("recovery_inventory_not_quiescent")
    require_idle(session, target, old["id"])
    revision = session.get(RuntimeRevisionRow, instance.runtime_revision_id)
    if revision is None:
        raise RecoveryRefused("recovery_runtime_missing")
    runtime_profile = session.get(
        RuntimeExecutionProfileRow, instance.runtime_revision_id
    )
    if runtime_profile is None or (
        runtime_profile.runtime_uid,
        runtime_profile.runtime_gid,
    ) != (1000, 1000):
        raise RecoveryRefused("recovery_runtime_owner_incompatible")
    return {
        "receipt": receipt,
        "selection": selected,
        "latest_generation": binding.latest,
        "bindings": {
            "target": values(session.get(TargetRow, target)),
            "instance": values(instance),
            "snapshot": values(snapshot),
            "revision": values(revision),
            "definition": values(
                session.get(RuntimeDefinitionRow, revision.runtime_definition_id)
            ),
            "profile": values(
                session.get(RuntimeExecutionProfileRow, instance.runtime_revision_id)
            ),
            "artifact": values(
                session.get(RuntimeContainerArtifactRow, instance.runtime_revision_id)
            ),
            "source": values(session.get(DevelopmentSourceRow, selected["source_id"])),
            "workspace": values(
                session.get(AgentWorkspaceRow, selected["workspace_id"])
            ),
            "policy": values(session.get(DevelopmentSessionPolicyRow, 1)),
            "resources": values(session.get(ManagedResourcePolicyRow, 1)),
        },
        "inventory": inventory(records),
        "epoch": epoch_row.id,
        "seal_digest": digest(json.loads(epoch_row.seal_json)),
        "web_owner": epoch_row.web_owner,
        "broker_owner": epoch_row.broker_owner,
        "evidence_digest": digest(evidence),
        "reviewed_manifest_size": reviewed_snapshot["manifest_size"],
        "final_state": "stopped",
    }


def checked(
    session: Session, row: SnapshotRecoveryRow, now: datetime, *, renew: bool = True
) -> dict[str, Any]:
    boundary = epoch(session, row.epoch_id)
    require_actor(session, row.actor_digest, now)
    proof: dict[str, Any] = json.loads(row.proof_json)
    if (
        boundary.web_owner != proof["web_owner"]
        or boundary.broker_owner != proof["broker_owner"]
        or digest(json.loads(boundary.seal_json)) != proof["seal_digest"]
        or row.state not in {"claimed", "provisioned", "applied", "verified"}
    ):
        raise RecoveryRefused("recovery_execution_owner_changed")
    instance = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
    heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
    for lease, kind in (
        (instance, ManagedInstanceMutationKind.SNAPSHOT_RECOVERY),
        (heavy, GlobalHeavyOperationKind.SNAPSHOT_RECOVERY),
    ):
        if (
            lease is None
            or lease.operation_kind != kind
            or lease.operation_id != row.id
            or not row.lease_digest
            or lease.lease_token_digest != row.lease_digest
            or lease.lease_expires_at is None
            or lease.lease_expires_at <= stamp(now)
        ):
            raise RecoveryRefused("recovery_exact_lease_lost")
        if renew:
            lease.heartbeat_at, lease.lease_expires_at = (
                stamp(now),
                stamp(now + LEASE_DURATION),
            )
    original = values(session.get(SnapshotRestoreOperationRow, row.incident_id))
    if original != proof["receipt"]:
        raise RecoveryRefused("recovery_original_receipt_changed")
    return proof


def check_bindings(
    session: Session,
    row: SnapshotRecoveryRow,
    records: Sequence[RetainedContainer],
    data_root: Path,
) -> dict[str, Any]:
    proof: dict[str, Any] = json.loads(row.proof_json)
    bindings = proof["bindings"]
    target, revision = row.target_id, bindings["instance"]["runtime_revision_id"]
    require_idle(session, target, row.incident_id, own=row.id)
    if session.scalar(select(ManagedStartReservationRow).limit(1)) is not None:
        raise RecoveryRefused("recovery_unexpected_start_reservation")
    require_managed_runtime(session, UUID(target), UUID(revision), data_root=data_root)
    for key, model, pk in (
        ("target", TargetRow, target),
        ("instance", ManagedInstanceRow, target),
        ("snapshot", SnapshotRow, proof["receipt"]["snapshot_id"]),
        ("revision", RuntimeRevisionRow, revision),
        (
            "definition",
            RuntimeDefinitionRow,
            bindings["revision"]["runtime_definition_id"],
        ),
        ("profile", RuntimeExecutionProfileRow, revision),
        ("artifact", RuntimeContainerArtifactRow, revision),
        ("source", DevelopmentSourceRow, proof["selection"]["source_id"]),
        ("workspace", AgentWorkspaceRow, proof["selection"]["workspace_id"]),
        ("resources", ManagedResourcePolicyRow, 1),
    ):
        expected = dict(bindings[key])
        if key == "instance":
            expected["runtime_state_revision"] += 1
            expected["runtime_state"] = "stopped"
        elif key == "target":
            expected.update(
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        if values(session.get(model, pk)) != expected:
            raise RecoveryRefused("recovery_reviewed_binding_changed")
    policy = values(session.get(DevelopmentSessionPolicyRow, 1))
    expected_policy = {
        **bindings["policy"],
        "active_generation": row.successor_generation,
    }
    if policy != expected_policy:
        raise RecoveryRefused("recovery_policy_changed")
    old = session.get(DevelopmentSessionRow, proof["selection"]["generation"])
    if (
        old is None
        or old.phase != "closed"
        or any(
            getattr(old, k) != v
            for k, v in proof["selection"].items()
            if k not in {"phase", "updated_at"}
        )
    ):
        raise RecoveryRefused("recovery_original_session_changed")
    current = session.get(DevelopmentSessionRow, row.successor_generation)
    if (
        current is None
        or current.phase not in {"reserved", "provisioning", "bound"}
        or current.container_id != row.container_id
        or any(
            getattr(current, k) != proof["selection"][k]
            for k in ("target_id", "runtime_revision_id", "source_id", "workspace_id")
        )
    ):
        raise RecoveryRefused("recovery_successor_session_changed")
    retained_inventory(session, records)
    reservations = list(session.scalars(select(ManagedProvisionReservationRow)))
    if any(r.operation_id != row.id for r in reservations):
        raise RecoveryRefused("recovery_conflicting_reservation")
    require_unique_version_profile(
        session,
        records,
        target_id=target,
        revision_id=revision,
        own_provision_operation_id=row.id,
    )
    others = [r for r in records if r.managed_instance_id != target]
    if inventory(others) != proof["inventory"]:
        raise RecoveryRefused("recovery_unrelated_inventory_changed")
    selected = [r for r in records if r.managed_instance_id == target]
    if row.container_id is None:
        if selected:
            raise RecoveryRefused("recovery_unknown_successor")
    elif (
        len(selected) != 1
        or selected[0].container_id != row.container_id
        or selected[0].runtime_revision_id != revision
        or selected[0].image_id != bindings["artifact"]["image_id"]
        or selected[0].runtime_state != "stopped"
    ):
        raise RecoveryRefused("recovery_successor_identity_unproven")
    return proof

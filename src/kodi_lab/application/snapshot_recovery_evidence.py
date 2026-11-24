"""Bounded legacy primary-capture review, not fabricated historical admission."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RecoveryRefused(RuntimeError):
    """Safe closed reason; never include raw evidence/settings in errors."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(value: str) -> str:
    if str(UUID(value)) != value:
        raise RecoveryRefused("recovery_identity_invalid")
    return value


def broker_event(line: str) -> dict[str, Any]:
    if not line.startswith("{"):
        prefix, separator, payload = line.partition(" ")
        if not separator or re.fullmatch(r"\d{4}-\d\d-\d\dT[0-9:.]+Z", prefix) is None:
            raise RecoveryRefused("legacy_broker_capture_invalid")
        line = payload
    result = json.loads(line)
    if not isinstance(result, dict):
        raise RecoveryRefused("legacy_broker_capture_invalid")
    return result


class ReviewedSnapshotIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot_id: str
    archive_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    archive_size: int = Field(gt=0)
    manifest_size: int = Field(gt=0)
    member_count: int = Field(gt=0)


class ReviewedAttribution(BaseModel):
    """New reviewed anchor from retained rows, never historical admission."""

    model_config = ConfigDict(extra="forbid")
    method: Literal[
        "retained_complete_rows_v1", "verified_historical_table_commitment_v1"
    ]
    source_id: str
    workspace_id: str
    source_digest: str
    workspace_digest: str
    policy_digest: str
    workspace_generation: int
    baseline_artifact_id: str
    baseline_manifest_digest: str
    current_tree_digest: str
    provenance_digest: str
    historical_commitment_digest: str | None = None


def reviewed_attribution(
    payload: str, method: str, selected: dict[str, Any]
) -> ReviewedAttribution:
    from kodi_lab.storage.models import AgentWorkspaceRow, DevelopmentSourceRow

    proof = json.loads(payload)
    if "checks" in proof and method != "verified_historical_table_commitment_v1":
        raise RecoveryRefused("legacy_attribution_provenance_mismatch")
    source, workspace = proof["selected_source"], proof["selected_workspace"]
    policy = proof["policy"]
    if (
        set(source) != set(DevelopmentSourceRow.__table__.columns.keys())
        or set(workspace) != set(AgentWorkspaceRow.__table__.columns.keys())
        or len(policy) != 1
        or set(policy[0])
        != {"id", "active_generation", "retain_profiles", "retained_omega_target_id"}
        or policy[0]["id"] != 1
        or policy[0]["active_generation"] != selected["generation"]
        or policy[0]["retain_profiles"] not in (0, 1, False, True)
        or source["id"] != selected["source_id"]
        or workspace["id"] != selected["workspace_id"]
        or workspace["development_source_id"] != source["id"]
        or workspace["source_addon_id"] != source["addon_id"]
        or workspace["state"] != "ready"
        or workspace["bytes_state"] != "present"
        or not workspace["baseline_manifest_digest"]
        or not workspace["current_tree_digest"]
    ):
        raise RecoveryRefused("legacy_attribution_unproven")
    commitment = None
    if method == "verified_historical_table_commitment_v1":
        commitments = {}
        for table, chosen in (
            ("development_sources", source),
            ("agent_workspaces", workspace),
        ):
            check = proof["checks"][table]
            rows = check["observed_rows"]
            historical = check["historical"]
            observed = hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str, allow_nan=False).encode()
            ).hexdigest()
            if (
                len(rows) != historical["count"]
                or observed != historical["digest"]
                or rows != sorted(rows, key=lambda row: row["id"])
                or len({row["id"] for row in rows}) != len(rows)
                or [row for row in rows if row["id"] == chosen["id"]] != [chosen]
            ):
                raise RecoveryRefused("legacy_attribution_commitment_mismatch")
            commitments[table] = historical
        for key in ("historical_capture_sha256", "current_capture_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", proof[key]) is None:
                raise RecoveryRefused("legacy_attribution_commitment_unproven")
        commitment = digest(
            {
                "tables": commitments,
                "historical_capture": proof["historical_capture_sha256"],
            }
        )
    normalized_policy = {
        **policy[0],
        "retain_profiles": bool(policy[0]["retain_profiles"]),
    }
    return ReviewedAttribution.model_validate(
        {
            "method": method,
            "source_id": source["id"],
            "workspace_id": workspace["id"],
            "source_digest": digest(source),
            "workspace_digest": digest(workspace),
            "policy_digest": digest(normalized_policy),
            "workspace_generation": workspace["generation"],
            "baseline_artifact_id": workspace["baseline_artifact_id"],
            "baseline_manifest_digest": workspace["baseline_manifest_digest"],
            "current_tree_digest": workspace["current_tree_digest"],
            "provenance_digest": hashlib.sha256(payload.encode()).hexdigest(),
            "historical_commitment_digest": commitment,
        }
    )


class LegacyCaptureReview(BaseModel):
    """Offline-only inputs from the independent reviewer; no Web import route.

    The reviewed link establishes primary-capture provenance and complete coverage.
    It is explicitly a *new* review assertion, not an old dispatch database row.
    """

    model_config = ConfigDict(extra="forbid")
    historical_commit: str
    removal_request_id: str
    refusal_request_id: str
    reviewer: str = Field(min_length=1, max_length=120)
    assessment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    correlation_method: str = Field(pattern="^reviewed_complete_primary_chain_v1$")
    reviewed_snapshot: ReviewedSnapshotIdentity
    attribution_method: Literal[
        "retained_complete_rows_v1", "verified_historical_table_commitment_v1"
    ] = "retained_complete_rows_v1"
    native_stop_kind: Literal["managed_version_stop_v1", "web_lifecycle_stop_v1"] = (
        "managed_version_stop_v1"
    )
    captures: dict[str, str]

    def normalized(self) -> dict[str, Any]:
        if self.historical_commit != "30316b88d20cc5fb4d8ed5a96e2b971cf2978afb":
            raise RecoveryRefused("legacy_code_semantics_not_reviewed")
        required = {
            "broker",
            "web",
            "receipt",
            "selection",
            "intent",
            "accepted",
            "terminal",
            "before",
            "native_stop",
            "attribution",
        }
        if self.native_stop_kind == "web_lifecycle_stop_v1":
            required.add("native_identity")
        if set(self.captures) != required or len(canonical(self.captures)) > 200000:
            raise RecoveryRefused("legacy_capture_coverage_invalid")
        try:
            docs = {
                key: json.loads(value)
                for key, value in self.captures.items()
                if key not in {"broker", "web"}
            }
            events = [
                broker_event(line) for line in self.captures["broker"].splitlines()
            ]
            receipt, selection = docs["receipt"], docs["selection"]["current"]
            attribution = reviewed_attribution(
                self.captures["attribution"], self.attribution_method, selection
            )
            target, snapshot, incident = (
                receipt[k] for k in ("managed_instance_id", "snapshot_id", "id")
            )
            for value in (
                target,
                snapshot,
                incident,
                self.removal_request_id,
                self.refusal_request_id,
            ):
                identity(value)
            old = receipt["old_container_id"]
            if self.reviewed_snapshot.snapshot_id != snapshot:
                raise ValueError
            if not re.fullmatch("[0-9a-f]{64}", old):
                raise ValueError
            if (
                receipt["state"] != "reconciliation_required"
                or receipt["phase"] != "provisioning"
                or not receipt["reconciliation_required"]
                or receipt["prior_source_runtime_state"] != "stopped"
                or receipt["fresh_container_id"] is not None
                or receipt["lease_token_digest"] is not None
                or receipt["lease_expires_at"] is not None
                or not receipt["started_at"]
                or not receipt["completed_at"]
            ):
                raise ValueError
            if (
                selection["target_id"] != target
                or selection["container_id"] != old
                or selection["phase"] != "bound"
                or docs["selection"]["latest_generation"] != selection["generation"]
            ):
                raise ValueError
            accepted = docs["accepted"]["operation"]
            terminal = docs["terminal"]
            for doc in (accepted, terminal):
                if (
                    doc["operation_id"] != incident
                    or doc["snapshot_id"] != snapshot
                    or doc["managed_instance_id"] != target
                    or doc["requested_at"] != receipt["requested_at"]
                ):
                    raise ValueError
            if (
                accepted["state"] != "queued"
                or terminal["state"] != receipt["state"]
                or terminal["phase"] != receipt["phase"]
                or terminal["completed_at"] != receipt["completed_at"]
            ):
                raise ValueError
            intent = docs["intent"]
            if self.native_stop_kind == "managed_version_stop_v1":
                stop = docs["native_stop"]["stop_receipt"]
                native = stop["status"]
                stop_confirmed = stop["stop_confirmed"] is True
            else:
                stop, observed = docs["native_stop"], docs["native_identity"]
                native = {
                    "target_id": stop["managed_instance_id"],
                    "profile_identity": {
                        "container_id": observed["container_id"],
                        "image_id": observed["image_id"],
                        "runtime_revision_id": observed["labels"][
                            "io.kodi-lab.runtime-revision-id"
                        ],
                    },
                }
                stop_confirmed = (
                    stop["container_state"] == "stopped"
                    and stop["process_state"] == "not_running"
                    and observed["stopped"] is True
                    and observed["labels"]["io.kodi-lab.instance-id"] == target
                    and observed["snapshot_id"] == snapshot
                    and stop["runtime_revision_id"] == selection["runtime_revision_id"]
                )
            if (
                intent["target"] != target
                or intent["snapshot"] != snapshot
                or intent["prior_state"] != "stopped"
                or intent["confirmed"] is not True
                or not stop_confirmed
                or native["target_id"] != target
                or native["profile_identity"]["container_id"] != old
                or native["profile_identity"]["runtime_revision_id"]
                != selection["runtime_revision_id"]
                or docs["before"]["nonterminal_operations"]
                or any(docs["before"]["active_leases_or_reservations"].values())
            ):
                raise ValueError
            web = self.captures["web"].splitlines()
            posts = [line for line in web if '"POST ' in line]
            route = (
                f"POST /api/instances/{target}/snapshots/{snapshot}/restore HTTP/1.1"
            )
            login = '"POST /login HTTP/1.1" 303 See Other'
            restore = f'"{route}" 202 Accepted'
            stopped = f'"POST /api/instances/{target}/stop HTTP/1.1" 200 OK'
            if (
                sum(line.endswith(restore) for line in posts) != 1
                or sum(line.endswith(login) for line in posts) > 1
                or any(
                    not line.endswith(restore)
                    and not line.endswith(login)
                    and not (
                        self.native_stop_kind == "web_lifecycle_stop_v1"
                        and line.endswith(stopped)
                    )
                    for line in posts
                )
                or not any(
                    f"snapshot-restore-operations/{incident} " in line for line in web
                )
            ):
                raise ValueError
            ids = [
                e.get("broker_request_id") for e in events if e.get("broker_request_id")
            ]
            if len(ids) != len(set(ids)) or not 2 <= len(events) <= 256:
                raise ValueError
            readonly = {
                None,
                "managed_container_inventory",
                "capabilities",
                "active_reviewed_runtime_installation",
                "reviewed_runtime_image_present",
                "ping",
                "broker_info",
            }
            mutations = [e for e in events if e.get("broker_operation") not in readonly]
            if len(mutations) != 2:
                raise ValueError
            removed, refused = mutations
            for event in mutations:
                if (
                    event["event"] != "broker request completed"
                    or event["broker_managed_instance_id"] != target
                    or event["broker_protocol_version"] != 1
                    or event["broker_peer_uid"] != 10001
                    or event["broker_peer_gid"] != 10001
                ):
                    raise ValueError
            if (
                removed["broker_operation"] != "deprovision_managed_container"
                or removed["broker_request_id"] != self.removal_request_id
                or removed["broker_result_code"] != "success"
                or removed["broker_container_removed"] is not True
                or removed["broker_runtime_revision_id"]
                != selection["runtime_revision_id"]
                or refused["broker_operation"] != "provision_managed_container"
                or refused["broker_request_id"] != self.refusal_request_id
                or refused["broker_result_code"]
                != "development_session_identity_changed"
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RecoveryRefused("legacy_primary_chain_unproven") from error
        return {
            "version": 1,
            "historical_commit": self.historical_commit,
            "receipt": receipt,
            "selection": selection,
            "native_identity": native["profile_identity"],
            "attribution": attribution.model_dump(),
            "native_stop_kind": self.native_stop_kind,
            "reviewed_snapshot": self.reviewed_snapshot.model_dump(),
            "removal": removed,
            "refusal": refused,
            "primary_digests": {
                key: hashlib.sha256(value.encode()).hexdigest()
                for key, value in self.captures.items()
            },
            "reviewed_link": {
                "reviewer": self.reviewer,
                "assessment_digest": self.assessment_digest,
                "method": self.correlation_method,
            },
            "historical_admission": "not_recorded",
            "historical_dispatch": "not_recorded",
        }

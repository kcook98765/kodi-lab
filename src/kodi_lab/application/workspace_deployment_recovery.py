"""Web-only stopped-container recovery; never copy, start, or replay deployment."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    InspectMcpWorkspaceDeploymentResult,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import InstanceType, TargetType
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from kodi_lab.storage.workspace_deployments import prior_workspace_copy
from kodi_lab.workspace.storage import workspace_coordination_lock


class WorkspaceDeploymentRecoveryError(RuntimeError):
    pass


RecoveryOutcome = Literal[
    "contained_requested_tree", "contained_prior_tree", "still_requires_admin"
]


class WorkspaceDeploymentRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    deployment_id: UUID
    request_id: UUID
    workspace_id: UUID
    target_id: UUID
    generation: int = Field(ge=0)
    outcome: Literal[
        "contained_requested_tree", "contained_prior_tree", "still_requires_admin"
    ]
    reason: str = Field(max_length=64)
    next_action: str = Field(max_length=256)
    failed_phase: str | None = Field(max_length=32)
    failure_cause: str | None = Field(max_length=128)
    deployment_succeeded: Literal[False] = False
    replayed: bool = False


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...
    def inspect_mcp_workspace_deployment(
        self,
        *,
        deployment_operation_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> InspectMcpWorkspaceDeploymentResult: ...


class WorkspaceDeploymentRecoveryService:
    def __init__(
        self, sessions: sessionmaker[Session], broker: _Broker, *, data_root: Path
    ) -> None:
        self._sessions, self._broker, self._data_root = sessions, broker, data_root
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    @staticmethod
    def _result(
        row: McpWorkspaceDeploymentOperationRow,
        outcome: RecoveryOutcome,
        reason: str,
        *,
        replayed: bool = False,
    ) -> WorkspaceDeploymentRecoveryResult:
        return WorkspaceDeploymentRecoveryResult(
            deployment_id=UUID(row.id),
            request_id=UUID(row.request_id),
            workspace_id=UUID(row.workspace_id),
            target_id=UUID(row.target_id),
            generation=row.generation,
            outcome=outcome,
            reason=reason,
            next_action="Review status, start explicitly, then deploy with a NEW "
            "request ID; containment is not deployment success."
            if outcome.startswith("contained_")
            else "Stop the exact target through Web controls, then recheck. "
            "If evidence still fails, inspect retained workspace/baseline and "
            "broker diagnostics; do not reset or replay deployment.",
            failed_phase=row.failure_phase,
            failure_cause=row.failure_cause or row.failure_code,
            replayed=replayed,
        )

    def _scope(
        self, session: Session, target_id: UUID, operation_id: UUID
    ) -> tuple[McpWorkspaceDeploymentOperationRow, ManagedInstanceRow]:
        row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
        target = session.get(TargetRow, str(target_id))
        instance = session.get(ManagedInstanceRow, str(target_id))
        if (
            row is None
            or target is None
            or instance is None
            or row.target_id != str(target_id)
            or target.target_type is not TargetType.MANAGED
            or instance.instance_type is not InstanceType.DISPOSABLE
            or row.target_class != "internal_lab"
            or row.deployment_mode != "direct"
        ):
            raise WorkspaceDeploymentRecoveryError("managed_deployment_not_found")
        return row, instance

    def _pending(
        self, operation_id: UUID, reason: str
    ) -> WorkspaceDeploymentRecoveryResult:
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            if row is None:
                raise WorkspaceDeploymentRecoveryError("managed_deployment_not_found")
            if row.state in {"ambiguous", "reconciliation_required"}:
                row.recovery_state = "still_requires_admin"
                row.recovery_at = timestamp(datetime.now(UTC))
            return self._result(row, "still_requires_admin", reason)

    def reconcile(
        self, target_id: UUID, operation_id: UUID
    ) -> WorkspaceDeploymentRecoveryResult:
        with self._sessions() as session:
            row, _instance = self._scope(session, target_id, operation_id)
            workspace_id = UUID(row.workspace_id)
        with workspace_coordination_lock(self._data_root, workspace_id, exclusive=True):
            return self._reconcile_locked(target_id, operation_id)

    def _reconcile_locked(
        self, target_id: UUID, operation_id: UUID
    ) -> WorkspaceDeploymentRecoveryResult:
        with self._sessions() as session:
            row, instance = self._scope(session, target_id, operation_id)
            if row.state == "failed" and row.recovery_state in {
                "contained_requested_tree",
                "contained_prior_tree",
            }:
                return self._result(
                    row,
                    cast(RecoveryOutcome, row.recovery_state),
                    "historical_containment_receipt",
                    replayed=True,
                )
            if row.state not in {"ambiguous", "reconciliation_required"}:
                raise WorkspaceDeploymentRecoveryError("deployment_not_reconcilable")
            workspace = session.get(AgentWorkspaceRow, row.workspace_id)
            baseline = session.get(
                DevelopmentSourceDeploymentRow,
                (row.target_id, row.development_source_id),
            )
            attachment = session.get(
                ManagedInstanceDevelopmentSourceRow,
                (row.target_id, row.development_source_id),
            )
            revision = instance.runtime_state_revision
            container_id = row.original_container_id
            if (
                container_id is None
                and baseline is not None
                and datetime.fromisoformat(baseline.verified_at.replace("Z", "+00:00"))
                <= datetime.fromisoformat(row.requested_at.replace("Z", "+00:00"))
                and baseline.runtime_revision_id == row.runtime_revision_id
            ):
                container_id = baseline.trusted_container_id
            valid = (
                workspace is not None
                and baseline is not None
                and attachment is not None
                and workspace.generation == row.generation
                and workspace.current_tree_digest is not None
                and workspace.state.value == "ready"
                and workspace.bytes_state.value == "present"
                and workspace.development_source_id == row.development_source_id
                and instance.runtime_revision_id == row.runtime_revision_id
                and container_id is not None
                and baseline.runtime_revision_id == row.runtime_revision_id
                and baseline.trusted_container_id == container_id
                and datetime.fromisoformat(baseline.verified_at.replace("Z", "+00:00"))
                <= datetime.fromisoformat(row.requested_at.replace("Z", "+00:00"))
            )
            prior = (
                prior_workspace_copy(
                    session,
                    row,
                    container_id=container_id,
                    baseline_verified_at=datetime.fromisoformat(
                        baseline.verified_at.replace("Z", "+00:00")
                    ),
                )
                if valid and baseline is not None and container_id is not None
                else None
            )
            prior_digest = (
                prior.verified_destination_digest
                if prior is not None
                else None
                if baseline is None
                else baseline.verified_destination_digest
            )
            prior_source_digest = (
                prior.source_manifest_digest
                if prior is not None
                else None
                if baseline is None
                else baseline.source_manifest_digest
            )
            session.expunge(row)
        if not valid:
            return self._pending(
                operation_id, "retained_workspace_or_runtime_binding_unavailable"
            )
        assert baseline is not None and container_id is not None
        status = self._reconciliation.status(target_id)
        if status.operation_uuid != operation_id or not status.blocking:
            return self._pending(
                operation_id, "other_operation_requires_reconciliation"
            )
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(target_id)
            ]
            if (
                len(matches) != 1
                or matches[0].container_id != container_id
                or matches[0].runtime_revision_id != row.runtime_revision_id
            ):
                return self._pending(
                    operation_id, "original_container_identity_unconfirmed"
                )
            if matches[0].runtime_state is not ContainerRuntimeState.STOPPED:
                return self._pending(operation_id, "stop_target_then_recheck")
            proof = self._broker.inspect_mcp_workspace_deployment(
                deployment_operation_id=row.id,
                managed_instance_id=row.target_id,
                expected_container_id=container_id,
            )
            if (
                proof.deployment_operation_id != row.id
                or proof.managed_instance_id != row.target_id
                or proof.development_source_id != row.development_source_id
                or proof.runtime_revision_id != row.runtime_revision_id
                or proof.container_id != container_id
                or not (proof.matches_requested or proof.matches_prior)
                or proof.matches_prior
                != (proof.installed_manifest_digest == prior_digest)
                or proof.matches_requested
                != (
                    proof.installed_manifest_digest
                    == proof.requested_destination_digest
                )
            ):
                return self._pending(
                    operation_id, "installed_tree_evidence_unconfirmed"
                )
            final = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == row.target_id
            ]
            if (
                len(final) != 1
                or final[0].container_id != container_id
                or final[0].runtime_revision_id != row.runtime_revision_id
                or final[0].runtime_state is not ContainerRuntimeState.STOPPED
            ):
                return self._pending(
                    operation_id, "containment_changed_during_inspection"
                )
        except Exception:
            return self._pending(
                operation_id, "broker_or_installed_tree_evidence_unavailable"
            )
        if self._reconciliation.status(target_id).operation_uuid != operation_id:
            return self._pending(
                operation_id, "other_operation_requires_reconciliation"
            )
        with self._sessions() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            current, instance = self._scope(session, target_id, operation_id)
            workspace = session.get(AgentWorkspaceRow, current.workspace_id)
            if (
                current.state not in {"ambiguous", "reconciliation_required"}
                or instance.runtime_state_revision != revision
                or session.get(ManagedInstanceMutationLeaseRow, row.target_id)
                is not None
                or workspace is None
                or workspace.generation != row.generation
                or workspace.current_tree_digest != proof.requested_manifest_digest
            ):
                session.rollback()
                return self._pending(operation_id, "state_changed_during_inspection")
            outcome: RecoveryOutcome = (
                "contained_requested_tree"
                if proof.matches_requested
                else "contained_prior_tree"
            )
            current.failure_cause = current.failure_cause or current.failure_code
            current.state = "failed"
            current.failure_code = "deployment_contained"
            current.recovery_state = outcome
            current.recovery_digest = proof.installed_manifest_digest
            current.recovery_at = timestamp(datetime.now(UTC))
            current.verified_destination_digest = proof.installed_manifest_digest
            current.source_manifest_digest = (
                proof.requested_manifest_digest
                if proof.matches_requested
                else prior_source_digest
            )
            result = self._result(
                current, outcome, "exact_stopped_container_and_tree_verified"
            )
            session.commit()
        return result

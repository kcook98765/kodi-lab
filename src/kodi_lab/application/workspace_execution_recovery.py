"""Exact-container containment for one evidence-bound ambiguous managed smoke."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import AddonInventoryQueryStatus
from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskState,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationOperationKind,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstanceMutationKind,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    TargetRow,
    WorkspaceExecutionRecoveryOperationRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeCurrentProjection,
    ManagedRuntimeProjectionRepository,
    ManagedRuntimeProjectionResult,
    ManagedRuntimeReconciliationBlockedError,
    RuntimeExecutionProfileRepository,
)

_LEASE = timedelta(minutes=15)


class WorkspaceExecutionRecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceExecutionRecoveryOutcome(StrEnum):
    RECOVERED = "recovered"
    CONTAINED_BUT_NOT_READY = "contained_but_not_ready"
    STILL_REQUIRES_ADMIN = "still_requires_admin"


class WorkspaceExecutionRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    execution_operation_id: UUID
    recovery_operation_id: UUID
    outcome: WorkspaceExecutionRecoveryOutcome


def _timestamp(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class WorkspaceExecutionRecoveryService:
    """Transport-neutral recovery with no caller-controlled lifecycle parameters."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: Any,
        inventory: Any,
        *,
        readiness_factory: Callable[[Any, Any], Any],
        readiness_registry: ReadinessTaskRegistry | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._broker = broker
        self._inventory = inventory
        self._readiness_factory = readiness_factory
        self._readiness_registry = readiness_registry
        self._clock = clock
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    def context(
        self, client_id: UUID, execution_operation_id: UUID
    ) -> tuple[UUID, UUID]:
        """Derive only the workspace/target needed for ordinary MCP authorization."""

        with self._sessions() as session:
            execution = session.get(
                McpWorkspaceExecutionOperationRow, str(execution_operation_id)
            )
            if execution is None or execution.client_id != str(client_id):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            return UUID(execution.workspace_id), UUID(execution.target_id)

    def recover(
        self, *, client_id: UUID, request_id: UUID, execution_operation_id: UUID
    ) -> WorkspaceExecutionRecoveryResult:
        existing = self._request(client_id, request_id)
        if existing is not None:
            if existing.execution_operation_id != str(execution_operation_id):
                raise WorkspaceExecutionRecoveryError("request_id_conflict")
            return self._result(existing)

        workspace_id, target_id = self.context(client_id, execution_operation_id)
        status = self._reconciliation.status(target_id)
        if (
            status.operation_kind
            is not ReconciliationOperationKind.MCP_WORKSPACE_EXECUTION
            or status.operation_uuid != execution_operation_id
            or not status.blocking
        ):
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed")

        with self._sessions() as session:
            execution, deployment, instance = self._eligible_rows(
                session, client_id, execution_operation_id, workspace_id, target_id
            )
            original_container_id = execution.original_container_id
            assert original_container_id is not None
            runtime_revision_id = instance.runtime_revision_id
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(runtime_revision_id)
            )
            if profile is None:
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            expected_deployment_id = deployment.id
            addon_id = execution.addon_id
            addon_version = execution.addon_version

        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(target_id)
            ]
        except Exception as error:
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed") from error
        if (
            len(matches) != 1
            or matches[0].container_id != original_container_id
            or matches[0].runtime_state
            not in {ContainerRuntimeState.RUNNING, ContainerRuntimeState.STOPPED}
        ):
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
        initial_state = matches[0].runtime_state
        if (
            execution.execution_classification == "ambiguous"
            and initial_state is not ContainerRuntimeState.STOPPED
        ):
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed")

        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        recovery_id = uuid4()
        now = self._clock()
        try:
            with self._sessions() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                prior = session.scalar(
                    select(WorkspaceExecutionRecoveryOperationRow).where(
                        WorkspaceExecutionRecoveryOperationRow.client_id
                        == str(client_id),
                        WorkspaceExecutionRecoveryOperationRow.request_id
                        == str(request_id),
                    )
                )
                if prior is not None:
                    if prior.execution_operation_id != str(execution_operation_id):
                        raise WorkspaceExecutionRecoveryError("request_id_conflict")
                    return self._result(prior)
                self._eligible_rows(
                    session, client_id, execution_operation_id, workspace_id, target_id
                )
                current_status = self._reconciliation.status(target_id)
                if (
                    current_status.operation_kind
                    is not ReconciliationOperationKind.MCP_WORKSPACE_EXECUTION
                    or current_status.operation_uuid != execution_operation_id
                    or not current_status.blocking
                ):
                    raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
                lease = ManagedInstanceMutationLeaseRepository(session)
                lease.reserve(
                    target_id,
                    kind=ManagedInstanceMutationKind.WORKSPACE_EXECUTION_RECOVERY,
                    operation_id=recovery_id,
                )
                if not lease.claim(
                    target_id,
                    operation_id=recovery_id,
                    token_digest=digest,
                    now=now,
                    expires_at=now + _LEASE,
                ):
                    raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
                revision = ManagedRuntimeProjectionRepository(
                    session
                ).observation_token(target_id)
                if revision is None:
                    raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
                session.add(
                    WorkspaceExecutionRecoveryOperationRow(
                        id=str(recovery_id),
                        client_id=str(client_id),
                        request_id=str(request_id),
                        execution_operation_id=str(execution_operation_id),
                        state="running",
                        phase="reserved",
                        outcome=None,
                        owned_runtime_state_revision=revision.runtime_state_revision,
                        containment_confirmed=False,
                        readiness_confirmed=False,
                        provenance_confirmed=False,
                        requested_at=_timestamp(now),
                        completed_at=None,
                    )
                )
                session.commit()
        except (
            IntegrityError,
            ManagedRuntimeReconciliationBlockedError,
            SQLAlchemyError,
        ) as error:
            existing = self._request(client_id, request_id)
            if existing is not None and existing.execution_operation_id == str(
                execution_operation_id
            ):
                return self._result(existing)
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed") from error

        contained = False
        ready = False
        provenance = False
        observed_state: ContainerRuntimeState | None = None
        try:
            self._phase(recovery_id, target_id, digest, "stopping")
            stopped = self._broker.stop_managed_container(
                managed_instance_id=str(target_id),
                expected_container_id=original_container_id,
                expected_runtime_state=initial_state,
            )
            if (
                stopped.container_id != original_container_id
                or stopped.runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(target_id)
            ]
            if (
                len(matches) != 1
                or matches[0].container_id != original_container_id
                or matches[0].runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            contained = True
            observed_state = ContainerRuntimeState.STOPPED
            if self._readiness_registry is not None:
                self._readiness_registry.clear(target_id)
            self._phase(recovery_id, target_id, digest, "contained", contained=True)
            self._phase(recovery_id, target_id, digest, "starting")
            started = self._broker.start_managed_container(
                managed_instance_id=str(target_id),
                expected_container_id=original_container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if (
                started.container_id != original_container_id
                or started.runtime_state is not ContainerRuntimeState.RUNNING
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            observed_state = ContainerRuntimeState.RUNNING
            self._phase(recovery_id, target_id, digest, "verifying")
            if not self._wait_ready(target_id, profile):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            ready = True
            verified = self._broker.verify_mcp_workspace_destination(
                deployment_operation_id=expected_deployment_id,
                managed_instance_id=str(target_id),
                expected_container_id=original_container_id,
            )
            if (
                not verified.matched
                or verified.container_id != original_container_id
                or verified.runtime_revision_id != runtime_revision_id
                or verified.addon_id != addon_id
                or verified.addon_version != addon_version
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            inventory = self._inventory.get(target_id)
            item = next(
                (item for item in inventory.items if item.addon_id == addon_id), None
            )
            if (
                inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
                or item is None
                or item.version != addon_version
                or not item.enabled
                or item.kodi_type != "xbmc.python.pluginsource"
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            final_matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(target_id)
            ]
            if (
                len(final_matches) != 1
                or final_matches[0].container_id != original_container_id
                or final_matches[0].runtime_state is not ContainerRuntimeState.RUNNING
                or final_matches[0].health_state is not ContainerHealthState.HEALTHY
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            final_observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(target_id),
                expected_container_id=original_container_id,
            )
            if (
                final_observation.container_id != original_container_id
                or final_observation.runtime_revision_id != runtime_revision_id
                or final_observation.process_state is not KodiProcessState.RUNNING
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            provenance = True
        except Exception:
            # A failed or unconfirmed broker call never proves containment.
            pass
        outcome = (
            WorkspaceExecutionRecoveryOutcome.RECOVERED
            if contained and ready and provenance
            else WorkspaceExecutionRecoveryOutcome.CONTAINED_BUT_NOT_READY
            if contained
            else WorkspaceExecutionRecoveryOutcome.STILL_REQUIRES_ADMIN
        )
        return self._complete(
            recovery_id,
            target_id,
            digest,
            outcome,
            contained=contained,
            ready=ready,
            provenance=provenance,
            observed_state=observed_state,
        )

    def _eligible_rows(
        self,
        session: Session,
        client_id: UUID,
        execution_id: UUID,
        workspace_id: UUID,
        target_id: UUID,
    ) -> tuple[
        McpWorkspaceExecutionOperationRow,
        McpWorkspaceDeploymentOperationRow,
        ManagedInstanceRow,
    ]:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(execution_id))
        deployment = (
            None
            if execution is None
            else session.get(
                McpWorkspaceDeploymentOperationRow, execution.deployment_id
            )
        )
        target = session.get(TargetRow, str(target_id))
        instance = session.get(ManagedInstanceRow, str(target_id))
        workspace = session.get(AgentWorkspaceRow, str(workspace_id))
        binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
        source_attachment = (
            None
            if execution is None
            else session.get(
                ManagedInstanceDevelopmentSourceRow,
                {
                    "managed_instance_id": str(target_id),
                    "development_source_id": execution.development_source_id,
                },
            )
        )
        target_grant = session.get(
            McpClientTargetGrantRow,
            {"client_id": str(client_id), "target_id": str(target_id)},
        )
        source_grant = (
            None
            if execution is None
            else session.get(
                McpClientDevelopmentSourceGrantRow,
                {
                    "client_id": str(client_id),
                    "development_source_id": execution.development_source_id,
                },
            )
        )
        if (
            execution is None
            or deployment is None
            or target is None
            or instance is None
            or workspace is None
            or binding is None
            or source_attachment is None
            or target_grant is None
            or source_grant is None
            or execution.client_id != str(client_id)
            or execution.workspace_id != str(workspace_id)
            or execution.target_id != str(target_id)
            or execution.execution_kind != "smoke"
            or execution.state != "ambiguous"
            or execution.execution_classification not in {"timeout", "ambiguous"}
            or execution.failure_code != "execution_ambiguous"
            or not execution.invocation_dispatched
            or execution.original_container_id is None
            or execution.original_runtime_state_revision is None
            or target.target_type is not TargetType.MANAGED
            or instance.instance_type is not InstanceType.DISPOSABLE
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.development_source_id != execution.development_source_id
            or binding.client_id != str(client_id)
            or binding.development_source_id != execution.development_source_id
            or deployment.id != execution.deployment_id
            or deployment.workspace_id != execution.workspace_id
            or deployment.target_id != execution.target_id
            or deployment.generation != execution.generation
            or (
                execution.execution_classification == "ambiguous"
                and workspace.generation != execution.generation
            )
            or deployment.target_class != "internal_lab"
            or deployment.state != "succeeded"
            or deployment.runtime_revision_id != instance.runtime_revision_id
            or deployment.source_manifest_digest is None
            or deployment.verified_destination_digest is None
            or not deployment.addon_enabled
            or execution.original_runtime_state_revision
            > instance.runtime_state_revision
        ):
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
        if execution.execution_classification == "ambiguous":
            self._require_later_stop(session, execution, instance)
        return execution, deployment, instance

    def _require_later_stop(
        self,
        session: Session,
        execution: McpWorkspaceExecutionOperationRow,
        instance: ManagedInstanceRow,
    ) -> None:
        """Require durable later containment; never infer a successful smoke."""
        lifecycle = session.scalar(
            select(ManagedInstanceLifecycleOperationRow)
            .where(
                ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                == execution.target_id
            )
            .order_by(
                ManagedInstanceLifecycleOperationRow.requested_at.desc(),
                ManagedInstanceLifecycleOperationRow.id.desc(),
            )
            .limit(1)
        )
        if (
            instance.runtime_state is not InstanceRuntimeState.STOPPED
            or execution.phase != "finalize"
            or execution.completed_at is None
            or execution.result_classification is not None
            or execution.item_count is not None
            or execution.kodi_error_code is not None
            or execution.kodi_error_message is not None
            or lifecycle is None
            or lifecycle.managed_instance_id != execution.target_id
            or lifecycle.kind.value != "stop"
            or lifecycle.state.value != "succeeded"
            or lifecycle.resulting_state != "stopped"
            or lifecycle.reconciliation_required
            or lifecycle.completed_at is None
            or execution.original_runtime_state_revision is None
            or execution.original_runtime_state_revision
            >= instance.runtime_state_revision
        ):
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
        try:
            times = [
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in (
                    execution.requested_at,
                    execution.started_at or "",
                    execution.completed_at,
                    lifecycle.requested_at,
                    lifecycle.started_at,
                    lifecycle.completed_at,
                )
            ]
            if any(value.utcoffset() != timedelta(0) for value in times) or not (
                times[0]
                <= times[1]
                <= times[2]
                < times[3]
                <= times[4]
                <= times[5]
                <= self._clock()
            ):
                raise ValueError("Stop chronology is not proven")
        except (ValueError, TypeError) as error:
            raise WorkspaceExecutionRecoveryError("recovery_not_allowed") from error

    def _wait_ready(self, target_id: UUID, profile: Any) -> bool:
        registry = self._readiness_registry
        if registry is None:
            result = self._readiness_factory(self._broker, profile).wait_until_usable(
                str(target_id)
            )
            return getattr(result.usability_state, "value", "") == "usable"
        registry.start(
            target_id,
            lambda cancelled: self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(target_id), cancelled=cancelled.is_set),
        )
        deadline = time.monotonic() + max(profile.readiness_timeout_seconds, 90) + 5
        while time.monotonic() < deadline:
            state = registry.snapshot(target_id).state
            if state is ReadinessTaskState.USABLE:
                return True
            if state in {
                ReadinessTaskState.FAILED,
                ReadinessTaskState.TIMED_OUT,
                ReadinessTaskState.CANCELLED,
            }:
                return False
            time.sleep(0.05)
        return False

    def _phase(
        self,
        recovery_id: UUID,
        target_id: UUID,
        digest: str,
        phase: str,
        *,
        contained: bool = False,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(WorkspaceExecutionRecoveryOperationRow, str(recovery_id))
            lease = ManagedInstanceMutationLeaseRepository(session).current(target_id)
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                target_id
            )
            if (
                row is None
                or row.state != "running"
                or lease is None
                or lease.operation_id != str(recovery_id)
                or lease.lease_token_digest != digest
                or lease.lease_expires_at is None
                or lease.lease_expires_at <= _timestamp(self._clock())
                or token is None
                or token.runtime_state_revision != row.owned_runtime_state_revision
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            row.phase = phase
            if contained:
                row.containment_confirmed = True

    def _complete(
        self,
        recovery_id: UUID,
        target_id: UUID,
        digest: str,
        outcome: WorkspaceExecutionRecoveryOutcome,
        *,
        contained: bool,
        ready: bool,
        provenance: bool,
        observed_state: ContainerRuntimeState | None,
    ) -> WorkspaceExecutionRecoveryResult:
        with self._sessions.begin() as session:
            row = session.get(WorkspaceExecutionRecoveryOperationRow, str(recovery_id))
            lease = ManagedInstanceMutationLeaseRepository(session).current(target_id)
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                target_id
            )
            if (
                row is None
                or row.state != "running"
                or lease is None
                or lease.operation_id != str(recovery_id)
                or lease.lease_token_digest != digest
                or lease.lease_expires_at is None
                or lease.lease_expires_at <= _timestamp(self._clock())
                or token is None
                or token.runtime_state_revision != row.owned_runtime_state_revision
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            projection = ManagedRuntimeProjectionRepository(session)
            if observed_state is not None:
                running = observed_state is ContainerRuntimeState.RUNNING
                usable = outcome is WorkspaceExecutionRecoveryOutcome.RECOVERED
                applied = projection.finalize_owned(
                    target_id,
                    operation_id=recovery_id,
                    token_digest=digest,
                    owned_revision=row.owned_runtime_state_revision,
                    projection=ManagedRuntimeCurrentProjection(
                        runtime_state=(
                            InstanceRuntimeState.RUNNING
                            if running
                            else InstanceRuntimeState.STOPPED
                        ),
                        availability=(
                            TargetAvailability.AVAILABLE
                            if usable
                            else TargetAvailability.UNAVAILABLE
                        ),
                        health=HealthStatus.HEALTHY if usable else HealthStatus.WARNING,
                        connection_state=(
                            ConnectionState.CONNECTED
                            if usable
                            else ConnectionState.DISCONNECTED
                        ),
                    ),
                )
                if applied is not ManagedRuntimeProjectionResult.APPLIED:
                    raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            row.state = "completed"
            row.phase = "finalized"
            row.outcome = outcome.value
            row.containment_confirmed = contained
            row.readiness_confirmed = ready
            row.provenance_confirmed = provenance
            row.completed_at = _timestamp(self._clock())
            if not ManagedInstanceMutationLeaseRepository(session).release(
                target_id, operation_id=recovery_id, token_digest=digest
            ):
                raise WorkspaceExecutionRecoveryError("recovery_not_allowed")
            return self._result(row)

    def _request(
        self, client_id: UUID, request_id: UUID
    ) -> WorkspaceExecutionRecoveryOperationRow | None:
        with self._sessions() as session:
            row = session.scalar(
                select(WorkspaceExecutionRecoveryOperationRow).where(
                    WorkspaceExecutionRecoveryOperationRow.client_id == str(client_id),
                    WorkspaceExecutionRecoveryOperationRow.request_id
                    == str(request_id),
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _result(
        row: WorkspaceExecutionRecoveryOperationRow,
    ) -> WorkspaceExecutionRecoveryResult:
        if row.state != "completed" or row.outcome is None:
            raise WorkspaceExecutionRecoveryError("recovery_in_progress")
        return WorkspaceExecutionRecoveryResult(
            request_id=UUID(row.request_id),
            execution_operation_id=UUID(row.execution_operation_id),
            recovery_operation_id=UUID(row.id),
            outcome=WorkspaceExecutionRecoveryOutcome(row.outcome),
        )

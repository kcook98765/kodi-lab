"""Exact-generation MCP workspace deployment and fixed smoke execution."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.development_source_enablements import (
    DependencyStatus,
    DevelopmentSourceEnablementService,
)
from kodi_lab.application.mcp_development_requests import (
    DEVELOPMENT_REQUEST_MAX_GLOBAL,
    DEVELOPMENT_REQUEST_MAX_PER_CLIENT,
    DEVELOPMENT_REQUEST_RETENTION,
    deployment_terminal_evidence_is_consistent_sql,
    execution_terminal_evidence_is_consistent_sql,
    prune_expired_safe_development_requests,
)
from kodi_lab.application.mcp_development_requests import (
    WORKSPACE_DEPLOY_OPERATION_KIND as DEPLOY_OPERATION_KIND,
)
from kodi_lab.application.mcp_development_requests import (
    WORKSPACE_EXECUTE_OPERATION_KIND as EXECUTE_OPERATION_KIND,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    copied_video_plugin_library,
    parse_captured_addon_xml,
)
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    DevelopmentSourceDeploymentConfidence,
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RuntimeExecutionProfile,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsUnsupportedResponseError,
    KodiAddonEnablementAdapter,
    KodiAddonInventoryAdapter,
    KodiAddonInventoryIdentityError,
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryUnsupportedResponseError,
    KodiAddonSmokeAdapter,
    KodiAddonSmokeAmbiguousError,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeTimeoutError,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError, KodiReadinessCoordinator
from kodi_lab.storage.models import (
    AgentWorkspacePromotionPlanRow,
    AgentWorkspaceRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceValidationRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)
from kodi_lab.workspace import workspace_coordination_lock

OPERATION_LEASE = timedelta(minutes=15)
DEVELOPMENT_OPERATION_PRUNE_LIMIT = 256


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _from_text(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _fingerprint(
    kind: str, workspace_id: UUID, generation: int, target_id: UUID
) -> str:
    value = f"{kind}\x00{workspace_id}\x00{generation}\x00{target_id}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class McpWorkspaceDevelopmentError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceDeploymentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    deployment_id: UUID
    workspace_id: UUID
    source_id: UUID
    target_id: UUID
    generation: int = Field(ge=0)
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: str = Field(max_length=32)
    result_classification: str = Field(max_length=64)
    inventory_classification: str = Field(max_length=16)
    addon_enabled: bool
    started_at: datetime
    completed_at: datetime
    reconciliation_required: bool = False


class WorkspaceExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    request_id: UUID
    execution_id: UUID
    deployment_id: UUID
    workspace_id: UUID
    source_id: UUID
    target_id: UUID
    generation: int = Field(ge=0)
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: str = Field(max_length=32)
    execution_classification: str = Field(max_length=32)
    result_classification: str = Field(max_length=64)
    item_count: int | None = Field(default=None, ge=0, le=1)
    started_at: datetime
    completed_at: datetime
    reconciliation_required: bool = False


class WorkspaceDevelopmentProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_id: UUID | None = None
    validation_state: str | None = None
    validation_generation: int | None = Field(default=None, ge=0)
    validated_at: datetime | None = None
    deployment_id: UUID | None = None
    deployment_state: str | None = None
    deployment_generation: int | None = Field(default=None, ge=0)
    deployment_target_id: UUID | None = None
    execution_id: UUID | None = None
    execution_state: str | None = None
    execution_generation: int | None = Field(default=None, ge=0)
    execution_target_id: UUID | None = None


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def stop_managed_container(self, **kwargs: Any) -> Any: ...

    def start_managed_container(self, **kwargs: Any) -> Any: ...

    def copy_sync_mcp_workspace(
        self, **kwargs: Any
    ) -> CopySyncAttachedDevelopmentSourceResult: ...

    def verify_mcp_workspace_destination(
        self, **kwargs: Any
    ) -> VerifyAttachedDevelopmentSourceDestinationResult: ...


class BoundedIntervalRateLimiter:
    """One fixed interval per bounded server-derived key with idle retirement."""

    def __init__(
        self,
        interval: float,
        *,
        maximum_keys: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval
        self._maximum = maximum_keys
        self._clock = clock
        self._keys: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            cutoff = now - max(self._interval * 2, 900.0)
            while self._keys and next(iter(self._keys.values())) < cutoff:
                self._keys.popitem(last=False)
            prior = self._keys.get(key)
            if prior is not None and now - prior < self._interval:
                self._keys.move_to_end(key)
                return False
            if prior is None and len(self._keys) >= self._maximum:
                return False
            self._keys[key] = now
            self._keys.move_to_end(key)
            return True


class McpWorkspaceDevelopmentService:
    """Transport-neutral closed deployment and execution application service."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _Broker,
        inventory: ManagedInstanceAddonInventoryService,
        workspaces: AgentWorkspaceService,
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        readiness_factory: Callable[[Any, Any], Any] = KodiReadinessCoordinator,
        deployment_rate: BoundedIntervalRateLimiter | None = None,
        execution_rate: BoundedIntervalRateLimiter | None = None,
        failure_injector: Callable[[str], None] = lambda _phase: None,
        inventory_adapter_factory: Callable[
            [Any, Any], Any
        ] = KodiAddonInventoryAdapter,
        details_adapter_factory: Callable[[Any, Any], Any] = KodiAddonEnablementAdapter,
        smoke_adapter_factory: Callable[[Any, Any], Any] = KodiAddonSmokeAdapter,
        retention: timedelta = DEVELOPMENT_REQUEST_RETENTION,
        max_per_client: int = DEVELOPMENT_REQUEST_MAX_PER_CLIENT,
        max_global: int = DEVELOPMENT_REQUEST_MAX_GLOBAL,
    ) -> None:
        if retention <= timedelta(0) or max_per_client < 1 or max_global < 1:
            raise ValueError("development request bounds must be positive")
        self._sessions = session_factory
        self._broker = broker
        self._inventory = inventory
        self._workspaces = workspaces
        self._data_root = data_root
        self._clock = clock
        self._readiness_factory = readiness_factory
        self._deployment_rate = deployment_rate or BoundedIntervalRateLimiter(300.0)
        self._execution_rate = execution_rate or BoundedIntervalRateLimiter(60.0)
        self._failure = failure_injector
        self._inventory_factory = inventory_adapter_factory
        self._details_factory = details_adapter_factory
        self._smoke_factory = smoke_adapter_factory
        self._retention = retention
        self._max_per_client = max_per_client
        self._max_global = max_global

    def record_validation(
        self,
        *,
        workspace_id: UUID,
        source_id: UUID,
        generation: int,
        addon_id: str,
        addon_version: str,
        extension_types: tuple[str, ...],
        validated_at: datetime,
    ) -> UUID:
        evidence_id = uuid4()
        encoded_types = json.dumps(list(extension_types), separators=(",", ":"))
        with self._sessions.begin() as session:
            row = session.scalar(
                select(McpWorkspaceValidationRow).where(
                    McpWorkspaceValidationRow.workspace_id == str(workspace_id)
                )
            )
            values = {
                "id": str(evidence_id),
                "development_source_id": str(source_id),
                "generation": generation,
                "classification": "passed",
                "addon_id": addon_id,
                "addon_version": addon_version,
                "extension_types_json": encoded_types,
                "validated_at": _to_text(validated_at),
            }
            if row is None:
                session.add(
                    McpWorkspaceValidationRow(workspace_id=str(workspace_id), **values)
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return evidence_id

    def deploy(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        expected_generation: int,
        target_id: UUID,
    ) -> WorkspaceDeploymentResult:
        row, created = self._claim_deployment(
            client_id, request_id, workspace_id, expected_generation, target_id
        )
        if not created:
            return self._replay_deployment(
                row, workspace_id, expected_generation, target_id
            )
        if not self._deployment_rate.allow(f"{client_id}:{target_id}"):
            self._finish_deployment(row.id, "failed", "rate_limited")
            raise McpWorkspaceDevelopmentError("rate_limited")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            self._acquire_deployment(row, digest)
            with workspace_coordination_lock(
                self._data_root, workspace_id, exclusive=True, blocking=False
            ):
                return self._run_deployment(UUID(row.id), digest)
        except McpWorkspaceDevelopmentError as error:
            current = self._deployment(UUID(row.id))
            if current is not None and current.state in {"pending", "running"}:
                uncertain = current.mutation_started
                if uncertain:
                    self._contain_current_deployment(current)
                self._finish_deployment(
                    row.id,
                    "reconciliation_required" if uncertain else "failed",
                    "deployment_ambiguous" if uncertain else error.code,
                    digest=digest,
                )
                if uncertain:
                    raise McpWorkspaceDevelopmentError(
                        "deployment_ambiguous"
                    ) from error
            raise
        except Exception as error:
            current = self._deployment(UUID(row.id))
            uncertain = bool(current and current.mutation_started)
            if current is not None and uncertain:
                self._contain_current_deployment(current)
            self._finish_deployment(
                row.id,
                "reconciliation_required" if uncertain else "failed",
                "deployment_ambiguous" if uncertain else "deployment_conflict",
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError(
                "deployment_ambiguous" if uncertain else "deployment_conflict"
            ) from error

    def execute(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        expected_generation: int,
        target_id: UUID,
    ) -> WorkspaceExecutionResult:
        row, created = self._claim_execution(
            client_id, request_id, workspace_id, expected_generation, target_id
        )
        if not created:
            return self._replay_execution(
                row, workspace_id, expected_generation, target_id
            )
        if not self._execution_rate.allow(str(client_id)):
            self._finish_execution(row.id, "failed", "rate_limited")
            raise McpWorkspaceDevelopmentError("rate_limited")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            self._acquire_execution(row, digest)
            with workspace_coordination_lock(
                self._data_root, workspace_id, exclusive=True, blocking=False
            ):
                return self._run_execution(UUID(row.id), digest)
        except McpWorkspaceDevelopmentError as error:
            current = self._execution(UUID(row.id))
            if current is not None and current.state in {"pending", "running"}:
                dispatched = current.invocation_dispatched
                self._finish_execution(
                    row.id,
                    "ambiguous" if dispatched else "failed",
                    "execution_ambiguous" if dispatched else error.code,
                    digest=digest,
                    execution_classification=(
                        "ambiguous" if dispatched else "not_attempted"
                    ),
                )
                if dispatched:
                    raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
            raise
        except Exception as error:
            current = self._execution(UUID(row.id))
            dispatched = bool(current and current.invocation_dispatched)
            self._finish_execution(
                row.id,
                "ambiguous" if dispatched else "failed",
                "execution_ambiguous" if dispatched else "execution_not_deployed",
                digest=digest,
                execution_classification="ambiguous" if dispatched else "not_attempted",
            )
            raise McpWorkspaceDevelopmentError(
                "execution_ambiguous" if dispatched else "execution_not_deployed"
            ) from error

    def projection(self, workspace_id: UUID) -> WorkspaceDevelopmentProjection:
        with self._sessions() as session:
            validation = session.scalar(
                select(McpWorkspaceValidationRow).where(
                    McpWorkspaceValidationRow.workspace_id == str(workspace_id)
                )
            )
            deployment = session.scalar(
                select(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.workspace_id == str(workspace_id)
                )
                .order_by(
                    McpWorkspaceDeploymentOperationRow.requested_at.desc(),
                    McpWorkspaceDeploymentOperationRow.id.desc(),
                )
            )
            execution = session.scalar(
                select(McpWorkspaceExecutionOperationRow)
                .where(
                    McpWorkspaceExecutionOperationRow.workspace_id == str(workspace_id)
                )
                .order_by(
                    McpWorkspaceExecutionOperationRow.requested_at.desc(),
                    McpWorkspaceExecutionOperationRow.id.desc(),
                )
            )
        return WorkspaceDevelopmentProjection(
            validation_id=None if validation is None else UUID(validation.id),
            validation_state=None if validation is None else validation.classification,
            validation_generation=None if validation is None else validation.generation,
            validated_at=None
            if validation is None
            else _from_text(validation.validated_at),
            deployment_id=None if deployment is None else UUID(deployment.id),
            deployment_state=None if deployment is None else deployment.state,
            deployment_generation=None if deployment is None else deployment.generation,
            deployment_target_id=None
            if deployment is None
            else UUID(deployment.target_id),
            execution_id=None if execution is None else UUID(execution.id),
            execution_state=None if execution is None else execution.state,
            execution_generation=None if execution is None else execution.generation,
            execution_target_id=None
            if execution is None
            else UUID(execution.target_id),
        )

    def retention_blocker(self, workspace_id: UUID, generation: int) -> str | None:
        with self._sessions() as session:
            unresolved_deployment = session.scalar(
                select(func.count())
                .select_from(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.workspace_id
                    == str(workspace_id),
                    McpWorkspaceDeploymentOperationRow.state.in_(
                        ("pending", "running", "ambiguous", "reconciliation_required")
                    ),
                )
            )
            unresolved_execution = session.scalar(
                select(func.count())
                .select_from(McpWorkspaceExecutionOperationRow)
                .where(
                    McpWorkspaceExecutionOperationRow.workspace_id == str(workspace_id),
                    McpWorkspaceExecutionOperationRow.state.in_(
                        ("pending", "running", "ambiguous", "reconciliation_required")
                    ),
                )
            )
            current = session.scalar(
                select(func.count())
                .select_from(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.workspace_id
                    == str(workspace_id),
                    McpWorkspaceDeploymentOperationRow.generation == generation,
                    McpWorkspaceDeploymentOperationRow.state == "succeeded",
                )
            )
        if unresolved_deployment or unresolved_execution:
            return "workspace_development_operation_unresolved"
        if current:
            return "workspace_current_deployment_retained"
        return None

    def sweep_startup(self) -> int:
        """Close interrupted work without redispatching either mutation."""

        with self._sessions() as session:
            deployments = list(
                session.scalars(
                    select(McpWorkspaceDeploymentOperationRow).where(
                        McpWorkspaceDeploymentOperationRow.state.in_(
                            ("pending", "running")
                        )
                    )
                )
            )
            executions = list(
                session.scalars(
                    select(McpWorkspaceExecutionOperationRow).where(
                        McpWorkspaceExecutionOperationRow.state.in_(
                            ("pending", "running")
                        )
                    )
                )
            )
        for deployment in deployments:
            uncertain = deployment.mutation_started
            self._finish_deployment(
                deployment.id,
                "reconciliation_required" if uncertain else "failed",
                "deployment_ambiguous" if uncertain else "deployment_interrupted",
            )
        for execution in executions:
            self._finish_execution(
                execution.id,
                "ambiguous" if execution.invocation_dispatched else "failed",
                "execution_ambiguous"
                if execution.invocation_dispatched
                else "execution_interrupted",
                execution_classification=(
                    "ambiguous" if execution.invocation_dispatched else "not_attempted"
                ),
            )
        return len(deployments) + len(executions)

    def _claim_deployment(
        self,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
    ) -> tuple[McpWorkspaceDeploymentOperationRow, bool]:
        fingerprint = _fingerprint(
            DEPLOY_OPERATION_KIND, workspace_id, generation, target_id
        )
        operation_id = uuid4()
        now = self._clock()
        try:
            with self._sessions() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._prune_and_require_capacity(session, client_id, now)
                existing = self._request(
                    session, client_id, request_id, DEPLOY_OPERATION_KIND
                )
                if existing is not None:
                    row = session.get(
                        McpWorkspaceDeploymentOperationRow, existing.operation_id
                    )
                    if row is None:
                        raise McpWorkspaceDevelopmentError("deployment_ambiguous")
                    session.expunge(row)
                    session.commit()
                    return row, False
                workspace, validation, instance = self._deployment_preflight_rows(
                    session, workspace_id, generation, target_id
                )
                row = McpWorkspaceDeploymentOperationRow(
                    id=str(operation_id),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    workspace_id=str(workspace_id),
                    development_source_id=str(workspace.development_source_id),
                    target_id=str(target_id),
                    runtime_revision_id=str(instance.runtime_revision_id),
                    generation=generation,
                    addon_id=validation.addon_id,
                    addon_version=validation.addon_version,
                    state="pending",
                    phase="claimed",
                    mutation_started=False,
                    target_stopped=False,
                    sync_started=False,
                    sync_completed=False,
                    target_started=False,
                    inventory_classification="not_checked",
                    addon_enabled=None,
                    result_classification=None,
                    source_manifest_digest=None,
                    verified_destination_digest=None,
                    failure_code=None,
                    requested_at=_to_text(now),
                    started_at=None,
                    completed_at=None,
                )
                session.add(row)
                session.add(
                    McpDevelopmentRequestRow(
                        id=str(uuid4()),
                        client_id=str(client_id),
                        request_id=str(request_id),
                        operation_kind=DEPLOY_OPERATION_KIND,
                        parameter_fingerprint=fingerprint,
                        development_source_id=str(workspace.development_source_id),
                        workspace_id=str(workspace_id),
                        creation_operation_id=None,
                        operation_id=str(operation_id),
                        state="allocated",
                        created_at=_to_text(now),
                        updated_at=_to_text(now),
                    )
                )
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    target_id,
                    kind=ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT,
                    operation_id=operation_id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.MCP_WORKSPACE_DEPLOYMENT,
                    operation_id=operation_id,
                )
                session.flush()
                session.expunge(row)
                session.commit()
                return row, True
        except McpWorkspaceDevelopmentError:
            raise
        except IntegrityError as error:
            row = self._existing_deployment(client_id, request_id)
            if row is not None:
                return row, False
            raise McpWorkspaceDevelopmentError("deployment_conflict") from error
        except SQLAlchemyError as error:
            row = self._existing_deployment(client_id, request_id)
            if row is not None:
                return row, False
            raise McpWorkspaceDevelopmentError("deployment_ambiguous") from error

    def _claim_execution(
        self,
        client_id: UUID,
        request_id: UUID,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
    ) -> tuple[McpWorkspaceExecutionOperationRow, bool]:
        fingerprint = _fingerprint(
            EXECUTE_OPERATION_KIND, workspace_id, generation, target_id
        )
        operation_id = uuid4()
        now = self._clock()
        try:
            with self._sessions() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self._prune_and_require_capacity(session, client_id, now)
                existing = self._request(
                    session, client_id, request_id, EXECUTE_OPERATION_KIND
                )
                if existing is not None:
                    row = session.get(
                        McpWorkspaceExecutionOperationRow, existing.operation_id
                    )
                    if row is None:
                        raise McpWorkspaceDevelopmentError("execution_ambiguous")
                    session.expunge(row)
                    session.commit()
                    return row, False
                workspace = AgentWorkspaceRepository(session).get(workspace_id)
                if workspace is None or workspace.generation != generation:
                    raise McpWorkspaceDevelopmentError("workspace_generation_conflict")
                deployment = session.scalar(
                    select(McpWorkspaceDeploymentOperationRow)
                    .where(
                        McpWorkspaceDeploymentOperationRow.workspace_id
                        == str(workspace_id),
                        McpWorkspaceDeploymentOperationRow.target_id == str(target_id),
                        McpWorkspaceDeploymentOperationRow.generation == generation,
                        McpWorkspaceDeploymentOperationRow.state == "succeeded",
                    )
                    .order_by(McpWorkspaceDeploymentOperationRow.completed_at.desc())
                )
                if deployment is None:
                    raise McpWorkspaceDevelopmentError("execution_not_deployed")
                row = McpWorkspaceExecutionOperationRow(
                    id=str(operation_id),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    workspace_id=str(workspace_id),
                    development_source_id=str(workspace.development_source_id),
                    target_id=str(target_id),
                    deployment_id=deployment.id,
                    generation=generation,
                    addon_id=deployment.addon_id,
                    addon_version=deployment.addon_version,
                    state="pending",
                    phase="claimed",
                    invocation_dispatched=False,
                    execution_classification="not_attempted",
                    item_count=None,
                    result_classification=None,
                    failure_code=None,
                    requested_at=_to_text(now),
                    started_at=None,
                    completed_at=None,
                )
                session.add(row)
                session.add(
                    McpDevelopmentRequestRow(
                        id=str(uuid4()),
                        client_id=str(client_id),
                        request_id=str(request_id),
                        operation_kind=EXECUTE_OPERATION_KIND,
                        parameter_fingerprint=fingerprint,
                        development_source_id=str(workspace.development_source_id),
                        workspace_id=str(workspace_id),
                        creation_operation_id=None,
                        operation_id=str(operation_id),
                        state="allocated",
                        created_at=_to_text(now),
                        updated_at=_to_text(now),
                    )
                )
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    target_id,
                    kind=ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION,
                    operation_id=operation_id,
                )
                session.flush()
                session.expunge(row)
                session.commit()
                return row, True
        except McpWorkspaceDevelopmentError:
            raise
        except IntegrityError as error:
            row = self._existing_execution(client_id, request_id)
            if row is not None:
                return row, False
            raise McpWorkspaceDevelopmentError("deployment_conflict") from error
        except SQLAlchemyError as error:
            row = self._existing_execution(client_id, request_id)
            if row is not None:
                return row, False
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error

    def _deployment_preflight_rows(
        self, session: Session, workspace_id: UUID, generation: int, target_id: UUID
    ) -> tuple[Any, McpWorkspaceValidationRow, Any]:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        validation = session.scalar(
            select(McpWorkspaceValidationRow).where(
                McpWorkspaceValidationRow.workspace_id == str(workspace_id)
            )
        )
        instance = ManagedInstanceRepository(session).get(target_id)
        attachment = (
            None
            if workspace is None
            else ManagedInstanceDevelopmentSourceRepository(session).get(
                target_id, workspace.development_source_id
            )
        )
        source = (
            None
            if workspace is None
            else DevelopmentSourceRepository(session).get(
                workspace.development_source_id
            )
        )
        if workspace is None:
            raise McpWorkspaceDevelopmentError("workspace_not_found_or_not_authorized")
        if workspace.generation != generation:
            raise McpWorkspaceDevelopmentError("workspace_generation_conflict")
        if (
            workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.current_tree_digest is None
        ):
            raise McpWorkspaceDevelopmentError("workspace_not_ready")
        if validation is None:
            raise McpWorkspaceDevelopmentError("validation_required")
        if validation.generation != generation:
            raise McpWorkspaceDevelopmentError("validation_generation_mismatch")
        if (
            source is None
            or validation.development_source_id != str(source.id)
            or validation.addon_id != source.addon_id
            or validation.addon_id != workspace.source_addon_id
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        if (
            instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or attachment is None
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        baseline = DevelopmentSourceDeploymentRepository(session).get(
            target_id, source.id
        )
        prior_workspace = session.scalar(
            select(McpWorkspaceDeploymentOperationRow)
            .where(
                McpWorkspaceDeploymentOperationRow.workspace_id == str(workspace_id),
                McpWorkspaceDeploymentOperationRow.target_id == str(target_id),
                McpWorkspaceDeploymentOperationRow.development_source_id
                == str(source.id),
                McpWorkspaceDeploymentOperationRow.state == "succeeded",
                McpWorkspaceDeploymentOperationRow.runtime_revision_id
                == str(instance.runtime_revision_id),
            )
            .order_by(McpWorkspaceDeploymentOperationRow.completed_at.desc())
        )
        if prior_workspace is None and (
            baseline is None
            or baseline.confidence
            is not DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
            or baseline.runtime_revision_id != instance.runtime_revision_id
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        return workspace, validation, instance

    def _acquire_deployment(
        self, row: McpWorkspaceDeploymentOperationRow, digest: str
    ) -> None:
        now = self._clock()
        expires = now + OPERATION_LEASE
        with self._sessions.begin() as session:
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                UUID(row.target_id),
                operation_id=UUID(row.id),
                token_digest=digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=UUID(row.id),
                token_digest=digest,
                now=now,
                expires_at=expires,
            )
            changed = session.execute(
                update(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.id == row.id,
                    McpWorkspaceDeploymentOperationRow.state == "pending",
                )
                .values(state="running", phase="preflight", started_at=_to_text(now))
            )
            if not (
                mutation and heavy and int(getattr(changed, "rowcount", 0) or 0) == 1
            ):
                raise McpWorkspaceDevelopmentError("deployment_conflict")

    def _acquire_execution(
        self, row: McpWorkspaceExecutionOperationRow, digest: str
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            mutation = ManagedInstanceMutationLeaseRepository(session).claim(
                UUID(row.target_id),
                operation_id=UUID(row.id),
                token_digest=digest,
                now=now,
                expires_at=now + OPERATION_LEASE,
            )
            changed = session.execute(
                update(McpWorkspaceExecutionOperationRow)
                .where(
                    McpWorkspaceExecutionOperationRow.id == row.id,
                    McpWorkspaceExecutionOperationRow.state == "pending",
                )
                .values(state="running", phase="preflight", started_at=_to_text(now))
            )
            if not (mutation and int(getattr(changed, "rowcount", 0) or 0) == 1):
                raise McpWorkspaceDevelopmentError("deployment_conflict")

    def _run_deployment(
        self, operation_id: UUID, digest: str
    ) -> WorkspaceDeploymentResult:
        operation = self._deployment_required(operation_id)
        self._failure("claim_before_target_mutation")
        self._recheck_deployment(operation)
        container_id = self._exact_running_container(UUID(operation.target_id))
        self._update_deployment(operation_id, phase="stop", mutation_started=True)
        try:
            self._broker.stop_managed_container(
                managed_instance_id=operation.target_id,
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
        except Exception as error:
            self._finish_deployment(
                operation.id,
                "reconciliation_required",
                "deployment_ambiguous",
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError("deployment_ambiguous") from error
        self._update_deployment(operation_id, target_stopped=True)
        self._failure("after_stop")
        self._update_deployment(operation_id, phase="sync", sync_started=True)
        try:
            result = self._broker.copy_sync_mcp_workspace(
                managed_instance_id=operation.target_id,
                workspace_id=operation.workspace_id,
                expected_generation=operation.generation,
                expected_container_id=container_id,
                deployment_operation_id=operation.id,
            )
        except ClientError as error:
            code = getattr(error, "code", "deployment_ambiguous")
            uncertain = code in {
                "development_source_reconciliation_required",
                "development_source_apply_unverified",
                "development_destination_verification_failed",
            }
            self._finish_deployment(
                operation.id,
                "reconciliation_required" if uncertain else "failed",
                "deployment_ambiguous" if uncertain else code,
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError(
                "deployment_ambiguous" if uncertain else code
            ) from error
        self._publish_sync(operation_id, result)
        self._failure("after_sync_before_readiness")
        self._update_deployment(operation_id, phase="start")
        try:
            self._broker.start_managed_container(
                managed_instance_id=operation.target_id,
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            self._update_deployment(
                operation_id, target_started=True, phase="readiness"
            )
            with self._sessions() as session:
                instance = ManagedInstanceRepository(session).get(
                    UUID(operation.target_id)
                )
                profile = (
                    None
                    if instance is None
                    else RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                )
            if profile is None:
                raise RuntimeError("runtime profile unavailable")
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(operation.target_id)
            if getattr(readiness.usability_state, "value", "") != "usable":
                raise RuntimeError("target readiness failed")
        except Exception as error:
            self._contain_stopped(operation, container_id)
            self._finish_deployment(
                operation.id,
                "reconciliation_required",
                "deployment_ambiguous",
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError("deployment_ambiguous") from error
        self._failure("after_readiness_before_evidence")
        self._update_deployment(operation_id, phase="inventory")
        inventory = self._inventory.get(UUID(operation.target_id))
        item = next(
            (
                entry
                for entry in inventory.items
                if entry.addon_id == operation.addon_id
            ),
            None,
        )
        matched = (
            inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
            and item is not None
            and item.version == operation.addon_version
            and item.enabled
        )
        if not matched:
            contained = self._contain_stopped(operation, container_id)
            self._update_deployment(
                operation_id,
                inventory_classification="failed"
                if inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
                else "unknown",
            )
            self._finish_deployment(
                operation.id,
                "failed" if contained else "reconciliation_required",
                "deployment_verification_failed"
                if contained
                else "deployment_ambiguous",
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError(
                "deployment_verification_failed"
                if contained
                else "deployment_ambiguous"
            )
        assert item is not None
        self._update_deployment(
            operation_id,
            inventory_classification="matched",
            addon_enabled=item.enabled,
            result_classification=result.result_classification,
        )
        self._finish_deployment(operation.id, "succeeded", None, digest=digest)
        return self._deployment_result(self._deployment_required(operation_id))

    def _run_execution(
        self, operation_id: UUID, digest: str
    ) -> WorkspaceExecutionResult:
        operation = self._execution_required(operation_id)
        deployment = self._execution_deployment(operation)
        self._recheck_execution(operation, deployment)
        observation, profile, container_id = self._runtime_context(
            UUID(operation.target_id)
        )
        descriptor = self._workspace_descriptor(UUID(operation.workspace_id))
        if copied_video_plugin_library(descriptor) is None:
            self._finish_execution(
                operation.id, "failed", "execution_unsupported", digest=digest
            )
            raise McpWorkspaceDevelopmentError("execution_unsupported")
        if profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS:
            self._finish_execution(
                operation.id, "failed", "execution_unsupported", digest=digest
            )
            raise McpWorkspaceDevelopmentError("execution_unsupported")
        try:
            inventory = self._inventory_factory(observation, profile).get_inventory()
            by_id = {item.addon_id: item for item in inventory.items}
            recognized = by_id.get(operation.addon_id)
            if (
                recognized is None
                or recognized.version != operation.addon_version
                or not recognized.enabled
                or recognized.kodi_type != "xbmc.python.pluginsource"
            ):
                raise McpWorkspaceDevelopmentError("execution_not_deployed")
            details = self._details_factory(observation, profile).get_addon_details(
                operation.addon_id
            )
            if (
                details.version != operation.addon_version
                or not details.enabled
                or details.kodi_type != "xbmc.python.pluginsource"
            ):
                raise McpWorkspaceDevelopmentError("execution_not_deployed")
            mismatch = DevelopmentSourceEnablementService._declarations_match(
                descriptor.dependencies, details.dependencies
            )
            dependencies = DevelopmentSourceEnablementService._preflight_dependencies(
                descriptor.dependencies, by_id
            )
            if mismatch is not None or any(
                not item.optional
                and item.status
                in {
                    DependencyStatus.MISSING,
                    DependencyStatus.DISABLED,
                    DependencyStatus.VERSION_UNVERIFIED,
                    DependencyStatus.UNKNOWN,
                }
                for item in dependencies
            ):
                raise McpWorkspaceDevelopmentError("execution_unsupported")
        except McpWorkspaceDevelopmentError:
            raise
        except (
            JsonRpcUnavailableError,
            KodiAddonDetailsUnsupportedResponseError,
            KodiAddonInventoryIdentityError,
            KodiAddonInventoryLimitExceededError,
            KodiAddonInventoryMalformedDataError,
            KodiAddonInventoryUnsupportedResponseError,
            ValueError,
        ) as error:
            self._finish_execution(
                operation.id, "failed", "execution_not_deployed", digest=digest
            )
            raise McpWorkspaceDevelopmentError("execution_not_deployed") from error
        self._update_execution(operation_id, phase="verify_pre")
        verified = self._verify_workspace_destination(operation, container_id)
        if not verified.video_plugin_descriptor_eligible:
            self._finish_execution(
                operation.id, "failed", "execution_unsupported", digest=digest
            )
            raise McpWorkspaceDevelopmentError("execution_unsupported")
        self._update_execution(operation_id, phase="invoke", invocation_dispatched=True)
        self._failure("execution_dispatch_marker")
        invocation = None
        execution_failure: KodiAddonSmokeJsonRpcError | None = None
        try:
            invocation = self._smoke_factory(observation, profile).invoke(
                operation.addon_id
            )
        except KodiAddonSmokeJsonRpcError as error:
            execution_failure = error
        except (KodiAddonSmokeTimeoutError, KodiAddonSmokeAmbiguousError) as error:
            self._finish_execution(
                operation.id,
                "ambiguous",
                "execution_ambiguous",
                digest=digest,
                execution_classification=(
                    "timeout"
                    if isinstance(error, KodiAddonSmokeTimeoutError)
                    else "ambiguous"
                ),
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
        self._update_execution(operation_id, phase="verify_post")
        try:
            self._verify_workspace_destination(operation, container_id)
        except McpWorkspaceDevelopmentError as error:
            self._finish_execution(
                operation.id,
                "reconciliation_required",
                "execution_ambiguous",
                digest=digest,
                execution_classification="ambiguous",
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
        if execution_failure is not None:
            self._finish_execution(
                operation.id,
                "failed",
                "execution_failed",
                digest=digest,
                execution_classification="jsonrpc_error",
                result_classification="smoke_failed",
            )
            raise McpWorkspaceDevelopmentError(
                "execution_failed"
            ) from execution_failure
        assert invocation is not None
        self._finish_execution(
            operation.id,
            "succeeded",
            None,
            digest=digest,
            execution_classification="directory_result",
            item_count=invocation.item_count,
            result_classification="smoke_passed",
        )
        return self._execution_result(self._execution_required(operation_id))

    def _recheck_deployment(
        self, operation: McpWorkspaceDeploymentOperationRow
    ) -> None:
        with self._sessions() as session:
            self._deployment_preflight_rows(
                session,
                UUID(operation.workspace_id),
                operation.generation,
                UUID(operation.target_id),
            )

    def _recheck_execution(
        self,
        operation: McpWorkspaceExecutionOperationRow,
        deployment: McpWorkspaceDeploymentOperationRow,
    ) -> None:
        with self._sessions() as session:
            workspace = AgentWorkspaceRepository(session).get(
                UUID(operation.workspace_id)
            )
            instance = ManagedInstanceRepository(session).get(UUID(operation.target_id))
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                UUID(operation.target_id), UUID(operation.development_source_id)
            )
        if (
            workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.generation != operation.generation
            or str(workspace.development_source_id) != operation.development_source_id
            or instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or attachment is None
            or deployment.state != "succeeded"
            or deployment.workspace_id != operation.workspace_id
            or deployment.target_id != operation.target_id
            or deployment.generation != operation.generation
            or deployment.addon_id != operation.addon_id
            or deployment.addon_version != operation.addon_version
            or not deployment.addon_enabled
        ):
            raise McpWorkspaceDevelopmentError("execution_not_deployed")

    def _runtime_context(
        self, target_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile, str]:
        with self._sessions() as session:
            instance = ManagedInstanceRepository(session).get(target_id)
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if instance is None or profile is None:
            raise McpWorkspaceDevelopmentError("target_not_found_or_not_authorized")
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(target_id)
        ]
        if (
            len(matches) != 1
            or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            or matches[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise McpWorkspaceDevelopmentError("target_not_ready")
        container_id = matches[0].container_id
        observation = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=str(target_id), expected_container_id=container_id
        )
        if (
            observation.runtime_revision_id != str(instance.runtime_revision_id)
            or observation.container_id != container_id
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            raise McpWorkspaceDevelopmentError("target_not_ready")
        return observation, profile, container_id

    def _workspace_descriptor(self, workspace_id: UUID) -> Any:
        try:
            read = self._workspaces.read_file(workspace_id, "addon.xml")
            return parse_captured_addon_xml("workspace", read.content.encode("utf-8"))
        except (OSError, DevelopmentSourceValidationError, RuntimeError) as error:
            raise McpWorkspaceDevelopmentError("execution_unsupported") from error

    def _verify_workspace_destination(
        self, operation: McpWorkspaceExecutionOperationRow, container_id: str
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        try:
            result = self._broker.verify_mcp_workspace_destination(
                deployment_operation_id=operation.deployment_id,
                managed_instance_id=operation.target_id,
                expected_container_id=container_id,
            )
        except ClientError as error:
            raise McpWorkspaceDevelopmentError("execution_not_deployed") from error
        if (
            not result.matched
            or result.addon_id != operation.addon_id
            or result.addon_version != operation.addon_version
            or result.container_id != container_id
        ):
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        return result

    def _exact_running_container(self, target_id: UUID) -> str:
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(target_id)
        ]
        if (
            len(matches) != 1
            or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            or matches[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise McpWorkspaceDevelopmentError("target_not_ready")
        return matches[0].container_id

    def _contain_stopped(
        self, operation: McpWorkspaceDeploymentOperationRow, container_id: str
    ) -> bool:
        try:
            self._broker.stop_managed_container(
                managed_instance_id=operation.target_id,
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
            return True
        except Exception:
            return False

    def _contain_current_deployment(
        self, operation: McpWorkspaceDeploymentOperationRow
    ) -> bool:
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == operation.target_id
            ]
            if len(matches) != 1:
                return False
            if matches[0].runtime_state is ContainerRuntimeState.STOPPED:
                return True
            if matches[0].runtime_state is not ContainerRuntimeState.RUNNING:
                return False
            return self._contain_stopped(operation, matches[0].container_id)
        except Exception:
            return False

    def _publish_sync(
        self, operation_id: UUID, result: CopySyncAttachedDevelopmentSourceResult
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            if row is None or row.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            row.sync_completed = True
            row.source_manifest_digest = result.source_manifest_digest
            row.verified_destination_digest = result.verified_destination_digest
            row.result_classification = result.result_classification
            DevelopmentSourceDeploymentRepository(session).mark_confidence(
                UUID(row.target_id),
                UUID(row.development_source_id),
                DevelopmentSourceDeploymentConfidence.STALE,
            )

    def _update_deployment(self, operation_id: UUID, **values: object) -> None:
        with self._sessions.begin() as session:
            result = session.execute(
                update(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.id == str(operation_id),
                    McpWorkspaceDeploymentOperationRow.state == "running",
                )
                .values(**values)
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")

    def _update_execution(self, operation_id: UUID, **values: object) -> None:
        with self._sessions.begin() as session:
            result = session.execute(
                update(McpWorkspaceExecutionOperationRow)
                .where(
                    McpWorkspaceExecutionOperationRow.id == str(operation_id),
                    McpWorkspaceExecutionOperationRow.state == "running",
                )
                .values(**values)
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                raise McpWorkspaceDevelopmentError("execution_ambiguous")

    def _finish_deployment(
        self,
        operation_id: str,
        state: str,
        failure_code: str | None,
        *,
        digest: str | None = None,
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, operation_id)
            if row is None or row.state not in {"pending", "running"}:
                return
            row.state = state
            row.phase = "finalize"
            row.failure_code = failure_code
            row.completed_at = _to_text(now)
            if row.started_at is None:
                row.started_at = _to_text(now)
            if (
                state == "reconciliation_required"
                and row.sync_started
                and not row.sync_completed
            ):
                DevelopmentSourceDeploymentRepository(session).mark_confidence(
                    UUID(row.target_id),
                    UUID(row.development_source_id),
                    DevelopmentSourceDeploymentConfidence.UNKNOWN,
                )
            mutation = ManagedInstanceMutationLeaseRepository(session)
            heavy = GlobalHeavyOperationLeaseRepository(session)
            if digest is None:
                mutation.release_for_operation(UUID(row.target_id), UUID(row.id))
                heavy.release_for_operation(UUID(row.id))
            else:
                mutation.release(
                    UUID(row.target_id), operation_id=UUID(row.id), token_digest=digest
                )
                heavy.release(operation_id=UUID(row.id), token_digest=digest)

    def _finish_execution(
        self,
        operation_id: str,
        state: str,
        failure_code: str | None,
        *,
        digest: str | None = None,
        execution_classification: str | None = None,
        item_count: int | None = None,
        result_classification: str | None = None,
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceExecutionOperationRow, operation_id)
            if row is None or row.state not in {"pending", "running"}:
                return
            row.state = state
            row.phase = "finalize"
            row.failure_code = failure_code
            row.completed_at = _to_text(now)
            if row.started_at is None:
                row.started_at = _to_text(now)
            if execution_classification is not None:
                row.execution_classification = execution_classification
            row.item_count = item_count
            row.result_classification = result_classification
            leases = ManagedInstanceMutationLeaseRepository(session)
            if digest is None:
                leases.release_for_operation(UUID(row.target_id), UUID(row.id))
            else:
                leases.release(
                    UUID(row.target_id), operation_id=UUID(row.id), token_digest=digest
                )

    def _replay_deployment(
        self,
        row: McpWorkspaceDeploymentOperationRow,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
    ) -> WorkspaceDeploymentResult:
        if (
            row.workspace_id != str(workspace_id)
            or row.generation != generation
            or row.target_id != str(target_id)
        ):
            raise McpWorkspaceDevelopmentError("request_id_conflict")
        if row.state == "succeeded":
            return self._deployment_result(row)
        if row.state == "failed" and row.failure_code:
            raise McpWorkspaceDevelopmentError(row.failure_code)
        raise McpWorkspaceDevelopmentError("deployment_ambiguous")

    def _replay_execution(
        self,
        row: McpWorkspaceExecutionOperationRow,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
    ) -> WorkspaceExecutionResult:
        if (
            row.workspace_id != str(workspace_id)
            or row.generation != generation
            or row.target_id != str(target_id)
        ):
            raise McpWorkspaceDevelopmentError("request_id_conflict")
        if row.state == "succeeded":
            return self._execution_result(row)
        if row.state == "failed" and row.failure_code:
            raise McpWorkspaceDevelopmentError(row.failure_code)
        raise McpWorkspaceDevelopmentError("execution_ambiguous")

    def _deployment_result(
        self, row: McpWorkspaceDeploymentOperationRow
    ) -> WorkspaceDeploymentResult:
        started = _from_text(row.started_at)
        completed = _from_text(row.completed_at)
        if started is None or completed is None or row.addon_enabled is None:
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        return WorkspaceDeploymentResult(
            request_id=UUID(row.request_id),
            deployment_id=UUID(row.id),
            workspace_id=UUID(row.workspace_id),
            source_id=UUID(row.development_source_id),
            target_id=UUID(row.target_id),
            generation=row.generation,
            addon_id=row.addon_id,
            addon_version=row.addon_version,
            state=row.state,
            result_classification=row.result_classification or "verified",
            inventory_classification=row.inventory_classification,
            addon_enabled=row.addon_enabled,
            started_at=started,
            completed_at=completed,
        )

    def _execution_result(
        self, row: McpWorkspaceExecutionOperationRow
    ) -> WorkspaceExecutionResult:
        started = _from_text(row.started_at)
        completed = _from_text(row.completed_at)
        if started is None or completed is None:
            raise McpWorkspaceDevelopmentError("execution_ambiguous")
        return WorkspaceExecutionResult(
            request_id=UUID(row.request_id),
            execution_id=UUID(row.id),
            deployment_id=UUID(row.deployment_id),
            workspace_id=UUID(row.workspace_id),
            source_id=UUID(row.development_source_id),
            target_id=UUID(row.target_id),
            generation=row.generation,
            addon_id=row.addon_id,
            addon_version=row.addon_version,
            state=row.state,
            execution_classification=row.execution_classification,
            result_classification=row.result_classification or "smoke_passed",
            item_count=row.item_count,
            started_at=started,
            completed_at=completed,
        )

    def _deployment_required(
        self, operation_id: UUID
    ) -> McpWorkspaceDeploymentOperationRow:
        row = self._deployment(operation_id)
        if row is None:
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        return row

    def _execution_required(
        self, operation_id: UUID
    ) -> McpWorkspaceExecutionOperationRow:
        row = self._execution(operation_id)
        if row is None:
            raise McpWorkspaceDevelopmentError("execution_ambiguous")
        return row

    def _deployment(
        self, operation_id: UUID
    ) -> McpWorkspaceDeploymentOperationRow | None:
        with self._sessions() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            if row is not None:
                session.expunge(row)
            return row

    def _execution(
        self, operation_id: UUID
    ) -> McpWorkspaceExecutionOperationRow | None:
        with self._sessions() as session:
            row = session.get(McpWorkspaceExecutionOperationRow, str(operation_id))
            if row is not None:
                session.expunge(row)
            return row

    def _execution_deployment(
        self, execution: McpWorkspaceExecutionOperationRow
    ) -> McpWorkspaceDeploymentOperationRow:
        deployment = self._deployment(UUID(execution.deployment_id))
        if deployment is None:
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        return deployment

    def _existing_deployment(
        self, client_id: UUID, request_id: UUID
    ) -> McpWorkspaceDeploymentOperationRow | None:
        with self._sessions() as session:
            request = self._request(
                session, client_id, request_id, DEPLOY_OPERATION_KIND
            )
            row = (
                None
                if request is None or request.operation_id is None
                else session.get(
                    McpWorkspaceDeploymentOperationRow, request.operation_id
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def _existing_execution(
        self, client_id: UUID, request_id: UUID
    ) -> McpWorkspaceExecutionOperationRow | None:
        with self._sessions() as session:
            request = self._request(
                session, client_id, request_id, EXECUTE_OPERATION_KIND
            )
            row = (
                None
                if request is None or request.operation_id is None
                else session.get(
                    McpWorkspaceExecutionOperationRow, request.operation_id
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _request(
        session: Session, client_id: UUID, request_id: UUID, kind: str
    ) -> McpDevelopmentRequestRow | None:
        return session.scalar(
            select(McpDevelopmentRequestRow).where(
                McpDevelopmentRequestRow.client_id == str(client_id),
                McpDevelopmentRequestRow.request_id == str(request_id),
                McpDevelopmentRequestRow.operation_kind == kind,
            )
        )

    def _prune_and_require_capacity(
        self, session: Session, client_id: UUID, now: datetime
    ) -> None:
        prune_expired_safe_development_requests(
            session, now=now, retention=self._retention
        )
        cutoff = _to_text(now - self._retention)
        execution_request_reference = exists().where(
            McpDevelopmentRequestRow.operation_id
            == McpWorkspaceExecutionOperationRow.id
        )
        execution_promotion_reference = exists().where(
            AgentWorkspacePromotionPlanRow.originating_mcp_execution_id
            == McpWorkspaceExecutionOperationRow.id
        )
        expired_execution_ids = (
            select(McpWorkspaceExecutionOperationRow.id)
            .where(
                McpWorkspaceExecutionOperationRow.completed_at < cutoff,
                execution_terminal_evidence_is_consistent_sql(),
                ~execution_request_reference,
                ~execution_promotion_reference,
            )
            .order_by(
                McpWorkspaceExecutionOperationRow.completed_at,
                McpWorkspaceExecutionOperationRow.id,
            )
            .limit(DEVELOPMENT_OPERATION_PRUNE_LIMIT)
        )
        session.execute(
            delete(McpWorkspaceExecutionOperationRow).where(
                McpWorkspaceExecutionOperationRow.id.in_(expired_execution_ids)
            )
        )
        current_workspace_deployment = (
            select(AgentWorkspaceRow.id)
            .where(
                AgentWorkspaceRow.id == McpWorkspaceDeploymentOperationRow.workspace_id,
                AgentWorkspaceRow.generation
                == McpWorkspaceDeploymentOperationRow.generation,
                AgentWorkspaceRow.bytes_state == AgentWorkspaceBytesState.PRESENT,
                McpWorkspaceDeploymentOperationRow.state == "succeeded",
            )
            .exists()
        )
        deployment_request_reference = exists().where(
            McpDevelopmentRequestRow.operation_id
            == McpWorkspaceDeploymentOperationRow.id
        )
        deployment_promotion_reference = exists().where(
            AgentWorkspacePromotionPlanRow.originating_mcp_deployment_id
            == McpWorkspaceDeploymentOperationRow.id
        )
        expired_deployment_ids = (
            select(McpWorkspaceDeploymentOperationRow.id)
            .where(
                McpWorkspaceDeploymentOperationRow.completed_at < cutoff,
                deployment_terminal_evidence_is_consistent_sql(),
                ~current_workspace_deployment,
                ~deployment_request_reference,
                ~deployment_promotion_reference,
            )
            .order_by(
                McpWorkspaceDeploymentOperationRow.completed_at,
                McpWorkspaceDeploymentOperationRow.id,
            )
            .limit(DEVELOPMENT_OPERATION_PRUNE_LIMIT)
        )
        session.execute(
            delete(McpWorkspaceDeploymentOperationRow).where(
                McpWorkspaceDeploymentOperationRow.id.in_(expired_deployment_ids)
            )
        )
        client_count = session.scalar(
            select(func.count())
            .select_from(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.client_id == str(client_id))
        )
        global_count = session.scalar(
            select(func.count()).select_from(McpDevelopmentRequestRow)
        )
        if (client_count or 0) >= self._max_per_client or (
            global_count or 0
        ) >= self._max_global:
            raise McpWorkspaceDevelopmentError("request_id_conflict")

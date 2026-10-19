"""Exact-generation MCP workspace deployment and fixed smoke execution."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_dependencies import (
    DependencyStatus,
    dependency_failure_code,
    first_blocking_dependency,
)
from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceEnablementService,
)
from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
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
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.workspace_execution_recovery import (
    WorkspaceExecutionRecoveryResult,
    WorkspaceExecutionRecoveryService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientError,
    ClientProtocolError,
    ClientTransportError,
)
from kodi_lab.broker.operations import (
    CaptureSmokeLogResult,
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ReadSmokeLogResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    parse_captured_addon_xml,
)
from kodi_lab.development.sources import (
    COPIED_SMOKE_UNSUPPORTED_REASONS,
    copied_video_plugin_unsupported_reason,
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
    TargetType,
)
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
    SmokeDiagnosticStatus,
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
    KodiJsonRpcDiagnostic,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError, KodiReadinessCoordinator
from kodi_lab.repository.development import (
    DevelopmentPublicationConflict,
    DevelopmentRepositoryService,
    publication_lock,
)
from kodi_lab.repository.development import (
    _version as repository_version,
)
from kodi_lab.repository.development import (
    bootstrap_zip as development_bootstrap_zip,
)
from kodi_lab.repository.development import (
    load_current as load_development_current,
)
from kodi_lab.repository.policy import DEVELOPMENT_REPOSITORY_ID
from kodi_lab.repository.stable import inspect_addon_zip, sha256
from kodi_lab.repository.workspace_package import (
    bridge_manifest_for_zip,
    build_workspace_package,
)
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    AgentWorkspacePromotionPlanRow,
    AgentWorkspaceRow,
    DevelopmentPublicationHoldRow,
    DevelopmentPublicationRow,
    DevelopmentRepositoryStateRow,
    ExternalBridgeDeploymentReceiptRow,
    ExternalBridgeEventRow,
    ExternalBridgePairingRow,
    ExternalDevelopmentRepositoryReadinessRow,
    ExternalDevelopmentSourceBaselineRow,
    ExternalKodiTargetRow,
    ManagedInstanceRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
    McpWorkspaceValidationRow,
    RepositoryWorkspaceDeploymentRow,
    RepositoryWorkspaceValidationSnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    ManagedRuntimeProjectionRepository,
    ManagedRuntimeReconciliationBlockedError,
    RuntimeExecutionProfileRepository,
)
from kodi_lab.workspace import (
    AgentWorkspaceStorageError,
    capture_workspace_deployment_tree,
    workspace_coordination_lock,
)

OPERATION_LEASE = timedelta(minutes=15)
DEVELOPMENT_OPERATION_PRUNE_LIMIT = 256


class WorkspaceExecutionKind(StrEnum):
    SMOKE = "smoke"
    KODI_RPC_NEGATIVE_DIAGNOSTIC = "kodi_rpc_negative_diagnostic"


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
    kind: str,
    workspace_id: UUID,
    generation: int,
    target_id: UUID,
    deployment_mode: str = "direct",
    execution_kind: WorkspaceExecutionKind = WorkspaceExecutionKind.SMOKE,
) -> str:
    value = f"{kind}\x00{workspace_id}\x00{generation}\x00{target_id}"
    if kind == DEPLOY_OPERATION_KIND and deployment_mode != "direct":
        value += f"\x00{deployment_mode}"
    if (
        kind == EXECUTE_OPERATION_KIND
        and execution_kind is not WorkspaceExecutionKind.SMOKE
    ):
        value += f"\x00{execution_kind.value}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _stable_adoption_identity_digest(
    target: TargetRow,
    external: ExternalKodiTargetRow,
    pairing: ExternalBridgePairingRow,
    readiness: ExternalDevelopmentRepositoryReadinessRow,
) -> str:
    """Bind native and paired identities, excluding reachability/observation clocks.

    Secret references are identities; credential bytes are never included. Native
    re-verification, pairing, endpoint, and repository confirmation are stable
    revisions of the proof, while last-observed/reachability fields are transient.
    """

    identity = {
        "target": (
            target.id,
            target.name,
            target.target_type.value,
            target.kodi_version,
        ),
        "native": (
            external.target_id,
            external.protocol_kind.value,
            external.host,
            external.port,
            external.authentication_mode.value,
            external.secret_reference,
            external.configured_capabilities_json,
            external.verified_capabilities_json,
            external.administrative_state.value,
            external.verification_state.value,
            external.verified_addresses_json,
            external.verified_kodi_name,
            external.verified_kodi_version,
            external.verified_at,
        ),
        "bridge": (
            pairing.target_id,
            pairing.bridge_port,
            pairing.pairing_state.value,
            pairing.bridge_secret_reference,
            pairing.pinned_bridge_id,
            pairing.pinned_protocol_version,
            pairing.pinned_addon_id,
            pairing.pinned_addon_version,
            pairing.pinned_kodi_version,
            pairing.pinned_build_fingerprint,
            pairing.verified_capabilities_json,
            pairing.endpoint_revision_digest,
            pairing.paired_at,
            pairing.verified_at,
        ),
        "repository_readiness": (
            readiness.target_id,
            readiness.base_url,
            readiness.bootstrap_sha256,
            readiness.native_kodi_version,
            readiness.bridge_id,
            readiness.repository_version,
            readiness.observed_manifest_digest,
            readiness.manual_updates_confirmed,
            readiness.confirmed_by,
            readiness.confirmed_at,
        ),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _matches_existing_adoption(
    operation: McpWorkspaceDeploymentOperationRow,
    linked: RepositoryWorkspaceDeploymentRow | None,
    publication: DevelopmentPublicationRow,
    workspace: AgentWorkspaceRow,
    target_id: UUID,
    kodi_version: str,
    bridge_id: str,
    base_url: str,
    bootstrap_sha256: str,
    identity_sha256: str,
    digest: str,
    file_count: int,
    total_bytes: int,
) -> bool:
    """Require every durable verification claim to match before reusing success."""

    return bool(
        linked is not None
        and operation.initiator_kind == "web"
        and operation.client_id is None
        and operation.repository_origin == "verified_existing"
        and operation.deployment_mode == "repository"
        and operation.target_class == "external_development"
        and operation.target_id == str(target_id)
        and operation.workspace_id == publication.workspace_id
        and operation.development_source_id == workspace.development_source_id
        and operation.generation == publication.workspace_generation
        and operation.addon_id == publication.addon_id
        and operation.addon_version == publication.version
        and operation.bridge_id == bridge_id
        and operation.file_count == file_count
        and operation.total_bytes == total_bytes
        and operation.state == "succeeded"
        and operation.phase == "finalize"
        and not operation.mutation_started
        and not operation.target_stopped
        and not operation.sync_started
        and not operation.sync_completed
        and not operation.target_started
        and operation.inventory_classification == "matched"
        and operation.addon_enabled is True
        and operation.result_classification == "repository_verified"
        and operation.source_manifest_digest == digest
        and operation.verified_destination_digest == digest
        and operation.failure_code is None
        and linked.operation_id == operation.id
        and linked.origin == "verified_existing"
        and linked.verified_by == "local_admin"
        and linked.identity_sha256 == identity_sha256
        and linked.publication_source_id == publication.source_id
        and linked.state == "succeeded"
        and linked.approved_at is None
        and linked.observed_at is not None
        and linked.verified_at is not None
        and linked.failure_code is None
        and linked.publication_id == publication.id
        and linked.workspace_id == publication.workspace_id
        and linked.workspace_generation == publication.workspace_generation
        and linked.target_id == str(target_id)
        and linked.target_kodi_version == kodi_version
        and linked.bridge_id == bridge_id
        and linked.addon_id == publication.addon_id
        and linked.addon_version == publication.version
        and linked.zip_sha256 == publication.zip_sha256
        and linked.package_tree_sha256 == publication.tree_sha256
        and linked.expected_manifest_digest == digest
        and linked.file_count == file_count
        and linked.total_bytes == total_bytes
        and linked.validation_receipt == publication.validation_receipt
        and linked.repository_id == DEVELOPMENT_REPOSITORY_ID
        and linked.base_url == base_url
        and linked.bootstrap_sha256 == bootstrap_sha256
        and linked.installed_before_version == publication.version
        and linked.published_at == publication.published_at
    )


class McpWorkspaceDevelopmentError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        reason: str | None = None,
        diagnostic: KodiJsonRpcDiagnostic | None = None,
        addon_exception: AddonExceptionDiagnostic | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.diagnostic = diagnostic
        self.addon_exception = addon_exception
        super().__init__(code)


EXECUTION_DEPENDENCY_REASONS = frozenset(
    {
        "dependency_declaration_mismatch",
        *(
            dependency_failure_code(status)
            for status in (
                DependencyStatus.MISSING,
                DependencyStatus.DISABLED,
                DependencyStatus.INCOMPATIBLE,
                DependencyStatus.VERSION_UNVERIFIED,
                DependencyStatus.UNKNOWN,
            )
        ),
    }
)

EXECUTION_UNSUPPORTED_REASONS = (
    EXECUTION_DEPENDENCY_REASONS | COPIED_SMOKE_UNSUPPORTED_REASONS
)


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
    deployment_mode: str = Field(default="direct", max_length=16)
    phase: str = Field(default="finalize", max_length=32)
    publication_id: UUID | None = None
    repository_id: str | None = Field(default=None, max_length=64)
    installation_kind: str | None = Field(default=None, max_length=16)
    origin: str | None = Field(default=None, max_length=24)
    zip_sha256: str | None = None
    expected_manifest_digest: str | None = None
    next_action: str | None = Field(default=None, max_length=160)
    result_classification: str = Field(max_length=64)
    inventory_classification: str = Field(max_length=16)
    addon_enabled: bool | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    reconciliation_required: bool = False


WORKSPACE_SMOKE_SUCCESS_DESCRIPTION = (
    "smoke_passed confirms only that the bounded launch/smoke invocation succeeded. "
    "It is not functional acceptance or proof of task completion: UI navigation, "
    "filtering/results, and other feature behavior remain unverified. "
    "Test those behaviors separately when the task depends on them."
)


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
    result_classification: str = Field(
        max_length=64, description=WORKSPACE_SMOKE_SUCCESS_DESCRIPTION
    )
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
    deployment_mode: str | None = None
    deployment_phase: str | None = None
    deployment_publication_id: UUID | None = None
    deployment_installation_kind: str | None = Field(default=None, max_length=16)
    deployment_next_action: str | None = Field(default=None, max_length=160)
    deployment_generation: int | None = Field(default=None, ge=0)
    deployment_target_id: UUID | None = None
    execution_id: UUID | None = None
    execution_state: str | None = None
    execution_generation: int | None = Field(default=None, ge=0)
    execution_target_id: UUID | None = None
    launch_id: UUID | None = None
    launch_request_id: UUID | None = None
    launch_state: str | None = None
    launch_generation: int | None = None
    launch_target_id: UUID | None = None
    launch_extension_ordinal: int | None = None


class ExternalBaselineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    source_id: UUID
    bridge_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    manifest_digest: str = Field(min_length=71, max_length=71)
    file_count: int = Field(ge=1, le=25_000)
    total_bytes: int = Field(ge=1, le=1024 * 1024 * 1024)
    state: str = "current"
    established_at: datetime


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

    def capture_smoke_log_cursor(
        self, *, execution_id: str
    ) -> CaptureSmokeLogResult: ...

    def read_smoke_log_window(
        self, *, execution_id: str, cursor: str
    ) -> ReadSmokeLogResult: ...


class _TargetAdapters(Protocol):
    def addon_inventory(self, target_id: UUID) -> Any: ...

    def fixed_execute(
        self, target_id: UUID, addon_id: str, invocation_id: UUID
    ) -> Any: ...


class _DeploymentTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_class: str
    runtime_revision_id: UUID | None = None
    bridge_id: UUID | None = None


def _repository_feed(
    repository: DevelopmentRepositoryService,
    *,
    error_code: str = "repository_prerequisite_unconfirmed",
) -> dict[str, object]:
    try:
        return load_development_current(
            repository.root, expected_base_url=repository.base_url
        )[1]
    except (OSError, ValueError, KeyError) as error:
        raise McpWorkspaceDevelopmentError(error_code) from error


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
        readiness_registry: ReadinessTaskRegistry | None = None,
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
        external_bridges: ExternalBridgeService | None = None,
        target_adapters: _TargetAdapters | None = None,
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
        self._external_bridges = external_bridges
        self._target_adapters = target_adapters
        self._development_repository: DevelopmentRepositoryService | None = None
        self._recovery = WorkspaceExecutionRecoveryService(
            session_factory,
            broker,
            inventory,
            readiness_factory=readiness_factory,
            readiness_registry=readiness_registry,
            clock=clock,
        )

    def recovery_context(
        self, client_id: UUID, execution_operation_id: UUID
    ) -> tuple[UUID, UUID]:
        return self._recovery.context(client_id, execution_operation_id)

    def recover_execution(
        self, *, client_id: UUID, request_id: UUID, execution_operation_id: UUID
    ) -> WorkspaceExecutionRecoveryResult:
        return self._recovery.recover(
            client_id=client_id,
            request_id=request_id,
            execution_operation_id=execution_operation_id,
        )

    def configure_repository(self, repository: DevelopmentRepositoryService) -> None:
        self._development_repository = repository

    def confirm_development_repository(
        self,
        *,
        target_id: UUID,
        base_url: str,
        reachable_in_kodi: bool,
        manual_updates_confirmed: bool,
    ) -> ExternalDevelopmentRepositoryReadinessRow:
        """Web-only confirmation after observing the exact installed repository."""

        repository = self._development_repository
        bridges = self._external_bridges
        targets = self._target_adapters
        if (
            repository is None
            or bridges is None
            or targets is None
            or base_url != repository.base_url
            or reachable_in_kodi is not True
            or manual_updates_confirmed is not True
        ):
            raise McpWorkspaceDevelopmentError("repository_prerequisite_unconfirmed")
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            external = session.get(ExternalKodiTargetRow, str(target_id))
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
        if (
            target is None
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external is None
            or external.administrative_state.value != "enabled"
            or external.verification_state.value != "verified"
            or external.verified_kodi_version is None
            or pairing is None
            or pairing.pairing_state.value != "paired"
            or pairing.pinned_bridge_id is None
            or pairing.pinned_kodi_version != external.verified_kodi_version
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        feed = _repository_feed(repository)
        bootstrap_version = str(feed["repository_version"])
        bootstrap = development_bootstrap_zip(base_url, bootstrap_version)
        expected_digest, expected_files, expected_bytes = bridge_manifest_for_zip(
            bootstrap, DEVELOPMENT_REPOSITORY_ID
        )
        try:
            inventory = targets.addon_inventory(target_id)
            observed = bridges.inspect_addon(target_id, DEVELOPMENT_REPOSITORY_ID)
        except Exception as error:
            raise McpWorkspaceDevelopmentError(
                "repository_prerequisite_unconfirmed"
            ) from error
        item = next(
            (
                entry
                for entry in inventory.items
                if entry.addon_id == DEVELOPMENT_REPOSITORY_ID
            ),
            None,
        )
        if (
            inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
            or inventory.kodi_release != external.verified_kodi_version
            or item is None
            or item.version != bootstrap_version
            or not item.enabled
            or observed.addon_version != bootstrap_version
            or not observed.enabled
            or observed.manifest_digest != expected_digest
            or observed.file_count != expected_files
            or observed.total_bytes != expected_bytes
        ):
            raise McpWorkspaceDevelopmentError("repository_prerequisite_unconfirmed")
        now = _to_text(self._clock())
        with self._sessions.begin() as session:
            row = session.get(ExternalDevelopmentRepositoryReadinessRow, str(target_id))
            values = {
                "base_url": base_url,
                "bootstrap_sha256": sha256(bootstrap),
                "native_kodi_version": external.verified_kodi_version,
                "bridge_id": pairing.pinned_bridge_id,
                "repository_version": item.version,
                "observed_manifest_digest": observed.manifest_digest,
                "manual_updates_confirmed": True,
                "confirmed_by": "local_admin",
                "confirmed_at": now,
            }
            if row is None:
                row = ExternalDevelopmentRepositoryReadinessRow(
                    target_id=str(target_id), **values
                )
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            session.flush()
            session.expunge(row)
        return row

    def verify_existing_repository_installation(
        self,
        *,
        target_id: UUID,
        publication_id: UUID,
        addon_id: str,
        addon_version: str,
        zip_sha256: str,
        expected_manifest_digest: str,
    ) -> WorkspaceDeploymentResult:
        """Web-only proof of a previously installed, approved workspace artifact."""

        repository = self._development_repository
        targets = self._target_adapters
        bridges = self._external_bridges
        if repository is None or targets is None or bridges is None:
            raise McpWorkspaceDevelopmentError("repository_unavailable")
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            external = session.get(ExternalKodiTargetRow, str(target_id))
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
            readiness = session.get(
                ExternalDevelopmentRepositoryReadinessRow, str(target_id)
            )
            publication = session.get(DevelopmentPublicationRow, str(publication_id))
            workspace = (
                None
                if publication is None or publication.workspace_id is None
                else session.get(AgentWorkspaceRow, publication.workspace_id)
            )
            snapshot = (
                None
                if publication is None or publication.validation_receipt is None
                else session.get(
                    RepositoryWorkspaceValidationSnapshotRow,
                    publication.validation_receipt,
                )
            )
            repository_state = session.get(DevelopmentRepositoryStateRow, 1)
        feed = _repository_feed(repository, error_code="adoption_artifact_unverified")
        bootstrap_version = str(feed["repository_version"])
        if (
            target is None
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external is None
            or external.administrative_state.value != "enabled"
            or external.verification_state.value != "verified"
            or external.verified_kodi_version is None
            or pairing is None
            or pairing.pairing_state.value != "paired"
            or pairing.pinned_bridge_id is None
            or pairing.pinned_kodi_version != external.verified_kodi_version
            or pairing.bridge_secret_reference is None
            or readiness is None
            or readiness.base_url != repository.base_url
            or readiness.native_kodi_version != external.verified_kodi_version
            or readiness.bridge_id != pairing.pinned_bridge_id
            or readiness.repository_version != bootstrap_version
            or not readiness.manual_updates_confirmed
            or readiness.bootstrap_sha256 != str(feed["bootstrap_zip_sha256"])
        ):
            raise McpWorkspaceDevelopmentError("adoption_prerequisite_unconfirmed")
        try:
            configured = json.loads(external.configured_capabilities_json)
        except (TypeError, ValueError) as error:
            raise McpWorkspaceDevelopmentError(
                "adoption_capability_unavailable"
            ) from error
        if (
            not isinstance(configured, list)
            or not all(isinstance(value, str) for value in configured)
            or not {"target_inspect", "addon_inventory", "workspace_deployment"}
            <= set(configured)
        ):
            raise McpWorkspaceDevelopmentError("adoption_capability_unavailable")
        generation_name = {"19": "matrix", "20": "nexus", "21": "omega"}.get(
            external.verified_kodi_version.split(".", 1)[0]
        )
        if (
            publication is None
            or publication.channel != "development"
            or publication.scope_kind != "development_only"
            or publication.addon_type != "plugin.video"
            or publication.status != "current"
            or publication.approval_identity is None
            or publication.approved_at is None
            or publication.source_kind != "workspace"
            or publication.workspace_id is None
            or publication.workspace_generation is None
            or publication.validation_receipt is None
            or publication.source_tree_sha256 is None
            or generation_name is None
            or generation_name not in publication.compatible_generations.split(",")
            or workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.generation != publication.workspace_generation
            or workspace.current_tree_digest != publication.source_tree_sha256
            or snapshot is None
            or snapshot.workspace_id != publication.workspace_id
            or snapshot.generation != publication.workspace_generation
            or snapshot.source_tree_digest != publication.source_tree_sha256
            or snapshot.addon_id != publication.addon_id
            or snapshot.addon_version != publication.version
            or repository_state is None
            or repository_state.base_url != repository.base_url
        ):
            raise McpWorkspaceDevelopmentError("adoption_publication_ineligible")
        if (
            addon_id != publication.addon_id
            or addon_version != publication.version
            or zip_sha256 != publication.zip_sha256
        ):
            raise McpWorkspaceDevelopmentError("adoption_confirmation_stale")
        identity_sha256 = _stable_adoption_identity_digest(
            target, external, pairing, readiness
        )
        try:
            _, provenance = load_development_current(
                repository.root, expected_base_url=repository.base_url
            )
            if (
                repository_state.current_generation != provenance["tree_sha256"]
                or repository_state.current_index_sha256
                != provenance["addons_xml_sha256"]
                or repository_state.bootstrap_sha256
                != provenance["bootstrap_zip_sha256"]
            ):
                raise ValueError("development repository state differs")
            package_records = provenance["packages"]
            if not isinstance(package_records, list) or not any(
                isinstance(record, dict)
                and record.get("publication_id") == str(publication_id)
                and record.get("id") == publication.addon_id
                and record.get("version") == publication.version
                and record.get("sha256") == publication.zip_sha256
                and record.get("tree_sha256") == publication.tree_sha256
                and record.get("status") == "current"
                for record in package_records
            ):
                raise ValueError("publication is absent from current feed")
            package_path = (
                repository.root
                / "packages"
                / publication.addon_id
                / f"{publication.addon_id}-{publication.version}.zip"
            )
            package_bytes = package_path.read_bytes()
            package = inspect_addon_zip(
                package_bytes, publication.addon_id, publication.version
            )
            if (
                package.sha256 != publication.zip_sha256
                or package.tree_sha256 != publication.tree_sha256
                or len(package_bytes) != publication.zip_bytes
            ):
                raise ValueError("approved artifact differs")
            digest, file_count, total_bytes = bridge_manifest_for_zip(
                package_bytes, publication.addon_id
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise McpWorkspaceDevelopmentError(
                "adoption_artifact_unverified"
            ) from error
        if expected_manifest_digest != digest:
            raise McpWorkspaceDevelopmentError("adoption_confirmation_stale")
        try:
            inventory = targets.addon_inventory(target_id)
        except Exception as error:
            raise McpWorkspaceDevelopmentError("adoption_target_unverified") from error
        repository_item = next(
            (
                item
                for item in inventory.items
                if item.addon_id == DEVELOPMENT_REPOSITORY_ID
            ),
            None,
        )
        item = next(
            (item for item in inventory.items if item.addon_id == publication.addon_id),
            None,
        )
        if (
            inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
            or inventory.instance_id != target_id
            or inventory.kodi_release != external.verified_kodi_version
            or repository_item is None
            or repository_item.version != "1.0.0"
            or not repository_item.enabled
            or item is None
            or item.version != publication.version
            or not item.enabled
            or item.kodi_type != "xbmc.python.pluginsource"
        ):
            raise McpWorkspaceDevelopmentError("adoption_installed_state_mismatch")
        try:
            repository_tree = bridges.inspect_addon(
                target_id, DEVELOPMENT_REPOSITORY_ID
            )
            observed = bridges.inspect_addon(target_id, publication.addon_id)
        except Exception as error:
            raise McpWorkspaceDevelopmentError("adoption_target_unverified") from error
        if (
            repository_tree.addon_id != DEVELOPMENT_REPOSITORY_ID
            or repository_tree.addon_version != "1.0.0"
            or not repository_tree.enabled
            or repository_tree.manifest_digest != readiness.observed_manifest_digest
            or observed.addon_id != publication.addon_id
            or observed.addon_version != publication.version
            or not observed.enabled
            or observed.manifest_digest != digest
            or observed.file_count != file_count
            or observed.total_bytes != total_bytes
        ):
            raise McpWorkspaceDevelopmentError("adoption_installed_state_mismatch")
        now = _to_text(self._clock())
        operation_id = uuid4()
        with (
            workspace_coordination_lock(
                self._data_root, UUID(publication.workspace_id), exclusive=True
            ),
            publication_lock(repository.root),
            self._sessions() as session,
        ):
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _, current_provenance = load_development_current(
                    repository.root, expected_base_url=repository.base_url
                )
                if (
                    current_provenance != provenance
                    or sha256(package_path.read_bytes()) != publication.zip_sha256
                ):
                    raise ValueError("approved feed or artifact changed")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise McpWorkspaceDevelopmentError("adoption_state_changed") from error
            current_target = session.get(TargetRow, str(target_id))
            current_external = session.get(ExternalKodiTargetRow, str(target_id))
            current_pairing = session.get(ExternalBridgePairingRow, str(target_id))
            current_readiness = session.get(
                ExternalDevelopmentRepositoryReadinessRow, str(target_id)
            )
            current_publication = session.get(
                DevelopmentPublicationRow, str(publication_id)
            )
            current_workspace = session.get(AgentWorkspaceRow, publication.workspace_id)
            current_snapshot = session.get(
                RepositoryWorkspaceValidationSnapshotRow, publication.validation_receipt
            )
            current_repository_state = session.get(DevelopmentRepositoryStateRow, 1)
            if (
                current_target is None
                or current_external is None
                or current_pairing is None
                or current_readiness is None
                or _stable_adoption_identity_digest(
                    current_target,
                    current_external,
                    current_pairing,
                    current_readiness,
                )
                != identity_sha256
                or current_publication is None
                or current_publication.status != "current"
                or current_publication.channel != publication.channel
                or current_publication.addon_id != publication.addon_id
                or current_publication.version != publication.version
                or current_publication.scope_kind != publication.scope_kind
                or current_publication.addon_type != publication.addon_type
                or current_publication.source_kind != "workspace"
                or current_publication.source_id != publication.source_id
                or current_publication.compatible_generations
                != publication.compatible_generations
                or current_publication.intended_targets != publication.intended_targets
                or current_publication.approval_identity
                != publication.approval_identity
                or current_publication.approved_at != publication.approved_at
                or current_publication.workspace_id != publication.workspace_id
                or current_publication.workspace_generation
                != publication.workspace_generation
                or current_publication.validation_receipt
                != publication.validation_receipt
                or current_publication.source_tree_sha256
                != publication.source_tree_sha256
                or current_publication.zip_sha256 != publication.zip_sha256
                or current_publication.tree_sha256 != publication.tree_sha256
                or current_publication.zip_bytes != publication.zip_bytes
                or current_publication.feed_generation != publication.feed_generation
                or current_publication.index_sha256 != publication.index_sha256
                or current_workspace is None
                or current_workspace.state is not AgentWorkspaceState.READY
                or current_workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or current_workspace.generation != publication.workspace_generation
                or current_workspace.current_tree_digest
                != publication.source_tree_sha256
                or current_snapshot is None
                or current_snapshot.workspace_id != publication.workspace_id
                or current_snapshot.source_tree_digest != publication.source_tree_sha256
                or current_snapshot.generation != publication.workspace_generation
                or current_snapshot.addon_id != publication.addon_id
                or current_snapshot.addon_version != publication.version
                or current_repository_state is None
                or current_repository_state.base_url != repository.base_url
                or current_repository_state.bootstrap_sha256
                != repository_state.bootstrap_sha256
                or current_repository_state.current_generation
                != repository_state.current_generation
                or current_repository_state.current_index_sha256
                != repository_state.current_index_sha256
            ):
                raise McpWorkspaceDevelopmentError("adoption_state_changed")
            existing = list(
                session.scalars(
                    select(McpWorkspaceDeploymentOperationRow).where(
                        McpWorkspaceDeploymentOperationRow.target_id == str(target_id)
                    )
                )
            )
            repeat: McpWorkspaceDeploymentOperationRow | None = None
            for operation in existing:
                linked = session.get(RepositoryWorkspaceDeploymentRow, operation.id)
                if operation.state in {
                    "pending",
                    "running",
                    "ambiguous",
                    "reconciliation_required",
                }:
                    raise McpWorkspaceDevelopmentError("adoption_conflict")
                if operation.state == "succeeded" and (
                    operation.addon_id == publication.addon_id
                    or (linked is not None and linked.publication_id == publication.id)
                ):
                    if _matches_existing_adoption(
                        operation,
                        linked,
                        publication,
                        workspace,
                        target_id,
                        external.verified_kodi_version,
                        pairing.pinned_bridge_id,
                        repository.base_url,
                        readiness.bootstrap_sha256,
                        identity_sha256,
                        digest,
                        file_count,
                        total_bytes,
                    ):
                        repeat = operation
                    else:
                        raise McpWorkspaceDevelopmentError("adoption_conflict")
            if repeat is not None:
                session.commit()
                return self._deployment_result(
                    self._deployment_required(UUID(repeat.id))
                )
            session.add(
                McpWorkspaceDeploymentOperationRow(
                    id=str(operation_id),
                    client_id=None,
                    initiator_kind="web",
                    repository_origin="verified_existing",
                    request_id=str(uuid4()),
                    workspace_id=publication.workspace_id,
                    development_source_id=workspace.development_source_id,
                    target_id=str(target_id),
                    runtime_revision_id=None,
                    target_class="external_development",
                    deployment_mode="repository",
                    bridge_id=pairing.pinned_bridge_id,
                    bridge_request_id=None,
                    bridge_receipt_id=None,
                    file_count=file_count,
                    total_bytes=total_bytes,
                    generation=publication.workspace_generation,
                    addon_id=publication.addon_id,
                    addon_version=publication.version,
                    state="succeeded",
                    phase="finalize",
                    mutation_started=False,
                    target_stopped=False,
                    sync_started=False,
                    sync_completed=False,
                    target_started=False,
                    inventory_classification="matched",
                    addon_enabled=True,
                    result_classification="repository_verified",
                    source_manifest_digest=digest,
                    verified_destination_digest=digest,
                    failure_code=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )
            session.add(
                RepositoryWorkspaceDeploymentRow(
                    operation_id=str(operation_id),
                    publication_id=str(publication_id),
                    workspace_id=publication.workspace_id,
                    workspace_generation=publication.workspace_generation,
                    target_id=str(target_id),
                    target_kodi_version=external.verified_kodi_version,
                    bridge_id=pairing.pinned_bridge_id,
                    addon_id=publication.addon_id,
                    addon_version=publication.version,
                    zip_sha256=publication.zip_sha256,
                    package_tree_sha256=publication.tree_sha256,
                    expected_manifest_digest=digest,
                    file_count=file_count,
                    total_bytes=total_bytes,
                    validation_receipt=publication.validation_receipt,
                    repository_id=DEVELOPMENT_REPOSITORY_ID,
                    base_url=repository.base_url,
                    bootstrap_sha256=readiness.bootstrap_sha256,
                    installed_before_version=publication.version,
                    origin="verified_existing",
                    verified_by="local_admin",
                    identity_sha256=identity_sha256,
                    publication_source_id=publication.source_id,
                    state="succeeded",
                    created_at=now,
                    approved_at=None,
                    published_at=publication.published_at,
                    observed_at=now,
                    verified_at=now,
                    failure_code=None,
                )
            )
            session.commit()
        return self._deployment_result(self._deployment_required(operation_id))

    def approve_repository_deployment(
        self,
        operation_id: UUID,
        *,
        workspace_id: UUID,
        target_id: UUID,
        generation: int,
        addon_id: str,
        addon_version: str,
        zip_sha256: str,
        expected_manifest_digest: str,
    ) -> WorkspaceDeploymentResult:
        """Web-only, one-generation publication approval with exact values."""

        try:
            with workspace_coordination_lock(
                self._data_root, workspace_id, exclusive=True, blocking=False
            ):
                return self._approve_repository_deployment_locked(
                    operation_id,
                    workspace_id=workspace_id,
                    target_id=target_id,
                    generation=generation,
                    addon_id=addon_id,
                    addon_version=addon_version,
                    zip_sha256=zip_sha256,
                    expected_manifest_digest=expected_manifest_digest,
                )
        except AgentWorkspaceStorageError as error:
            raise McpWorkspaceDevelopmentError("workspace_busy") from error

    def _approve_repository_deployment_locked(
        self,
        operation_id: UUID,
        *,
        workspace_id: UUID,
        target_id: UUID,
        generation: int,
        addon_id: str,
        addon_version: str,
        zip_sha256: str,
        expected_manifest_digest: str,
    ) -> WorkspaceDeploymentResult:

        repository = self._development_repository
        if repository is None:
            raise McpWorkspaceDevelopmentError("repository_unavailable")
        with self._sessions() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            linked = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            workspace = session.get(AgentWorkspaceRow, str(workspace_id))
            target = session.get(TargetRow, str(target_id))
            external = session.get(ExternalKodiTargetRow, str(target_id))
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
            snapshot = (
                None
                if linked is None
                else session.get(
                    RepositoryWorkspaceValidationSnapshotRow,
                    linked.validation_receipt,
                )
            )
        if (
            row is None
            or linked is None
            or workspace is None
            or target is None
            or snapshot is None
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        if (
            row.deployment_mode != "repository"
            or row.repository_origin != "planned"
            or row.initiator_kind != "mcp"
            or row.client_id is None
            or linked.origin != "planned"
            or row.state != "running"
            or linked.state not in {"awaiting_approval", "publication_approved"}
            or row.workspace_id != str(workspace_id)
            or row.target_id != str(target_id)
            or row.generation != generation
            or linked.addon_id != addon_id
            or linked.addon_version != addon_version
            or linked.zip_sha256 != zip_sha256
            or linked.expected_manifest_digest != expected_manifest_digest
        ):
            raise McpWorkspaceDevelopmentError("publication_approval_mismatch")
        if workspace.generation != generation:
            self._fail_repository_deployment(
                operation_id, "workspace_generation_conflict"
            )
            raise McpWorkspaceDevelopmentError("workspace_generation_conflict")
        if (
            target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external is None
            or external.verified_kodi_version != linked.target_kodi_version
            or pairing is None
            or pairing.pinned_bridge_id != linked.bridge_id
            or pairing.pinned_kodi_version != linked.target_kodi_version
            or repository.base_url != linked.base_url
        ):
            self._fail_repository_deployment(operation_id, "target_identity_changed")
            raise McpWorkspaceDevelopmentError("target_identity_changed")
        publication = repository.get_publication(UUID(linked.publication_id))
        if (
            publication is None
            or publication.source_kind != "workspace"
            or publication.workspace_id != str(workspace_id)
            or publication.workspace_generation != generation
            or publication.zip_sha256 != zip_sha256
            or publication.validation_receipt != linked.validation_receipt
            or publication.source_tree_sha256 is None
            or workspace.current_tree_digest != publication.source_tree_sha256
            or snapshot.source_tree_digest != publication.source_tree_sha256
            or snapshot.addon_id != addon_id
            or snapshot.addon_version != addon_version
        ):
            raise McpWorkspaceDevelopmentError("publication_approval_mismatch")
        if linked.state == "awaiting_approval":
            with self._sessions.begin() as session:
                durable = session.get(
                    RepositoryWorkspaceDeploymentRow, str(operation_id)
                )
                operation = session.get(
                    McpWorkspaceDeploymentOperationRow, str(operation_id)
                )
                if (
                    durable is None
                    or durable.state != "awaiting_approval"
                    or operation is None
                    or operation.state != "running"
                ):
                    raise McpWorkspaceDevelopmentError("deployment_ambiguous")
                durable.state = "publication_approved"
                durable.approved_at = _to_text(self._clock())
                operation.result_classification = "publication_approved"
        if publication.status == "prepared":
            try:
                repository.approve(
                    UUID(linked.publication_id), observed_sha256=zip_sha256
                )
            except ValueError as error:
                raise McpWorkspaceDevelopmentError(
                    "repository_publication_failed"
                ) from error
        elif publication.status != "current":
            raise McpWorkspaceDevelopmentError("repository_publication_failed")
        self._mark_repository_published(operation_id)
        return self._deployment_result(self._deployment_required(operation_id))

    def _mark_repository_published(self, operation_id: UUID) -> None:
        repository = self._development_repository
        if repository is None:
            raise McpWorkspaceDevelopmentError("repository_unavailable")
        try:
            repository.reconcile()
        except (OSError, ValueError) as error:
            raise McpWorkspaceDevelopmentError("repository_unavailable") from error
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            linked = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            if (
                row is None
                or linked is None
                or row.state != "running"
                or row.repository_origin != "planned"
                or row.initiator_kind != "mcp"
                or row.client_id is None
                or linked.origin != "planned"
            ):
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            publication = repository.get_publication(UUID(linked.publication_id))
            if (
                publication is None
                or publication.status != "current"
                or publication.zip_sha256 != linked.zip_sha256
                or publication.tree_sha256 != linked.package_tree_sha256
            ):
                raise McpWorkspaceDevelopmentError("repository_publication_failed")
            if linked.state == "publication_approved":
                now = _to_text(self._clock())
                linked.state = "awaiting_kodi_update"
                linked.approved_at = linked.approved_at or now
                linked.published_at = publication.published_at or now
                row.phase = "awaiting_kodi_update"
                row.result_classification = "awaiting_kodi_update"

    def recheck_repository_deployment(
        self, operation_id: UUID
    ) -> WorkspaceDeploymentResult:
        """Web-only bounded native and bridge observation; never installs add-ons."""

        targets = self._target_adapters
        bridges = self._external_bridges
        repository = self._development_repository
        if targets is None or bridges is None or repository is None:
            raise McpWorkspaceDevelopmentError("repository_unavailable")
        with self._sessions() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            linked = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            target = None if row is None else session.get(TargetRow, row.target_id)
            external = (
                None
                if row is None
                else session.get(ExternalKodiTargetRow, row.target_id)
            )
            pairing = (
                None
                if row is None
                else session.get(ExternalBridgePairingRow, row.target_id)
            )
        if (
            row is None
            or linked is None
            or row.deployment_mode != "repository"
            or row.repository_origin != "planned"
            or row.initiator_kind != "mcp"
            or row.client_id is None
            or linked.origin != "planned"
        ):
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        if row.state == "succeeded":
            raise McpWorkspaceDevelopmentError("deployment_already_completed")
        if row.state != "running":
            raise McpWorkspaceDevelopmentError(
                row.failure_code or "deployment_ambiguous"
            )
        if linked.state in {"awaiting_approval", "publication_approved"}:
            return self._deployment_result(row)
        if (
            target is None
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external is None
            or external.administrative_state.value != "enabled"
            or external.verification_state.value != "verified"
            or external.verified_kodi_version != linked.target_kodi_version
            or pairing is None
            or pairing.pinned_bridge_id != linked.bridge_id
            or pairing.pinned_kodi_version != linked.target_kodi_version
            or pairing.pairing_state.value != "paired"
        ):
            self._fail_repository_deployment(operation_id, "target_identity_changed")
            raise McpWorkspaceDevelopmentError("target_identity_changed")
        try:
            repository.reconcile()
        except (OSError, ValueError):
            self._update_deployment(
                operation_id, result_classification="repository_unavailable"
            )
            return self._deployment_result(self._deployment_required(operation_id))
        publication = repository.get_publication(UUID(linked.publication_id))
        if (
            publication is None
            or publication.status not in {"current", "superseded"}
            or publication.zip_sha256 != linked.zip_sha256
            or publication.tree_sha256 != linked.package_tree_sha256
        ):
            self._fail_repository_deployment(
                operation_id, "repository_publication_failed"
            )
            raise McpWorkspaceDevelopmentError("repository_publication_failed")
        try:
            inventory = targets.addon_inventory(UUID(row.target_id))
        except Exception:
            self._update_deployment(
                operation_id, result_classification="target_offline"
            )
            return self._deployment_result(self._deployment_required(operation_id))
        if inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE:
            self._update_deployment(
                operation_id, result_classification="target_offline"
            )
            return self._deployment_result(self._deployment_required(operation_id))
        if inventory.kodi_release != linked.target_kodi_version:
            self._fail_repository_deployment(operation_id, "target_identity_changed")
            raise McpWorkspaceDevelopmentError("target_identity_changed")
        item = next(
            (entry for entry in inventory.items if entry.addon_id == linked.addon_id),
            None,
        )
        if item is None or (
            linked.installed_before_version is not None
            and item.version == linked.installed_before_version
        ):
            return self._deployment_result(row)
        if item.version != linked.addon_version or not item.enabled:
            code = (
                "installed_version_mismatch"
                if item.version != linked.addon_version
                else "addon_disabled"
            )
            self._fail_repository_deployment(operation_id, code)
            raise McpWorkspaceDevelopmentError(code)
        with self._sessions.begin() as session:
            current = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(operation_id)
            )
            if current is None or operation is None or operation.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            current.state = "observed_identity"
            current.observed_at = _to_text(self._clock())
            operation.phase = "verify_tree"
            operation.result_classification = "observed_identity"
        try:
            observed = bridges.inspect_addon(UUID(row.target_id), linked.addon_id)
        except ExternalBridgeServiceError as error:
            if error.code in {"bridge_unreachable", "target_unreachable"}:
                return self._deployment_result(self._deployment_required(operation_id))
            self._fail_repository_deployment(operation_id, "installed_tree_unverified")
            raise McpWorkspaceDevelopmentError("installed_tree_unverified") from error
        if (
            observed.addon_id != linked.addon_id
            or observed.addon_version != linked.addon_version
            or not observed.enabled
            or observed.manifest_digest != linked.expected_manifest_digest
            or observed.file_count != linked.file_count
            or observed.total_bytes != linked.total_bytes
        ):
            self._fail_repository_deployment(operation_id, "installed_tree_mismatch")
            raise McpWorkspaceDevelopmentError("installed_tree_mismatch")
        with self._sessions.begin() as session:
            current = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(operation_id)
            )
            if current is None or operation is None or operation.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            current.state = "verified_tree"
            current.verified_at = _to_text(self._clock())
            operation.result_classification = "verified_tree"
        with self._sessions.begin() as session:
            current = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(operation_id)
            )
            if current is None or operation is None or operation.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            now = _to_text(self._clock())
            current.state = "succeeded"
            current.verified_at = now
            session.execute(
                delete(DevelopmentPublicationHoldRow).where(
                    DevelopmentPublicationHoldRow.publication_id
                    == current.publication_id,
                    DevelopmentPublicationHoldRow.reason
                    == "pending_repository_deployment",
                    DevelopmentPublicationHoldRow.reference_id == operation.id,
                )
            )
            operation.state = "succeeded"
            operation.phase = "finalize"
            operation.inventory_classification = "matched"
            operation.addon_enabled = True
            operation.verified_destination_digest = observed.manifest_digest
            operation.result_classification = "repository_verified"
            operation.completed_at = now
        return self._deployment_result(self._deployment_required(operation_id))

    def _fail_repository_deployment(self, operation_id: UUID, code: str) -> None:
        with self._sessions.begin() as session:
            linked = session.get(RepositoryWorkspaceDeploymentRow, str(operation_id))
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            if (
                linked is None
                or row is None
                or linked.origin != "planned"
                or row.repository_origin != "planned"
                or row.initiator_kind != "mcp"
            ):
                raise McpWorkspaceDevelopmentError("deployment_not_allowed")
            if row.state == "running":
                linked.state = "failed"
                linked.failure_code = code
                session.execute(
                    delete(DevelopmentPublicationHoldRow).where(
                        DevelopmentPublicationHoldRow.publication_id
                        == linked.publication_id,
                        DevelopmentPublicationHoldRow.reason
                        == "pending_repository_deployment",
                        DevelopmentPublicationHoldRow.reference_id == row.id,
                    )
                )
                row.state = "failed"
                row.phase = "finalize"
                row.failure_code = code
                row.completed_at = _to_text(self._clock())

    def configure_external(
        self,
        *,
        bridges: ExternalBridgeService,
        targets: _TargetAdapters,
    ) -> None:
        """Complete the shared adapter graph after Web composition is built."""

        self._external_bridges = bridges
        self._target_adapters = targets

    def establish_external_baseline(
        self, *, target_id: UUID, source_id: UUID
    ) -> ExternalBaselineResult:
        """Web-only exact comparison; MCP has no route to this method."""

        bridges = self._external_bridges
        if bridges is None:
            raise McpWorkspaceDevelopmentError("bridge_capability_unavailable")
        manifest = self._workspaces.capture_registered_source_manifest(source_id)
        files = [item for item in manifest.entries if item.entry_type == "file"]
        if (
            not files
            or any(item.size > 2 * 1024 * 1024 for item in files)
            or len(files) > 25_000
            or len(manifest.entries) > 50_000
            or not 1 <= manifest.included_bytes <= 1024 * 1024 * 1024
        ):
            raise McpWorkspaceDevelopmentError("transfer_too_large")
        with self._sessions() as session:
            source = DevelopmentSourceRepository(session).get(source_id)
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
        if source is None or pairing is None or pairing.pinned_bridge_id is None:
            raise McpWorkspaceDevelopmentError("target_provenance_unknown")
        try:
            observed = bridges.inspect_addon(target_id, source.addon_id)
        except ExternalBridgeServiceError as error:
            raise McpWorkspaceDevelopmentError("target_provenance_unknown") from error
        if (
            observed.addon_id != source.addon_id
            or observed.addon_version != source.observed_version
            or observed.manifest_digest != manifest.digest
            or observed.file_count != len(files)
            or observed.total_bytes != manifest.included_bytes
        ):
            raise McpWorkspaceDevelopmentError("target_provenance_mismatch")
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(
                ExternalDevelopmentSourceBaselineRow,
                {
                    "target_id": str(target_id),
                    "development_source_id": str(source_id),
                },
            )
            values = {
                "bridge_id": pairing.pinned_bridge_id,
                "state": "current",
                "addon_id": source.addon_id,
                "addon_version": source.observed_version,
                "manifest_digest": manifest.digest,
                "file_count": len(files),
                "total_bytes": manifest.included_bytes,
                "established_at": _to_text(now),
                "checked_at": _to_text(now),
                "invalidated_at": None,
            }
            if row is None:
                session.add(
                    ExternalDevelopmentSourceBaselineRow(
                        target_id=str(target_id),
                        development_source_id=str(source_id),
                        **values,
                    )
                )
            else:
                for name, value in values.items():
                    setattr(row, name, value)
            session.add(
                ExternalBridgeEventRow(
                    id=str(uuid4()),
                    target_id=str(target_id),
                    bridge_id=pairing.pinned_bridge_id,
                    request_id=None,
                    event_kind="baseline_established",
                    classification="exact_manifest_match",
                    byte_size=manifest.included_bytes,
                    width=None,
                    height=None,
                    content_digest=manifest.digest.removeprefix("sha256:"),
                    occurred_at=_to_text(now),
                )
            )
        return ExternalBaselineResult(
            target_id=target_id,
            source_id=source_id,
            bridge_id=UUID(pairing.pinned_bridge_id),
            addon_id=source.addon_id,
            addon_version=source.observed_version,
            manifest_digest=manifest.digest,
            file_count=len(files),
            total_bytes=manifest.included_bytes,
            established_at=now,
        )

    def reconcile_external_deployment(self, operation_id: UUID) -> str:
        """Web-only receipt reconciliation; never redispatch deployment."""

        bridges = self._external_bridges
        if bridges is None:
            raise McpWorkspaceDevelopmentError("bridge_capability_unavailable")
        operation = self._deployment_required(operation_id)
        if operation.target_class != "external_development":
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        with self._sessions() as session:
            local = session.get(ExternalBridgeDeploymentReceiptRow, str(operation_id))
        if (
            local is None
            or local.state != "ambiguous"
            or operation.state not in {"ambiguous", "reconciliation_required"}
        ):
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        try:
            receipt = bridges.deployment_receipt(
                UUID(operation.target_id), operation_id
            )
        except ExternalBridgeServiceError as error:
            raise McpWorkspaceDevelopmentError("deployment_ambiguous") from error
        if (
            receipt.deployment_id != operation_id
            or receipt.request_id != UUID(operation.request_id)
            or operation.bridge_id is None
            or receipt.bridge_id != UUID(operation.bridge_id)
            or receipt.addon_id != operation.addon_id
            or receipt.addon_version != operation.addon_version
            or receipt.generation != operation.generation
            or receipt.manifest_digest != local.expected_manifest_digest
            or receipt.file_count != local.file_count
            or receipt.total_bytes != local.total_bytes
        ):
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        if receipt.state == "ambiguous":
            return "still_ambiguous"
        if receipt.state not in {"succeeded", "failed"} or receipt.receipt_id is None:
            return "still_ambiguous"
        if receipt.state == "succeeded" and (
            not receipt.inventory_verified
            or receipt.enabled_state not in {"enabled", "disabled"}
        ):
            return "still_ambiguous"
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
            correlation = session.get(
                ExternalBridgeDeploymentReceiptRow, str(operation_id)
            )
            if row is None or correlation is None:
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            correlation.state = receipt.state
            correlation.classification = receipt.classification
            correlation.bridge_receipt_id = str(receipt.receipt_id)
            correlation.completed_at = _to_text(receipt.completed_at or now)
            correlation.reconciled_at = _to_text(now)
            row.bridge_receipt_id = str(receipt.receipt_id)
            row.completed_at = _to_text(receipt.completed_at or now)
            row.phase = "finalize"
            if receipt.state == "succeeded":
                row.state = "succeeded"
                row.sync_completed = True
                row.target_started = True
                row.inventory_classification = "matched"
                row.addon_enabled = receipt.enabled_state == "enabled"
                row.result_classification = receipt.classification
                row.verified_destination_digest = receipt.manifest_digest
                row.failure_code = None
            else:
                row.state = "failed"
                row.failure_code = "deployment_verification_failed"
            session.add(
                ExternalBridgeEventRow(
                    id=str(uuid4()),
                    target_id=operation.target_id,
                    bridge_id=operation.bridge_id,
                    request_id=operation.request_id,
                    event_kind="deployment_reconciled",
                    classification=receipt.classification,
                    byte_size=receipt.total_bytes,
                    width=None,
                    height=None,
                    content_digest=receipt.manifest_digest.removeprefix("sha256:"),
                    occurred_at=_to_text(now),
                )
            )
        return receipt.state

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
            workspace = session.get(AgentWorkspaceRow, str(workspace_id))
            if (
                workspace is None
                or workspace.development_source_id != str(source_id)
                or workspace.generation != generation
                or workspace.current_tree_digest is None
            ):
                raise McpWorkspaceDevelopmentError("validation_generation_changed")
            row = session.scalar(
                select(McpWorkspaceValidationRow).where(
                    McpWorkspaceValidationRow.workspace_id == str(workspace_id)
                )
            )
            values = {
                "id": str(evidence_id),
                "development_source_id": str(source_id),
                "generation": generation,
                "tree_digest": workspace.current_tree_digest,
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
        deployment_mode: str = "auto",
    ) -> WorkspaceDeploymentResult:
        if deployment_mode not in {"auto", "direct", "repository"}:
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        if deployment_mode == "auto":
            with self._sessions() as session:
                target = session.get(TargetRow, str(target_id))
                ready = session.get(
                    ExternalDevelopmentRepositoryReadinessRow, str(target_id)
                )
            deployment_mode = (
                "repository"
                if target is not None
                and target.target_type is TargetType.EXTERNAL_DEVELOPMENT
                and ready is not None
                else "direct"
            )
        row, created = self._claim_deployment(
            client_id,
            request_id,
            workspace_id,
            expected_generation,
            target_id,
            deployment_mode,
        )
        if not created or row.state != "pending":
            return self._replay_deployment(
                row,
                client_id,
                workspace_id,
                expected_generation,
                target_id,
                deployment_mode,
            )
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            self._acquire_deployment(row, digest)
            with workspace_coordination_lock(
                self._data_root, workspace_id, exclusive=True, blocking=False
            ):
                if deployment_mode == "repository":
                    return self._run_repository_deployment(row, digest)
                return self._run_deployment(UUID(row.id), digest)
        except McpWorkspaceDevelopmentError as error:
            current = self._deployment(UUID(row.id))
            if current is not None and current.state in {"pending", "running"}:
                uncertain = current.mutation_started
                if uncertain and current.target_class == "external_development":
                    with self._sessions() as session:
                        receipt = session.get(
                            ExternalBridgeDeploymentReceiptRow, current.id
                        )
                    if receipt is not None and receipt.state == "failed":
                        uncertain = False
                if uncertain and current.target_class == "internal_lab":
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
            if (
                current is not None
                and uncertain
                and current.target_class == "internal_lab"
            ):
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
        execution_kind: WorkspaceExecutionKind = WorkspaceExecutionKind.SMOKE,
    ) -> WorkspaceExecutionResult:
        if not isinstance(execution_kind, WorkspaceExecutionKind):
            raise McpWorkspaceDevelopmentError("execution_unsupported")
        row, created = self._claim_execution(
            client_id,
            request_id,
            workspace_id,
            expected_generation,
            target_id,
            execution_kind,
        )
        if not created:
            return self._replay_execution(
                row, workspace_id, expected_generation, target_id, execution_kind
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
                return self._run_execution(UUID(row.id), digest, execution_kind)
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
                    result_classification=(
                        error.reason
                        if not dispatched
                        and error.code == "execution_unsupported"
                        and error.reason in EXECUTION_UNSUPPORTED_REASONS
                        else None
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
            launch = session.scalar(
                select(McpWorkspaceLaunchRow)
                .where(McpWorkspaceLaunchRow.workspace_id == str(workspace_id))
                .order_by(
                    McpWorkspaceLaunchRow.requested_at.desc(),
                    McpWorkspaceLaunchRow.id.desc(),
                )
                .limit(1)
            )
        return WorkspaceDevelopmentProjection(
            launch_id=None if launch is None else UUID(launch.id),
            launch_request_id=None if launch is None else UUID(launch.request_id),
            launch_state=None if launch is None else launch.state,
            launch_generation=None if launch is None else launch.generation,
            launch_target_id=None if launch is None else UUID(launch.target_id),
            launch_extension_ordinal=None if launch is None else launch.ordinal,
            validation_id=None if validation is None else UUID(validation.id),
            validation_state=None if validation is None else validation.classification,
            validation_generation=None if validation is None else validation.generation,
            validated_at=None
            if validation is None
            else _from_text(validation.validated_at),
            deployment_id=None if deployment is None else UUID(deployment.id),
            deployment_state=None if deployment is None else deployment.state,
            deployment_mode=None if deployment is None else deployment.deployment_mode,
            deployment_phase=None if deployment is None else deployment.phase,
            deployment_publication_id=(
                None
                if deployment is None or deployment.deployment_mode != "repository"
                else self._repository_publication_id(deployment.id)
            ),
            deployment_installation_kind=(
                None
                if deployment is None or deployment.deployment_mode != "repository"
                else self._deployment_result(deployment).installation_kind
            ),
            deployment_next_action=(
                None
                if deployment is None or deployment.deployment_mode != "repository"
                else self._deployment_result(deployment).next_action
            ),
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
            if (
                session.scalar(
                    select(McpWorkspaceLaunchRow.id)
                    .where(
                        McpWorkspaceLaunchRow.workspace_id == str(workspace_id),
                        McpWorkspaceLaunchRow.state.in_(
                            ("pending", "dispatching", "ambiguous")
                        ),
                    )
                    .limit(1)
                )
                is not None
            ):
                return "launch_requires_reconciliation"
        with self._sessions() as session:
            captured_history = session.scalar(
                select(AgentWorkspaceCaptureOperationRow.id)
                .where(
                    AgentWorkspaceCaptureOperationRow.workspace_id == str(workspace_id)
                )
                .limit(1)
            )
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
        if captured_history is not None:
            return "workspace_capture_history_retained"
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
            if deployment.deployment_mode == "repository":
                with self._sessions() as session:
                    linked = session.get(
                        RepositoryWorkspaceDeploymentRow, deployment.id
                    )
                if (
                    linked is not None
                    and linked.origin == "planned"
                    and deployment.repository_origin == "planned"
                    and deployment.initiator_kind == "mcp"
                    and linked.state
                    in {
                        "awaiting_approval",
                        "publication_approved",
                        "awaiting_kodi_update",
                        "observed_identity",
                        "verified_tree",
                    }
                ):
                    if linked.state in {"awaiting_approval", "publication_approved"}:
                        with self._sessions() as session:
                            workspace = session.get(
                                AgentWorkspaceRow, deployment.workspace_id
                            )
                        publication = (
                            None
                            if self._development_repository is None
                            else self._development_repository.get_publication(
                                UUID(linked.publication_id)
                            )
                        )
                        if (
                            workspace is None
                            or workspace.generation != linked.workspace_generation
                        ) and (
                            linked.state == "awaiting_approval"
                            or publication is None
                            or publication.status != "current"
                        ):
                            self._fail_repository_deployment(
                                UUID(deployment.id), "workspace_generation_conflict"
                            )
                            continue
                    if (
                        linked.state == "publication_approved"
                        and self._development_repository is not None
                        and publication is not None
                        and publication.status == "current"
                    ):
                        with suppress(McpWorkspaceDevelopmentError):
                            self._mark_repository_published(UUID(deployment.id))
                    continue
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
        deployment_mode: str = "direct",
    ) -> tuple[McpWorkspaceDeploymentOperationRow, bool]:
        fingerprint = _fingerprint(
            DEPLOY_OPERATION_KIND,
            workspace_id,
            generation,
            target_id,
            deployment_mode,
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
                    if existing.parameter_fingerprint != fingerprint:
                        raise McpWorkspaceDevelopmentError("request_id_conflict")
                    row = session.get(
                        McpWorkspaceDeploymentOperationRow, existing.operation_id
                    )
                    if row is None:
                        raise McpWorkspaceDevelopmentError("deployment_ambiguous")
                    session.expunge(row)
                    session.commit()
                    return row, False
                workspace, validation, target = self._deployment_preflight_rows(
                    session,
                    workspace_id,
                    generation,
                    target_id,
                    require_baseline=deployment_mode == "direct",
                )
                if deployment_mode == "repository" and (
                    target.target_class != "external_development"
                    or self._development_repository is None
                    or session.get(
                        ExternalDevelopmentRepositoryReadinessRow, str(target_id)
                    )
                    is None
                ):
                    raise McpWorkspaceDevelopmentError("deployment_not_allowed")
                row = McpWorkspaceDeploymentOperationRow(
                    id=str(operation_id),
                    client_id=str(client_id),
                    request_id=str(request_id),
                    workspace_id=str(workspace_id),
                    development_source_id=str(workspace.development_source_id),
                    target_id=str(target_id),
                    runtime_revision_id=(
                        None
                        if target.runtime_revision_id is None
                        else str(target.runtime_revision_id)
                    ),
                    target_class=target.target_class,
                    deployment_mode=deployment_mode,
                    repository_origin=(
                        "planned" if deployment_mode == "repository" else None
                    ),
                    bridge_id=(
                        None if target.bridge_id is None else str(target.bridge_id)
                    ),
                    bridge_request_id=(
                        str(request_id)
                        if target.target_class == "external_development"
                        and deployment_mode == "direct"
                        else None
                    ),
                    bridge_receipt_id=None,
                    file_count=None,
                    total_bytes=None,
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
                if (
                    target.target_class == "internal_lab"
                    and ManagedRuntimeProjectionRepository(
                        session
                    ).has_blocking_runtime_reconciliation(target_id)
                ):
                    raise ManagedRuntimeReconciliationBlockedError(
                        "Runtime reconciliation is unresolved for this "
                        "managed instance."
                    )
                if (
                    target.target_class == "internal_lab"
                    and ManagedInstanceMutationLeaseRepository(session).current(
                        target_id
                    )
                    is not None
                ) or GlobalHeavyOperationLeaseRepository(session).current() is not None:
                    raise McpWorkspaceDevelopmentError("deployment_conflict")
                # A cooldown denial retains replay history without invalidating runtime.
                if not self._deployment_rate.allow(f"{client_id}:{target_id}"):
                    row.state = "failed"
                    row.phase = "finalize"
                    row.failure_code = "rate_limited"
                    row.started_at = _to_text(now)
                    row.completed_at = _to_text(now)
                    session.flush()
                    session.expunge(row)
                    session.commit()
                    return row, True
                if target.target_class == "internal_lab":
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
        execution_kind: WorkspaceExecutionKind = WorkspaceExecutionKind.SMOKE,
    ) -> tuple[McpWorkspaceExecutionOperationRow, bool]:
        fingerprint = _fingerprint(
            EXECUTE_OPERATION_KIND,
            workspace_id,
            generation,
            target_id,
            execution_kind=execution_kind,
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
                target = session.get(TargetRow, str(target_id))
                if workspace is None or workspace.generation != generation:
                    raise McpWorkspaceDevelopmentError("workspace_generation_conflict")
                deployments = list(
                    session.scalars(
                        select(McpWorkspaceDeploymentOperationRow)
                        .where(
                            McpWorkspaceDeploymentOperationRow.workspace_id
                            == str(workspace_id),
                            McpWorkspaceDeploymentOperationRow.target_id
                            == str(target_id),
                            McpWorkspaceDeploymentOperationRow.generation == generation,
                            McpWorkspaceDeploymentOperationRow.state == "succeeded",
                        )
                        .limit(2)
                    )
                )
                if not deployments:
                    raise McpWorkspaceDevelopmentError("execution_not_deployed")
                if len(deployments) != 1:
                    raise McpWorkspaceDevelopmentError("execution_ambiguous")
                deployment = deployments[0]
                if target is None:
                    raise McpWorkspaceDevelopmentError("execution_not_deployed")
                if (
                    execution_kind
                    is WorkspaceExecutionKind.KODI_RPC_NEGATIVE_DIAGNOSTIC
                    and deployment.target_class != "internal_lab"
                ):
                    raise McpWorkspaceDevelopmentError("execution_unsupported")
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
                    execution_kind=execution_kind.value,
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
                if target.target_type is TargetType.MANAGED:
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
        self,
        session: Session,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
        *,
        require_baseline: bool = True,
    ) -> tuple[Any, McpWorkspaceValidationRow, _DeploymentTarget]:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        validation = session.scalar(
            select(McpWorkspaceValidationRow).where(
                McpWorkspaceValidationRow.workspace_id == str(workspace_id)
            )
        )
        common_target = session.get(TargetRow, str(target_id))
        instance = ManagedInstanceRepository(session).get(target_id)
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
        if common_target is None:
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        if common_target.target_type is TargetType.MANAGED:
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                target_id, source.id
            )
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
                    McpWorkspaceDeploymentOperationRow.workspace_id
                    == str(workspace_id),
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
            return (
                workspace,
                validation,
                _DeploymentTarget(
                    target_class="internal_lab",
                    runtime_revision_id=instance.runtime_revision_id,
                ),
            )
        if common_target.target_type is not TargetType.EXTERNAL_DEVELOPMENT:
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        external = session.get(ExternalKodiTargetRow, str(target_id))
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        external_baseline = session.get(
            ExternalDevelopmentSourceBaselineRow,
            {
                "target_id": str(target_id),
                "development_source_id": str(source.id),
            },
        )
        if (
            external is None
            or external.administrative_state.value != "enabled"
            or external.verification_state.value != "verified"
            or pairing is None
            or pairing.pairing_state.value != "paired"
            or pairing.reachability_state.value != "reachable"
            or pairing.pinned_bridge_id is None
            or "workspace_deployment"
            not in set(json.loads(pairing.verified_capabilities_json))
            or (
                require_baseline
                and (
                    external_baseline is None
                    or external_baseline.state != "current"
                    or external_baseline.bridge_id != pairing.pinned_bridge_id
                    or external_baseline.addon_id != source.addon_id
                )
            )
        ):
            raise McpWorkspaceDevelopmentError("target_provenance_unknown")
        return (
            workspace,
            validation,
            _DeploymentTarget(
                target_class="external_development",
                bridge_id=UUID(pairing.pinned_bridge_id),
            ),
        )

    def _acquire_deployment(
        self, row: McpWorkspaceDeploymentOperationRow, digest: str
    ) -> None:
        now = self._clock()
        expires = now + OPERATION_LEASE
        with self._sessions.begin() as session:
            mutation = True
            if row.target_class == "internal_lab":
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
            deployment = session.get(
                McpWorkspaceDeploymentOperationRow, row.deployment_id
            )
            mutation = True
            if deployment is None:
                raise McpWorkspaceDevelopmentError("execution_not_deployed")
            if deployment.target_class == "internal_lab":
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
        if operation.target_class == "external_development":
            return self._run_external_deployment(operation, digest)
        container_id = self._exact_running_container(UUID(operation.target_id))
        self._update_deployment(operation_id, phase="stop", mutation_started=True)
        try:
            self._broker.stop_managed_container(
                managed_instance_id=operation.target_id,
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
        except BrokerUnavailableError as error:
            self._finish_deployment(
                operation.id,
                "failed",
                "broker_unavailable_before_dispatch",
                digest=digest,
            )
            raise McpWorkspaceDevelopmentError(
                "broker_unavailable_before_dispatch"
            ) from error
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
            uncertain = isinstance(
                error, (ClientTransportError, ClientProtocolError)
            ) or code in {
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
            if not isinstance(error, (ClientTransportError, ClientProtocolError)):
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
                addon_enabled=(
                    item.enabled
                    if item is not None
                    and inventory.query_status is AddonInventoryQueryStatus.AVAILABLE
                    and item.version == operation.addon_version
                    else None
                ),
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

    def _run_external_deployment(
        self, operation: McpWorkspaceDeploymentOperationRow, digest: str
    ) -> WorkspaceDeploymentResult:
        bridges = self._external_bridges
        targets = self._target_adapters
        if bridges is None or targets is None or operation.bridge_id is None:
            raise McpWorkspaceDevelopmentError("bridge_capability_unavailable")
        with tempfile.TemporaryDirectory(
            prefix=f"external-deployment-{operation.id}-", dir=self._data_root
        ) as temporary:
            stage_root = Path(temporary)
            try:
                manifest = capture_workspace_deployment_tree(
                    self._data_root, UUID(operation.workspace_id), stage_root=stage_root
                )
            except Exception as error:
                raise McpWorkspaceDevelopmentError("transfer_invalid") from error
            files = [item for item in manifest.entries if item.entry_type == "file"]
            if (
                not files
                or any(item.size > 2 * 1024 * 1024 for item in files)
                or len(files) > 25_000
                or len(manifest.entries) > 50_000
                or not 1 <= manifest.included_bytes <= 1024 * 1024 * 1024
            ):
                raise McpWorkspaceDevelopmentError("transfer_too_large")
            with self._sessions() as session:
                workspace = AgentWorkspaceRepository(session).get(
                    UUID(operation.workspace_id)
                )
            if (
                workspace is None
                or workspace.generation != operation.generation
                or workspace.current_tree_digest != manifest.digest
            ):
                raise McpWorkspaceDevelopmentError("workspace_generation_conflict")
            self._prepare_external_receipt(
                operation,
                manifest.digest,
                len(files),
                manifest.included_bytes,
            )

            def before_commit() -> None:
                with self._sessions() as session:
                    external = session.get(ExternalKodiTargetRow, operation.target_id)
                    pairing = session.get(ExternalBridgePairingRow, operation.target_id)
                if (
                    external is None
                    or external.administrative_state.value != "enabled"
                    or external.verification_state.value != "verified"
                    or pairing is None
                    or pairing.pairing_state.value != "paired"
                    or pairing.reachability_state.value != "reachable"
                    or pairing.pinned_bridge_id != operation.bridge_id
                    or "workspace_deployment"
                    not in set(json.loads(pairing.verified_capabilities_json))
                ):
                    raise McpWorkspaceDevelopmentError("deployment_not_allowed")
                self._update_deployment(
                    UUID(operation.id),
                    phase="replace",
                    mutation_started=True,
                    sync_started=True,
                )

            try:
                receipt = bridges.deploy_workspace(
                    UUID(operation.target_id),
                    operation_id=UUID(operation.id),
                    request_id=UUID(operation.request_id),
                    addon_id=operation.addon_id,
                    addon_version=operation.addon_version,
                    generation=operation.generation,
                    manifest=manifest,
                    stage_root=stage_root,
                    before_commit=before_commit,
                )
            except ExternalBridgeServiceError as error:
                if error.code == "deployment_ambiguous":
                    self._mark_external_receipt_ambiguous(operation.id)
                raise McpWorkspaceDevelopmentError(error.code) from error
        if (
            receipt.deployment_id != UUID(operation.id)
            or receipt.request_id != UUID(operation.request_id)
            or receipt.bridge_id != UUID(operation.bridge_id)
            or receipt.addon_id != operation.addon_id
            or receipt.addon_version != operation.addon_version
            or receipt.generation != operation.generation
            or receipt.manifest_digest != manifest.digest
            or receipt.file_count != len(files)
            or receipt.total_bytes != manifest.included_bytes
        ):
            self._mark_external_receipt_ambiguous(operation.id)
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        if receipt.state == "ambiguous":
            self._mark_external_receipt_ambiguous(operation.id)
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        if receipt.state == "succeeded" and (
            not receipt.inventory_verified
            or receipt.enabled_state not in {"enabled", "disabled"}
        ):
            self._mark_external_receipt_ambiguous(operation.id)
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        if receipt.state != "succeeded" or receipt.receipt_id is None:
            self._complete_external_receipt(operation.id, receipt)
            raise McpWorkspaceDevelopmentError("deployment_verification_failed")
        try:
            inventory = targets.addon_inventory(UUID(operation.target_id))
        except Exception as error:
            self._mark_external_receipt_ambiguous(operation.id)
            raise McpWorkspaceDevelopmentError("deployment_ambiguous") from error
        item = next(
            (
                entry
                for entry in inventory.items
                if entry.addon_id == operation.addon_id
            ),
            None,
        )
        if (
            inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
            or item is None
            or item.version != operation.addon_version
        ):
            self._complete_external_receipt(operation.id, receipt)
            raise McpWorkspaceDevelopmentError("deployment_verification_failed")
        self._complete_external_receipt(operation.id, receipt)
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, operation.id)
            if row is None or row.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            row.phase = "inventory"
            row.sync_completed = True
            row.target_started = True
            row.inventory_classification = "matched"
            row.addon_enabled = item.enabled
            row.result_classification = receipt.classification
            row.source_manifest_digest = manifest.digest
            row.verified_destination_digest = receipt.manifest_digest
            row.bridge_receipt_id = str(receipt.receipt_id)
            row.file_count = len(files)
            row.total_bytes = manifest.included_bytes
            baseline = session.get(
                ExternalDevelopmentSourceBaselineRow,
                {
                    "target_id": operation.target_id,
                    "development_source_id": operation.development_source_id,
                },
            )
            if baseline is not None:
                baseline.checked_at = _to_text(self._clock())
        self._finish_deployment(operation.id, "succeeded", None, digest=digest)
        return self._deployment_result(self._deployment_required(UUID(operation.id)))

    def _run_repository_deployment(
        self, claimed: McpWorkspaceDeploymentOperationRow, digest: str
    ) -> WorkspaceDeploymentResult:
        """Prepare an immutable publication; leave deployment pending for a human."""

        repository = self._development_repository
        targets = self._target_adapters
        bridges = self._external_bridges
        if repository is None or targets is None or bridges is None:
            raise McpWorkspaceDevelopmentError("repository_unavailable")
        operation = self._deployment_required(UUID(claimed.id))
        if operation.target_class != "external_development":
            raise McpWorkspaceDevelopmentError("deployment_not_allowed")
        with self._sessions() as session:
            workspace, validation, _ = self._deployment_preflight_rows(
                session,
                UUID(operation.workspace_id),
                operation.generation,
                UUID(operation.target_id),
                require_baseline=False,
            )
            external = session.get(ExternalKodiTargetRow, operation.target_id)
            readiness = session.get(
                ExternalDevelopmentRepositoryReadinessRow, operation.target_id
            )
            pairing = session.get(ExternalBridgePairingRow, operation.target_id)
            if external is None or readiness is None or pairing is None:
                raise McpWorkspaceDevelopmentError(
                    "repository_prerequisite_unconfirmed"
                )
            kodi_version = external.verified_kodi_version
            pinned_bridge_id = pairing.pinned_bridge_id
            feed = _repository_feed(repository)
            bootstrap_hash = str(feed["bootstrap_zip_sha256"])
            if (
                kodi_version is None
                or pinned_bridge_id is None
                or readiness.base_url != repository.base_url
                or readiness.bootstrap_sha256 != bootstrap_hash
                or readiness.native_kodi_version != kodi_version
                or readiness.bridge_id != pinned_bridge_id
                or not readiness.manual_updates_confirmed
                or pairing.pinned_kodi_version != kodi_version
            ):
                raise McpWorkspaceDevelopmentError(
                    "repository_prerequisite_unconfirmed"
                )
            source_digest = workspace.current_tree_digest
        assert source_digest is not None
        generation_name = {"19": "matrix", "20": "nexus", "21": "omega"}.get(
            kodi_version.split(".", 1)[0]
        )
        if generation_name is None:
            raise McpWorkspaceDevelopmentError("repository_kodi_incompatible")
        try:
            inventory = targets.addon_inventory(UUID(operation.target_id))
            repo_tree = bridges.inspect_addon(
                UUID(operation.target_id), DEVELOPMENT_REPOSITORY_ID
            )
        except Exception as error:
            raise McpWorkspaceDevelopmentError("target_unreachable") from error
        repo_item = next(
            (
                item
                for item in inventory.items
                if item.addon_id == DEVELOPMENT_REPOSITORY_ID
            ),
            None,
        )
        if (
            inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
            or inventory.kodi_release != kodi_version
            or repo_item is None
            or not repo_item.enabled
            or repo_item.version != "1.0.0"
            or not repo_tree.enabled
            or repo_tree.manifest_digest != readiness.observed_manifest_digest
        ):
            raise McpWorkspaceDevelopmentError("repository_prerequisite_unconfirmed")
        installed = next(
            (item for item in inventory.items if item.addon_id == operation.addon_id),
            None,
        )
        try:
            candidate_version = repository_version(operation.addon_version)
            if installed is not None and candidate_version <= repository_version(
                installed.version
            ):
                raise ValueError("workspace version must exceed installed version")
            current = next(
                (
                    item
                    for item in repository.list_publications()
                    if item.addon_id == operation.addon_id and item.status == "current"
                ),
                None,
            )
            if current is not None and candidate_version < repository_version(
                current.version
            ):
                raise ValueError(
                    "workspace version must exceed current repository version"
                )
        except ValueError as error:
            raise McpWorkspaceDevelopmentError(
                "repository_version_not_newer"
            ) from error
        with tempfile.TemporaryDirectory(
            prefix=f"repository-workspace-{operation.id}-", dir=self._data_root
        ) as temporary:
            stage_root = Path(temporary)
            try:
                manifest = capture_workspace_deployment_tree(
                    self._data_root, UUID(operation.workspace_id), stage_root=stage_root
                )
                if manifest.digest != source_digest:
                    raise ValueError("workspace generation changed")
                package = build_workspace_package(
                    operation.addon_id, manifest, stage_root
                )
                inspect_addon_zip(
                    package.content, operation.addon_id, operation.addon_version
                )
                checked = repository.prepare_workspace(
                    package.content,
                    (generation_name,),
                    operation_id=UUID(operation.id),
                    workspace_id=UUID(operation.workspace_id),
                    workspace_generation=operation.generation,
                    target_id=UUID(operation.target_id),
                    validation_receipt=UUID(validation.id),
                    source_tree_sha256=manifest.digest,
                )
            except DevelopmentPublicationConflict as error:
                raise McpWorkspaceDevelopmentError(
                    "repository_same_version_conflict"
                ) from error
            except (ValueError, OSError, AgentWorkspaceStorageError) as error:
                raise McpWorkspaceDevelopmentError(
                    "repository_package_invalid"
                ) from error
        if (
            checked.addon_id != operation.addon_id
            or checked.version != operation.addon_version
            or checked.zip_sha256 != package.zip_sha256
            or checked.tree_sha256 != package.package_tree_sha256
        ):
            raise McpWorkspaceDevelopmentError("repository_package_invalid")
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, operation.id)
            if row is None or row.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            snapshot = session.get(
                RepositoryWorkspaceValidationSnapshotRow, validation.id
            )
            snapshot_values = {
                "workspace_id": operation.workspace_id,
                "generation": operation.generation,
                "addon_id": operation.addon_id,
                "addon_version": operation.addon_version,
                "extension_types_json": validation.extension_types_json,
                "source_tree_digest": source_digest,
                "validated_at": validation.validated_at,
            }
            if snapshot is None:
                session.add(
                    RepositoryWorkspaceValidationSnapshotRow(
                        id=validation.id, **snapshot_values
                    )
                )
            elif any(
                getattr(snapshot, key) != value
                for key, value in snapshot_values.items()
            ):
                raise McpWorkspaceDevelopmentError("validation_receipt_conflict")
            session.flush()
            session.add(
                RepositoryWorkspaceDeploymentRow(
                    operation_id=operation.id,
                    publication_id=checked.id,
                    workspace_id=operation.workspace_id,
                    workspace_generation=operation.generation,
                    target_id=operation.target_id,
                    target_kodi_version=kodi_version,
                    bridge_id=pinned_bridge_id,
                    addon_id=operation.addon_id,
                    addon_version=operation.addon_version,
                    zip_sha256=package.zip_sha256,
                    package_tree_sha256=package.package_tree_sha256,
                    expected_manifest_digest=package.installed_manifest_digest,
                    file_count=package.file_count,
                    total_bytes=package.total_bytes,
                    validation_receipt=validation.id,
                    repository_id=DEVELOPMENT_REPOSITORY_ID,
                    base_url=repository.base_url,
                    bootstrap_sha256=bootstrap_hash,
                    installed_before_version=None
                    if installed is None
                    else installed.version,
                    origin="planned",
                    state="awaiting_approval",
                    created_at=_to_text(self._clock()),
                )
            )
            session.add(
                DevelopmentPublicationHoldRow(
                    id=str(uuid4()),
                    publication_id=checked.id,
                    reason="pending_repository_deployment",
                    reference_id=operation.id,
                    created_at=_to_text(self._clock()),
                )
            )
            row.phase = "publication_approval"
            row.source_manifest_digest = package.installed_manifest_digest
            row.file_count = package.file_count
            row.total_bytes = package.total_bytes
            row.result_classification = "awaiting_publication_approval"
            GlobalHeavyOperationLeaseRepository(session).release(
                operation_id=UUID(operation.id), token_digest=digest
            )
        return self._deployment_result(self._deployment_required(UUID(operation.id)))

    def _prepare_external_receipt(
        self,
        operation: McpWorkspaceDeploymentOperationRow,
        manifest_digest: str,
        file_count: int,
        total_bytes: int,
    ) -> None:
        if operation.bridge_id is None or operation.bridge_request_id is None:
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        with self._sessions.begin() as session:
            row = session.get(McpWorkspaceDeploymentOperationRow, operation.id)
            if row is None or row.state != "running":
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            row.phase = "transfer"
            row.source_manifest_digest = manifest_digest
            row.file_count = file_count
            row.total_bytes = total_bytes
            session.add(
                ExternalBridgeDeploymentReceiptRow(
                    operation_id=operation.id,
                    target_id=operation.target_id,
                    bridge_id=operation.bridge_id,
                    bridge_request_id=operation.bridge_request_id,
                    bridge_receipt_id=None,
                    expected_manifest_digest=manifest_digest,
                    state="pending",
                    classification=None,
                    file_count=file_count,
                    total_bytes=total_bytes,
                    created_at=_to_text(self._clock()),
                    completed_at=None,
                    reconciled_at=None,
                )
            )

    def _mark_external_receipt_ambiguous(self, operation_id: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgeDeploymentReceiptRow, operation_id)
            if row is not None and row.state == "pending":
                row.state = "ambiguous"
                row.classification = "deployment_ambiguous"

    def _complete_external_receipt(self, operation_id: str, receipt: Any) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgeDeploymentReceiptRow, operation_id)
            if row is None:
                raise McpWorkspaceDevelopmentError("deployment_ambiguous")
            row.state = "succeeded" if receipt.state == "succeeded" else "failed"
            row.classification = receipt.classification
            row.bridge_receipt_id = (
                None if receipt.receipt_id is None else str(receipt.receipt_id)
            )
            row.completed_at = (
                _to_text(self._clock())
                if receipt.completed_at is None
                else _to_text(receipt.completed_at)
            )

    def _run_execution(
        self, operation_id: UUID, digest: str, execution_kind: WorkspaceExecutionKind
    ) -> WorkspaceExecutionResult:
        operation = self._execution_required(operation_id)
        deployment = self._execution_deployment(operation)
        self._recheck_execution(operation, deployment)
        descriptor = self._workspace_descriptor(UUID(operation.workspace_id))
        unsupported_reason = copied_video_plugin_unsupported_reason(descriptor)
        if unsupported_reason is not None:
            self._finish_execution(
                operation.id,
                "failed",
                "execution_unsupported",
                digest=digest,
                result_classification=unsupported_reason,
            )
            raise McpWorkspaceDevelopmentError(
                "execution_unsupported", reason=unsupported_reason
            )
        if deployment.target_class == "external_development":
            return self._run_external_execution(
                operation, deployment, digest, descriptor
            )
        observation, profile, container_id = self._runtime_context(
            UUID(operation.target_id)
        )
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
                descriptor.dependencies,
                by_id,
                kodi_release=profile.kodi_release.canonical,
            )
            if mismatch is not None:
                raise McpWorkspaceDevelopmentError(
                    "execution_unsupported", reason="dependency_declaration_mismatch"
                )
            blocked = first_blocking_dependency(dependencies)
            if blocked is not None:
                raise McpWorkspaceDevelopmentError(
                    "execution_unsupported",
                    reason=dependency_failure_code(blocked.status),
                )
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
        if execution_kind is WorkspaceExecutionKind.SMOKE:
            self._mark_smoke_dispatch(operation_id, digest, container_id, deployment)
        else:
            self._update_execution(
                operation_id, phase="invoke", invocation_dispatched=True
            )
        self._failure("execution_dispatch_marker")
        smoke_cursor: str | None = None
        addon_exception: AddonExceptionDiagnostic | None = None
        if execution_kind is WorkspaceExecutionKind.SMOKE:
            try:
                captured = self._broker.capture_smoke_log_cursor(
                    execution_id=operation.id
                )
                if captured.container_id == container_id:
                    smoke_cursor = captured.cursor
            except Exception:
                # Observation has no authority over the fixed Kodi call.
                pass
        invocation = None
        execution_failure: KodiAddonSmokeJsonRpcError | None = None
        try:
            adapter = self._smoke_factory(observation, profile)
            if execution_kind is WorkspaceExecutionKind.SMOKE:
                invocation = adapter.invoke(operation.addon_id)
            else:
                adapter.invoke_negative_diagnostic()
                raise KodiAddonSmokeAmbiguousError(
                    "Kodi diagnostic returned without a correlated error"
                )
        except KodiAddonSmokeJsonRpcError as error:
            execution_failure = error
            if (
                execution_kind is WorkspaceExecutionKind.SMOKE
                and error.diagnostic is not None
            ):
                addon_exception = AddonExceptionDiagnostic(
                    status=SmokeDiagnosticStatus.UNAVAILABLE
                )
                if smoke_cursor is not None:
                    try:
                        observed = self._broker.read_smoke_log_window(
                            execution_id=operation.id, cursor=smoke_cursor
                        )
                        if observed.container_id == container_id:
                            addon_exception = observed.diagnostic
                    except Exception:
                        pass
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
                result_classification=(
                    "smoke_failed"
                    if execution_kind is WorkspaceExecutionKind.SMOKE
                    else "kodi_rpc_diagnostic_failed"
                ),
                diagnostic=execution_failure.diagnostic,
                addon_exception=addon_exception,
            )
            raise McpWorkspaceDevelopmentError(
                "execution_failed",
                diagnostic=execution_failure.diagnostic,
                addon_exception=addon_exception,
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

    def _run_external_execution(
        self,
        operation: McpWorkspaceExecutionOperationRow,
        deployment: McpWorkspaceDeploymentOperationRow,
        digest: str,
        _descriptor: Any,
    ) -> WorkspaceExecutionResult:
        targets = self._target_adapters
        bridges = self._external_bridges
        if targets is None or bridges is None:
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        self._update_execution(UUID(operation.id), phase="verify_pre")
        try:
            inventory = targets.addon_inventory(UUID(operation.target_id))
        except Exception as error:
            raise McpWorkspaceDevelopmentError("execution_not_deployed") from error
        item = next(
            (
                entry
                for entry in inventory.items
                if entry.addon_id == operation.addon_id
            ),
            None,
        )
        if (
            inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE
            or item is None
            or item.version != operation.addon_version
            or not item.enabled
            or item.kodi_type != "xbmc.python.pluginsource"
        ):
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        self._update_execution(
            UUID(operation.id), phase="invoke", invocation_dispatched=True
        )
        self._failure("execution_dispatch_marker")
        try:
            invocation = targets.fixed_execute(
                UUID(operation.target_id), operation.addon_id, UUID(operation.id)
            )
        except KodiAddonSmokeJsonRpcError as error:
            self._finish_execution(
                operation.id,
                "failed",
                "execution_failed",
                digest=digest,
                execution_classification="jsonrpc_error",
                result_classification="smoke_failed",
                diagnostic=error.diagnostic,
            )
            raise McpWorkspaceDevelopmentError(
                "execution_failed", diagnostic=error.diagnostic
            ) from error
        except (KodiAddonSmokeTimeoutError, KodiAddonSmokeAmbiguousError) as error:
            self._finish_execution(
                operation.id,
                "ambiguous",
                "execution_ambiguous",
                digest=digest,
                execution_classification="ambiguous",
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
        except Exception as error:
            self._finish_execution(
                operation.id,
                "ambiguous",
                "execution_ambiguous",
                digest=digest,
                execution_classification="ambiguous",
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
        self._update_execution(UUID(operation.id), phase="verify_post")
        try:
            observed = bridges.inspect_addon(
                UUID(operation.target_id), operation.addon_id
            )
        except ExternalBridgeServiceError as error:
            self._finish_execution(
                operation.id,
                "reconciliation_required",
                "execution_ambiguous",
                digest=digest,
                execution_classification="ambiguous",
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous") from error
        if (
            observed.manifest_digest != deployment.verified_destination_digest
            or observed.addon_version != operation.addon_version
            or not observed.enabled
        ):
            self._invalidate_external_baseline(
                UUID(operation.target_id), UUID(operation.development_source_id)
            )
            self._finish_execution(
                operation.id,
                "reconciliation_required",
                "execution_ambiguous",
                digest=digest,
                execution_classification="ambiguous",
            )
            raise McpWorkspaceDevelopmentError("execution_ambiguous")
        self._finish_execution(
            operation.id,
            "succeeded",
            None,
            digest=digest,
            execution_classification="directory_result",
            item_count=invocation.item_count,
            result_classification="smoke_passed",
        )
        return self._execution_result(self._execution_required(UUID(operation.id)))

    def _recheck_deployment(
        self, operation: McpWorkspaceDeploymentOperationRow
    ) -> None:
        with self._sessions() as session:
            _workspace, _validation, target = self._deployment_preflight_rows(
                session,
                UUID(operation.workspace_id),
                operation.generation,
                UUID(operation.target_id),
            )
            baseline = (
                None
                if target.target_class != "external_development"
                else session.get(
                    ExternalDevelopmentSourceBaselineRow,
                    {
                        "target_id": operation.target_id,
                        "development_source_id": operation.development_source_id,
                    },
                )
            )
            prior = (
                None
                if target.target_class != "external_development"
                else session.scalar(
                    select(McpWorkspaceDeploymentOperationRow)
                    .where(
                        McpWorkspaceDeploymentOperationRow.target_id
                        == operation.target_id,
                        McpWorkspaceDeploymentOperationRow.development_source_id
                        == operation.development_source_id,
                        McpWorkspaceDeploymentOperationRow.target_class
                        == "external_development",
                        McpWorkspaceDeploymentOperationRow.state == "succeeded",
                    )
                    .order_by(McpWorkspaceDeploymentOperationRow.completed_at.desc())
                )
            )
        if target.target_class != "external_development":
            return
        bridges = self._external_bridges
        if bridges is None or baseline is None:
            raise McpWorkspaceDevelopmentError("target_provenance_unknown")
        # An explicit Web re-baseline is a newer human trust decision and must
        # supersede older deployment lineage. Only a successful deployment
        # completed after that decision advances the expected target tree.
        prior_is_newer = (
            prior is not None
            and prior.completed_at is not None
            and prior.completed_at > baseline.established_at
        )
        expected = (
            prior.verified_destination_digest
            if prior_is_newer and prior is not None
            else baseline.manifest_digest
        )
        try:
            observed = bridges.inspect_addon(
                UUID(operation.target_id), operation.addon_id
            )
        except ExternalBridgeServiceError as error:
            raise McpWorkspaceDevelopmentError("target_provenance_unknown") from error
        if (
            expected is None
            or observed.manifest_digest != expected
            or observed.addon_id != operation.addon_id
        ):
            self._invalidate_external_baseline(
                UUID(operation.target_id), UUID(operation.development_source_id)
            )
            raise McpWorkspaceDevelopmentError("target_provenance_mismatch")

    def _invalidate_external_baseline(self, target_id: UUID, source_id: UUID) -> None:
        with self._sessions.begin() as session:
            row = session.get(
                ExternalDevelopmentSourceBaselineRow,
                {
                    "target_id": str(target_id),
                    "development_source_id": str(source_id),
                },
            )
            if row is not None and row.state == "current":
                now = self._clock()
                row.state = "stale"
                row.checked_at = _to_text(now)
                row.invalidated_at = _to_text(now)
                session.add(
                    ExternalBridgeEventRow(
                        id=str(uuid4()),
                        target_id=str(target_id),
                        bridge_id=row.bridge_id,
                        request_id=None,
                        event_kind="baseline_invalidated",
                        classification="external_tree_drift",
                        byte_size=row.total_bytes,
                        width=None,
                        height=None,
                        content_digest=row.manifest_digest.removeprefix("sha256:"),
                        occurred_at=_to_text(now),
                    )
                )

    def _recheck_execution(
        self,
        operation: McpWorkspaceExecutionOperationRow,
        deployment: McpWorkspaceDeploymentOperationRow,
    ) -> None:
        with self._sessions() as session:
            successes = list(
                session.scalars(
                    select(McpWorkspaceDeploymentOperationRow.id)
                    .where(
                        McpWorkspaceDeploymentOperationRow.workspace_id
                        == operation.workspace_id,
                        McpWorkspaceDeploymentOperationRow.target_id
                        == operation.target_id,
                        McpWorkspaceDeploymentOperationRow.generation
                        == operation.generation,
                        McpWorkspaceDeploymentOperationRow.state == "succeeded",
                    )
                    .limit(2)
                )
            )
            if successes != [deployment.id]:
                raise McpWorkspaceDevelopmentError("execution_ambiguous")
            workspace = AgentWorkspaceRepository(session).get(
                UUID(operation.workspace_id)
            )
            target = session.get(TargetRow, operation.target_id)
            instance = ManagedInstanceRepository(session).get(UUID(operation.target_id))
            attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
                UUID(operation.target_id), UUID(operation.development_source_id)
            )
            external_baseline = session.get(
                ExternalDevelopmentSourceBaselineRow,
                {
                    "target_id": operation.target_id,
                    "development_source_id": operation.development_source_id,
                },
            )
            repository_link = (
                session.get(RepositoryWorkspaceDeploymentRow, deployment.id)
                if deployment.deployment_mode == "repository"
                else None
            )
            pairing = (
                session.get(ExternalBridgePairingRow, operation.target_id)
                if deployment.deployment_mode == "repository"
                else None
            )
        if (
            workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or workspace.generation != operation.generation
            or str(workspace.development_source_id) != operation.development_source_id
            or target is None
            or deployment.state != "succeeded"
            or deployment.workspace_id != operation.workspace_id
            or deployment.target_id != operation.target_id
            or deployment.generation != operation.generation
            or deployment.addon_id != operation.addon_id
            or deployment.addon_version != operation.addon_version
            or not deployment.addon_enabled
        ):
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        if target.target_type is TargetType.MANAGED:
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or attachment is None
                or deployment.target_class != "internal_lab"
            ):
                raise McpWorkspaceDevelopmentError("execution_not_deployed")
            return
        if (
            target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or deployment.target_class != "external_development"
            or (
                deployment.deployment_mode == "direct"
                and (external_baseline is None or external_baseline.state != "current")
            )
            or deployment.verified_destination_digest is None
            or self._external_bridges is None
            or (
                deployment.deployment_mode == "repository"
                and (
                    repository_link is None
                    or repository_link.state != "succeeded"
                    or pairing is None
                    or pairing.pinned_bridge_id != repository_link.bridge_id
                    or pairing.pairing_state.value != "paired"
                )
            )
        ):
            raise McpWorkspaceDevelopmentError("execution_not_deployed")
        try:
            observed = self._external_bridges.inspect_addon(
                UUID(operation.target_id), operation.addon_id
            )
        except ExternalBridgeServiceError as error:
            raise McpWorkspaceDevelopmentError("execution_not_deployed") from error
        if (
            observed.manifest_digest != deployment.verified_destination_digest
            or observed.addon_version != operation.addon_version
            or not observed.enabled
        ):
            if deployment.deployment_mode == "direct":
                self._invalidate_external_baseline(
                    UUID(operation.target_id), UUID(operation.development_source_id)
                )
            raise McpWorkspaceDevelopmentError("target_provenance_mismatch")

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
            deployments = DevelopmentSourceDeploymentRepository(session)
            baseline = deployments.get(
                UUID(row.target_id), UUID(row.development_source_id)
            )
            if not (
                result.result_classification == "unchanged"
                and baseline is not None
                and baseline.confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                and baseline.runtime_revision_id == UUID(row.runtime_revision_id)
                and baseline.trusted_container_id == result.container_id
                and baseline.source_manifest_digest == result.source_manifest_digest
                and baseline.verified_destination_digest
                == result.verified_destination_digest
            ):
                deployments.mark_confidence(
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

    def _mark_smoke_dispatch(
        self,
        operation_id: UUID,
        digest: str,
        container_id: str,
        deployment: McpWorkspaceDeploymentOperationRow,
    ) -> None:
        """Persist trusted runtime identity with the irreversible dispatch marker."""

        with self._sessions.begin() as session:
            operation = session.get(
                McpWorkspaceExecutionOperationRow, str(operation_id)
            )
            instance = (
                None
                if operation is None
                else session.get(ManagedInstanceRow, operation.target_id)
            )
            lease = (
                None
                if operation is None
                else ManagedInstanceMutationLeaseRepository(session).current(
                    UUID(operation.target_id)
                )
            )
            if (
                operation is None
                or operation.state != "running"
                or operation.execution_kind != WorkspaceExecutionKind.SMOKE.value
                or lease is None
                or lease.operation_id != str(operation_id)
                or lease.lease_token_digest != digest
            ):
                raise McpWorkspaceDevelopmentError("execution_ambiguous")
            operation.phase = "invoke"
            operation.invocation_dispatched = True
            if (
                instance is not None
                and deployment.target_class == "internal_lab"
                and instance.runtime_revision_id == deployment.runtime_revision_id
                and len(container_id) == 64
                and all(character in "0123456789abcdef" for character in container_id)
            ):
                operation.original_container_id = container_id
                operation.original_runtime_state_revision = (
                    instance.runtime_state_revision
                )

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
                if row.target_class == "internal_lab":
                    mutation.release_for_operation(UUID(row.target_id), UUID(row.id))
                heavy.release_for_operation(UUID(row.id))
            else:
                if row.target_class == "internal_lab":
                    mutation.release(
                        UUID(row.target_id),
                        operation_id=UUID(row.id),
                        token_digest=digest,
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
        diagnostic: KodiJsonRpcDiagnostic | None = None,
        addon_exception: AddonExceptionDiagnostic | None = None,
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
            row.kodi_error_code = None if diagnostic is None else diagnostic.code
            row.kodi_error_message = None if diagnostic is None else diagnostic.message
            row.addon_exception_status = (
                None if addon_exception is None else addon_exception.status.value
            )
            row.addon_exception_class = (
                None if addon_exception is None else addon_exception.exception_class
            )
            row.addon_exception_file = (
                None if addon_exception is None else addon_exception.addon_file
            )
            row.addon_exception_line = (
                None if addon_exception is None else addon_exception.line_number
            )
            row.addon_exception_detail = (
                None if addon_exception is None else addon_exception.detail
            )
            deployment = session.get(
                McpWorkspaceDeploymentOperationRow, row.deployment_id
            )
            if deployment is not None and deployment.target_class == "internal_lab":
                leases = ManagedInstanceMutationLeaseRepository(session)
                if digest is None:
                    leases.release_for_operation(UUID(row.target_id), UUID(row.id))
                else:
                    leases.release(
                        UUID(row.target_id),
                        operation_id=UUID(row.id),
                        token_digest=digest,
                    )

    def _replay_deployment(
        self,
        row: McpWorkspaceDeploymentOperationRow,
        client_id: UUID,
        workspace_id: UUID,
        generation: int,
        target_id: UUID,
        deployment_mode: str,
    ) -> WorkspaceDeploymentResult:
        if (
            row.initiator_kind != "mcp"
            or row.client_id != str(client_id)
            or row.repository_origin
            != ("planned" if deployment_mode == "repository" else None)
            or row.workspace_id != str(workspace_id)
            or row.generation != generation
            or row.target_id != str(target_id)
            or row.deployment_mode != deployment_mode
        ):
            raise McpWorkspaceDevelopmentError("request_id_conflict")
        if row.state == "succeeded":
            return self._deployment_result(row)
        if row.deployment_mode == "repository" and row.state in {"pending", "running"}:
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
        execution_kind: WorkspaceExecutionKind,
    ) -> WorkspaceExecutionResult:
        with self._sessions() as session:
            request = self._request(
                session,
                UUID(row.client_id),
                UUID(row.request_id),
                EXECUTE_OPERATION_KIND,
            )
            fingerprint = _fingerprint(
                EXECUTE_OPERATION_KIND,
                workspace_id,
                generation,
                target_id,
                execution_kind=execution_kind,
            )
        if (
            request is None
            or request.parameter_fingerprint != fingerprint
            or row.client_id != request.client_id
            or row.workspace_id != str(workspace_id)
            or row.generation != generation
            or row.target_id != str(target_id)
        ):
            raise McpWorkspaceDevelopmentError("request_id_conflict")
        if row.state == "succeeded":
            return self._execution_result(row)
        if row.state == "failed" and row.failure_code:
            raise McpWorkspaceDevelopmentError(
                row.failure_code,
                reason=(
                    row.result_classification
                    if row.failure_code == "execution_unsupported"
                    and row.result_classification in EXECUTION_UNSUPPORTED_REASONS
                    else None
                ),
                diagnostic=(
                    KodiJsonRpcDiagnostic(
                        code=row.kodi_error_code, message=row.kodi_error_message
                    )
                    if row.failure_code == "execution_failed"
                    and row.execution_classification == "jsonrpc_error"
                    and row.kodi_error_code is not None
                    else None
                ),
                addon_exception=(
                    None
                    if row.addon_exception_status is None
                    else AddonExceptionDiagnostic(
                        status=SmokeDiagnosticStatus(row.addon_exception_status),
                        exception_class=row.addon_exception_class,
                        addon_file=row.addon_exception_file,
                        line_number=row.addon_exception_line,
                        detail=row.addon_exception_detail,
                    )
                ),
            )
        raise McpWorkspaceDevelopmentError("execution_ambiguous")

    def _deployment_result(
        self, row: McpWorkspaceDeploymentOperationRow
    ) -> WorkspaceDeploymentResult:
        started = _from_text(row.started_at)
        completed = _from_text(row.completed_at)
        if row.deployment_mode == "direct" and (
            started is None or completed is None or row.addon_enabled is None
        ):
            raise McpWorkspaceDevelopmentError("deployment_ambiguous")
        linked = None
        if row.deployment_mode == "repository":
            with self._sessions() as session:
                linked = session.get(RepositoryWorkspaceDeploymentRow, row.id)
        next_action = None
        if linked is not None and row.state == "running":
            if row.result_classification == "repository_unavailable":
                next_action = (
                    "Restore development repository availability, then recheck"
                )
            elif linked.state == "awaiting_approval":
                next_action = "Approve exact publication in Kodi Lab Web"
            elif linked.state == "publication_approved":
                next_action = "Retry approved publication in Kodi Lab Web"
            elif linked.state == "observed_identity":
                next_action = "Reconnect the bridge, then recheck the installed tree"
            elif linked.state in {"awaiting_kodi_update", "verified_tree"}:
                next_action = (
                    "Install from Kodi Lab Development, then recheck"
                    if linked.installed_before_version is None
                    else "Update in Kodi Add-ons, then recheck"
                )
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
            deployment_mode=row.deployment_mode,
            phase=row.phase,
            publication_id=None if linked is None else UUID(linked.publication_id),
            repository_id=None if linked is None else linked.repository_id,
            installation_kind=(
                None
                if linked is None
                else "existing"
                if linked.origin == "verified_existing"
                else "first_install"
                if linked.installed_before_version is None
                else "update"
            ),
            origin=None if linked is None else linked.origin,
            zip_sha256=None if linked is None else linked.zip_sha256,
            expected_manifest_digest=(
                None if linked is None else linked.expected_manifest_digest
            ),
            next_action=next_action,
            result_classification=row.result_classification or "verified",
            inventory_classification=row.inventory_classification,
            addon_enabled=row.addon_enabled,
            started_at=started,
            completed_at=completed,
        )

    def _repository_publication_id(self, operation_id: str) -> UUID | None:
        with self._sessions() as session:
            linked = session.get(RepositoryWorkspaceDeploymentRow, operation_id)
            return None if linked is None else UUID(linked.publication_id)

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
        repository_history_reference = exists().where(
            RepositoryWorkspaceDeploymentRow.operation_id
            == McpWorkspaceDeploymentOperationRow.id
        )
        expired_deployment_query = (
            select(McpWorkspaceDeploymentOperationRow.id)
            .where(
                McpWorkspaceDeploymentOperationRow.completed_at < cutoff,
                deployment_terminal_evidence_is_consistent_sql(),
                ~current_workspace_deployment,
                ~deployment_request_reference,
                ~deployment_promotion_reference,
                ~repository_history_reference,
            )
            .order_by(
                McpWorkspaceDeploymentOperationRow.completed_at,
                McpWorkspaceDeploymentOperationRow.id,
            )
            .limit(DEVELOPMENT_OPERATION_PRUNE_LIMIT)
        )
        expired_deployment_ids = list(session.scalars(expired_deployment_query))
        if expired_deployment_ids:
            session.execute(
                delete(ExternalBridgeDeploymentReceiptRow).where(
                    ExternalBridgeDeploymentReceiptRow.operation_id.in_(
                        expired_deployment_ids
                    )
                )
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

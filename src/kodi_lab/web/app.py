"""Explicit FastAPI application factory for the Phase 4 Web slice."""

# FastAPI intentionally represents dependencies and body metadata as defaults.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from python_multipart.multipart import MultipartParser, parse_options_header
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.activity import (
    ManagedInstanceActivityItem,
    ManagedInstanceActivityService,
)
from kodi_lab.application.addon_inventory import (
    InventoryAdapterFactory,
    ManagedInstanceAddonInventory,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.addon_repositories import (
    AddonRepositoryAdministrationService,
    AddonRepositoryCandidateRead,
    AddonRepositoryConflictError,
    AddonRepositoryExecutor,
    AddonRepositoryFetchCoordinator,
    AddonRepositoryFetchStatus,
    AddonRepositoryNotFoundError,
    AddonRepositoryRefreshCoordinator,
    AddonRepositoryRefreshStatus,
    AddonRepositoryServiceError,
    AddonRepositorySourceRead,
)
from kodi_lab.application.addon_zip_enablements import (
    AddonZipEnablementError,
    AddonZipEnablementOperationCoordinator,
    AddonZipEnablementService,
    AddonZipEnablementStatus,
)
from kodi_lab.application.addon_zip_imports import (
    AddonZipImportConflictError,
    AddonZipImportCoordinator,
    AddonZipImportExecutor,
)
from kodi_lab.application.addon_zip_installations import (
    AddonZipInstallationCoordinator,
    AddonZipInstallationError,
    AddonZipInstallationExecutor,
    AddonZipInstallationService,
    AddonZipInstallationServiceFactory,
    AddonZipInstallationStatus,
)
from kodi_lab.application.addon_zip_installations import (
    ReadinessCoordinatorFactory as AddonZipReadinessCoordinatorFactory,
)
from kodi_lab.application.addon_zip_presentation import (
    AddonZipArtifactSummary,
    AddonZipImportStatus,
    AddonZipPresentationService,
)
from kodi_lab.application.addon_zip_smoke_tests import (
    AddonZipSmokeTestConflictError,
    AddonZipSmokeTestError,
    AddonZipSmokeTestExecutor,
    AddonZipSmokeTestNotFoundError,
    AddonZipSmokeTestOperationCoordinator,
    AddonZipSmokeTestService,
    AddonZipSmokeTestServiceFactory,
    AddonZipSmokeTestStatus,
)
from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupExecutor,
    AgentWorkspaceCleanupService,
    AgentWorkspaceCleanupStatus,
)
from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionConflictError,
    AgentWorkspacePromotionError,
    AgentWorkspacePromotionOperationStatus,
    AgentWorkspacePromotionPlanRead,
    AgentWorkspacePromotionService,
    AgentWorkspacePromotionTargetRead,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceActivityRead,
    AgentWorkspaceChangesRead,
    AgentWorkspaceConflictError,
    AgentWorkspaceCreationExecutor,
    AgentWorkspaceCreationStatus,
    AgentWorkspaceError,
    AgentWorkspaceFileRead,
    AgentWorkspaceLifecycleActivityRead,
    AgentWorkspaceMutationRead,
    AgentWorkspaceNotFoundError,
    AgentWorkspaceRead,
    AgentWorkspaceService,
    AgentWorkspaceTreeEntryRead,
)
from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteRateLimiter,
    BrowserRemoteResult,
    BrowserRemoteService,
    RemoteActorContext,
    RemoteAdapterFactory,
)
from kodi_lab.application.development_source_attachments import (
    AttachedDevelopmentSourceRead,
    DevelopmentSourceAttachmentService,
)
from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentConflictError,
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentExecutor,
    DevelopmentSourceDeploymentOperationCoordinator,
    DevelopmentSourceDeploymentService,
    DevelopmentSourceDeploymentServiceFactory,
    DevelopmentSourceDeploymentStatus,
    DevelopmentSourceDeploymentUnsupportedError,
)
from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceEnablementError,
    DevelopmentSourceEnablementOperationCoordinator,
    DevelopmentSourceEnablementService,
    DevelopmentSourceEnablementStatus,
)
from kodi_lab.application.development_source_presentation import (
    DevelopmentSourceDetail,
    DevelopmentSourcePresentationService,
    DevelopmentSourceSummary,
)
from kodi_lab.application.development_source_smoke_tests import (
    DevelopmentSourceSmokeTestConflictError,
    DevelopmentSourceSmokeTestError,
    DevelopmentSourceSmokeTestExecutor,
    DevelopmentSourceSmokeTestNotFoundError,
    DevelopmentSourceSmokeTestOperationCoordinator,
    DevelopmentSourceSmokeTestService,
    DevelopmentSourceSmokeTestServiceFactory,
    DevelopmentSourceSmokeTestStatus,
)
from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceRead,
)
from kodi_lab.application.errors import (
    DevelopmentSourceAddonConflictError,
    DevelopmentSourceAlreadyAttachedError,
    DevelopmentSourceAttachmentError,
    DevelopmentSourceAttachmentValidationError,
    DevelopmentSourceConflictError,
    DevelopmentSourceDataValidationError,
    DevelopmentSourceIdentityChangedError,
    DevelopmentSourceNotAttachedError,
    DevelopmentSourceNotFoundError,
    ManagedInstanceNotFoundError,
    PersistentDevelopmentSourceAttachmentUnsupportedError,
    TargetNotFoundError,
)
from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalBridgeStatus,
    PairingTokenResult,
    ProductionHttpAcknowledgementRead,
    ReviewedBridgeBuildRead,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetEventRead,
    ExternalTargetRead,
    ProductionMutationControlRead,
)
from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteConflictError,
    ManagedInstanceDeleteNotFoundError,
    ManagedInstanceDeleteOperationCoordinator,
    ManagedInstanceDeleteStatus,
    ManagedInstanceDeleteUnsupportedError,
)
from kodi_lab.application.instance_deletions import (
    DisposableManagedInstanceDeletionService,
    ManagedInstanceDeleteExecutor,
    ManagedInstanceDeleteServiceFactory,
)
from kodi_lab.application.instance_history import (
    DeletedManagedInstanceHistoryNotFoundError,
    DeletedManagedInstanceHistoryService,
    ManagedInstanceDetailService,
)
from kodi_lab.application.lifecycle_operations import (
    LifecycleOperationConflictError,
    ManagedInstanceLifecycleOperationCoordinator,
)
from kodi_lab.application.live_display import (
    LiveDisplayError,
    LiveDisplayFrameSkipped,
    ManagedLiveDisplayService,
)
from kodi_lab.application.mcp_administration import (
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpClientRead,
    McpCredentialRead,
    McpEligibleWorkspaceRead,
    McpIssuedCredential,
    McpPresetRead,
    McpProductionScopeRead,
    McpTargetPolicyRead,
    McpWorkspaceBindingRead,
)
from kodi_lab.application.mcp_promotion_requests import McpPromotionRequestService
from kodi_lab.application.mcp_remote_actions import McpRemoteActionReceiptService
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.application.mcp_workspace_development import (
    ExternalBaselineResult,
    McpWorkspaceDevelopmentError,
    McpWorkspaceDevelopmentService,
)
from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceCreationService
from kodi_lab.application.mcp_workspace_retention import (
    McpWorkspaceRetentionService,
)
from kodi_lab.application.mutations import (
    ManagedInstanceMutationConflictError,
    ManagedInstanceMutationCoordinator,
)
from kodi_lab.application.production_inspection import ProductionInspectionError
from kodi_lab.application.production_navigation import (
    ProductionNativeControlAcknowledgementRead,
    ProductionNativeControlAcknowledgementService,
)
from kodi_lab.application.production_screenshots import ProductionScreenshotService
from kodi_lab.application.production_transient_approvals import (
    ProductionApprovalError,
    ProductionApprovalRead,
    ProductionTransientApprovalService,
)
from kodi_lab.application.promotion_verifications import (
    PromotionVerificationError,
    PromotionVerificationService,
    PromotionVerificationStatus,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reconciliation import (
    CoordinationCorruptionError,
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ManagedInstanceReconciliationStatus,
    ReconciliationAction,
    SnapshotReconciliationService,
    SnapshotReconciliationStatus,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.repository_replacements import (
    RepositoryReplacementCoordinator,
    RepositoryReplacementError,
    RepositoryReplacementExecutor,
    RepositoryReplacementPlanRead,
    RepositoryReplacementService,
    RepositoryReplacementServiceFactory,
    RepositoryReplacementStatus,
    RepositoryReplacementUnsupportedError,
)
from kodi_lab.application.reset import DisposableInstanceResetService
from kodi_lab.application.reset_activity import ResetActivityRegistry
from kodi_lab.application.reset_operations import (
    QueuedResetOperation,
    ResetExecutor,
    ResetOperationConflictError,
    ResetOperationCoordinator,
    ResetServiceFactory,
)
from kodi_lab.application.reset_status import (
    ManagedInstanceResetStatus,
    ResetStatusService,
)
from kodi_lab.application.runtime_lifecycle import (
    LifecycleConflictError,
    ManagedRuntimeLifecycleService,
    ReadinessCoordinatorFactory,
)
from kodi_lab.application.runtime_matrix import (
    RuntimeMatrixResponse,
    RuntimeMatrixService,
    TargetRuntimeSelection,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.screenshots import (
    ManagedScreenshotCaptureResult,
    ManagedScreenshotService,
    ManagedScreenshotStatus,
    ScreenshotServiceError,
)
from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteConflictError,
    SnapshotDeleteNotFoundError,
    SnapshotDeleteOperationCoordinator,
    SnapshotDeleteStatus,
    SnapshotDeleteUnsupportedError,
)
from kodi_lab.application.snapshot_deletions import (
    DisposableSnapshotDeletionService,
    SnapshotDeleteExecutor,
    SnapshotDeleteServiceFactory,
)
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateConflictError,
    SnapshotCreateOperationCoordinator,
    SnapshotCreateStatus,
    SnapshotCreateUnsupportedError,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreConflictError,
    SnapshotRestoreOperationCoordinator,
    SnapshotRestoreStatus,
    SnapshotRestoreUnsupportedError,
    SnapshotSourceInstanceDeletedError,
)
from kodi_lab.application.snapshot_restores import (
    DisposableSnapshotRestoreService,
    SnapshotRestoreExecutor,
    SnapshotRestoreServiceFactory,
)
from kodi_lab.application.snapshot_status import (
    SnapshotHistoryItem,
    SnapshotStatusService,
)
from kodi_lab.application.snapshots import (
    DisposableSnapshotCreationService,
    SnapshotCreateExecutor,
    SnapshotServiceFactory,
)
from kodi_lab.auth import (
    AuthenticatedWebSession,
    LoginThrottler,
    WebAuthenticationService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.config import Settings
from kodi_lab.development import (
    MAX_COMPRESSED_BYTES,
    AddonRepositoryError,
    AddonZipValidationError,
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
    create_upload_stage,
    finish_upload_stage,
    remove_upload_stage,
    write_upload_chunk,
)
from kodi_lab.domain import (
    MAX_DEVELOPMENT_SOURCE_LOCATOR,
    MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME,
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    ManagedInstanceDeleteOperationState,
    SnapshotSourceBinding,
    SnapshotState,
    TargetType,
    normalize_development_source_logical_name,
)
from kodi_lab.kodi.bridge import ExternalBridgeClient
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.kodi.readiness import KodiReadinessCoordinator
from kodi_lab.kodi.remote import BrowserRemoteAction, KodiBrowserRemoteAdapter
from kodi_lab.mcp.authorization import (
    InternalManagedTargetAuthorityProvider,
    McpAuthorizationService,
)
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.mcp.transport import McpHttpTransport, build_mcp_http_transport
from kodi_lab.storage.models import McpWorkspaceDeploymentOperationRow
from kodi_lab.storage.repositories import ManagedInstanceRepository, SnapshotRepository
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    ExternalCredentialStore,
    decode_master_key,
)
from kodi_lab.workspace import AgentWorkspaceStorageError

_WEB_ROOT = Path(__file__).parent
_MAX_WORKSPACE_MUTATION_REQUEST_BYTES = 16 * 1024 * 1024


class _WorkspaceRequestTooLarge(Exception):
    pass


class _WorkspaceMutationBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        limited = (
            scope["type"] == "http"
            and scope.get("method") in {"POST", "PUT"}
            and isinstance(path, str)
            and path.startswith("/api/agent-workspaces/")
            and path.endswith(("/files", "/patches"))
        )
        if not limited:
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_WORKSPACE_MUTATION_REQUEST_BYTES:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _MAX_WORKSPACE_MUTATION_REQUEST_BYTES:
                    raise _WorkspaceRequestTooLarge
            return message

        try:
            await self._app(scope, bounded_receive, send)
        except _WorkspaceRequestTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={
                "code": "agent_workspace_request_too_large",
                "message": "Workspace mutation request is too large.",
            },
        )
        await response(scope, receive, send)


class EmptyActionRequest(BaseModel):
    """Lifecycle requests deliberately contain no browser authority."""

    model_config = ConfigDict(extra="forbid")


class BrowserRemoteRequest(BaseModel):
    """One closed semantic action and no Kodi/network/runtime authority."""

    model_config = ConfigDict(extra="forbid")

    action: BrowserRemoteAction


class DevelopmentSourceRegistrationRequest(BaseModel):
    """Bounded logical registration fields with no host-path authority."""

    model_config = ConfigDict(extra="forbid")

    logical_name: str = Field(
        min_length=1, max_length=MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME
    )
    relative_locator: str = Field(
        min_length=1, max_length=MAX_DEVELOPMENT_SOURCE_LOCATOR
    )


class AgentWorkspaceCreationRequest(BaseModel):
    """The caller selects only one registered DevelopmentSource UUID."""

    model_config = ConfigDict(extra="forbid")

    development_source_id: UUID


class AgentWorkspaceCreationAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AgentWorkspaceCreationStatus
    status_url: str
    workspace_url: str


class AgentWorkspaceCleanupConfirmationRequest(BaseModel):
    """One confirmation bit and no filesystem authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class AgentWorkspaceCleanupAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AgentWorkspaceCleanupStatus
    status_url: str


class AgentWorkspaceTextWriteRequest(BaseModel):
    """One bounded path-and-text mutation with an optimistic generation."""

    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=2 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_utf8_bound(self) -> AgentWorkspaceTextWriteRequest:
        try:
            encoded = self.content.encode("utf-8", "strict")
        except UnicodeEncodeError as error:
            raise ValueError("content must be UTF-8 text") from error
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("content exceeds the 2 MiB limit")
        return self


class AgentWorkspaceExactPatchRequest(BaseModel):
    """Exact full-file preimage replacement; no offsets or fuzzy matching."""

    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=512)
    expected_text: str = Field(max_length=2 * 1024 * 1024)
    replacement_text: str = Field(max_length=2 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_utf8_bounds(self) -> AgentWorkspaceExactPatchRequest:
        for value in (self.expected_text, self.replacement_text):
            try:
                encoded = value.encode("utf-8", "strict")
            except UnicodeEncodeError as error:
                raise ValueError("patch fields must be UTF-8 text") from error
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError("patch field exceeds the 2 MiB limit")
        return self


class AddonRepositoryRegistrationRequest(BaseModel):
    """Closed repository registration with no credentials or fetch controls."""

    model_config = ConfigDict(extra="forbid")

    logical_name: str = Field(min_length=1, max_length=120)
    index_url: str = Field(min_length=1, max_length=2304)


class ResetConfirmationRequest(BaseModel):
    """One explicit confirmation bit, with no runtime or Docker authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class DevelopmentSourceDeploymentAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: DevelopmentSourceDeploymentStatus
    status_url: str


class DevelopmentSourceSmokeTestAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: DevelopmentSourceSmokeTestStatus
    status_url: str


class AddonZipImportAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AddonZipImportStatus
    status_url: str


class AddonZipInstallationAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AddonZipInstallationStatus
    status_url: str


class AddonZipSmokeTestAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AddonZipSmokeTestStatus
    status_url: str


class AddonRepositoryRefreshAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AddonRepositoryRefreshStatus
    status_url: str


class AddonRepositoryFetchAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: AddonRepositoryFetchStatus
    status_url: str


class RepositoryReplacementConfirmationRequest(BaseModel):
    """One immediate confirmation bit and no replacement authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class RepositoryReplacementAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: RepositoryReplacementStatus
    status_url: str


class AgentWorkspacePromotionConfirmationRequest(BaseModel):
    """One immediate confirmation bit and no source-write authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotCreateRequest(BaseModel):
    """Optional display metadata with no storage or runtime authority."""

    model_config = ConfigDict(extra="forbid")
    label: str | None = None


class SnapshotCreateAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotCreateStatus


class SnapshotRestoreRequest(BaseModel):
    """One destructive confirmation bit and no artifact or runtime authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotRestoreAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotRestoreStatus


class SnapshotDeleteRequest(BaseModel):
    """One permanent-deletion confirmation bit and no storage authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotDeleteAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotDeleteStatus


class ManagedInstanceDeleteRequest(BaseModel):
    """Explicit destructive choice with no Snapshot-list or Docker authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    preserve_snapshots: StrictBool


class McpClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    preset_id: UUID


class McpPresetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    template_preset_id: UUID


class McpClientRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)


class McpClientEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class McpPresetAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: UUID


class McpProductionScopeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_generation: int = Field(ge=1)


class McpProductionVisualScopeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_generation: int = Field(ge=1)
    bridge_trust_revision: int = Field(ge=1)


class McpCredentialIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expiration_seconds: int | None = Field(default=24 * 60 * 60, ge=60, le=31 * 86400)


class ExternalTargetRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_name: str = Field(min_length=1, max_length=120)
    protocol_kind: ExternalProtocolKind
    host: str = Field(min_length=2, max_length=64)
    port: int = Field(ge=1, le=65_535)
    authentication_mode: ExternalAuthenticationMode
    configured_capabilities: set[str] = Field(min_length=1, max_length=6)
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class ExternalTargetEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_kind: ExternalProtocolKind
    host: str = Field(min_length=2, max_length=64)
    port: int = Field(ge=1, le=65_535)


class ExternalTargetCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authentication_mode: ExternalAuthenticationMode
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class ExternalTargetConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    observation_id: UUID


class ExternalTargetEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class ProductionDesignationConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    observation_id: UUID
    acknowledgement: Literal[
        "I confirm this is a production Kodi target and mutations may affect "
        "real user state."
    ]


class ProductionMutationBlockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocked: StrictBool


class ProductionTargetMutationSuspensionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspended: StrictBool


class ProductionApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class ProductionApprovalConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    action: BrowserRemoteAction


class ExternalBridgePairingTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    bridge_port: int = Field(ge=1, le=65_535)


class ExternalBridgeConfirmedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class ProductionBridgeConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    acknowledgement: Literal[
        "I confirm this bridge is installed on the production Kodi target and "
        "the displayed bridge identity and package provenance match the reviewed "
        "package."
    ]


class ReviewedBridgeBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    zip_sha256: str = Field(min_length=64, max_length=64)
    addon_id: str = Field(min_length=1, max_length=128)
    addon_version: str = Field(min_length=1, max_length=64)
    protocol_version: str = Field(min_length=1, max_length=16)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=2)
    supported_kodi_versions: tuple[str, ...] = Field(min_length=1, max_length=8)
    source_reference: str = Field(min_length=1, max_length=512)


class ProductionBridgeHttpAcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    bridge_port: int = Field(ge=1, le=65_535)
    acknowledgement: Literal[
        "I acknowledge that private-LAN HTTP protects authenticity and integrity "
        "but does not provide screenshot confidentiality."
    ]


class ProductionNativeControlHttpAcknowledgementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    acknowledgement: Literal[
        "I acknowledge that native JSON-RPC production control is sent over "
        "HTTP; bridge HMAC does not protect that separate transport, and HTTP "
        "provides no transport confidentiality or integrity beyond the network."
    ]


class ExternalBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    source_id: UUID


class ExternalDeploymentReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


@dataclass(frozen=True, slots=True)
class WebApplicationContext:
    settings: Settings
    auth: WebAuthenticationService
    throttler: LoginThrottler
    status: ManagedRuntimeStatusService
    lifecycle: ManagedRuntimeLifecycleService
    matrix: RuntimeMatrixService
    readiness: ReadinessTaskRegistry
    reset_activity: ResetActivityRegistry
    reset_status: ResetStatusService
    reset_operations: ResetOperationCoordinator
    reset_executor: ResetExecutor
    snapshot_status: SnapshotStatusService
    snapshot_operations: SnapshotCreateOperationCoordinator
    snapshot_executor: SnapshotCreateExecutor
    snapshot_restore_operations: SnapshotRestoreOperationCoordinator
    snapshot_restore_executor: SnapshotRestoreExecutor
    snapshot_delete_operations: SnapshotDeleteOperationCoordinator
    snapshot_delete_executor: SnapshotDeleteExecutor
    instance_delete_operations: ManagedInstanceDeleteOperationCoordinator
    instance_delete_executor: ManagedInstanceDeleteExecutor
    reconciliation: ManagedInstanceReconciliationService
    snapshot_reconciliation: SnapshotReconciliationService
    stale_dispatcher: StaleOperationReconciliationDispatcher
    lifecycle_operations: ManagedInstanceLifecycleOperationCoordinator
    activity: ManagedInstanceActivityService
    addon_inventory: ManagedInstanceAddonInventoryService
    instance_detail: ManagedInstanceDetailService
    deleted_history: DeletedManagedInstanceHistoryService
    development_sources: DevelopmentSourcePresentationService
    development_source_administration: DevelopmentSourceAdministrationService
    development_source_attachments: DevelopmentSourceAttachmentService
    development_source_deployment_operations: (
        DevelopmentSourceDeploymentOperationCoordinator
    )
    development_source_deployment_executor: DevelopmentSourceDeploymentExecutor
    development_source_enablement: DevelopmentSourceEnablementService
    development_source_smoke_test_operations: (
        DevelopmentSourceSmokeTestOperationCoordinator
    )
    development_source_smoke_test_executor: DevelopmentSourceSmokeTestExecutor
    addon_zips: AddonZipPresentationService
    addon_zip_imports: AddonZipImportCoordinator
    addon_zip_import_executor: AddonZipImportExecutor
    addon_zip_installations: AddonZipInstallationCoordinator
    addon_zip_installation_executor: AddonZipInstallationExecutor
    addon_zip_enablement: AddonZipEnablementService
    addon_zip_smoke_test_operations: AddonZipSmokeTestOperationCoordinator
    addon_zip_smoke_test_executor: AddonZipSmokeTestExecutor
    addon_repositories: AddonRepositoryAdministrationService
    addon_repository_refreshes: AddonRepositoryRefreshCoordinator
    addon_repository_refresh_executor: AddonRepositoryExecutor
    addon_repository_fetches: AddonRepositoryFetchCoordinator
    addon_repository_fetch_executor: AddonRepositoryExecutor
    repository_replacements: RepositoryReplacementCoordinator
    repository_replacement_executor: RepositoryReplacementExecutor
    agent_workspaces: AgentWorkspaceService
    agent_workspace_executor: AgentWorkspaceCreationExecutor
    agent_workspace_cleanups: AgentWorkspaceCleanupService
    agent_workspace_cleanup_executor: AgentWorkspaceCleanupExecutor
    mcp_workspace_retention: McpWorkspaceRetentionService
    mcp_workspace_development: McpWorkspaceDevelopmentService
    mcp_promotion_requests: McpPromotionRequestService
    agent_workspace_promotions: AgentWorkspacePromotionService
    promotion_verifications: PromotionVerificationService
    screenshots: ManagedScreenshotService
    browser_remote: BrowserRemoteService
    browser_remote_rate_limiter: BrowserRemoteRateLimiter
    live_display: ManagedLiveDisplayService
    external_targets: ExternalTargetAdministrationService
    production_targets: ExternalTargetAdministrationService
    external_bridges: ExternalBridgeService
    production_native_control_acknowledgements: (
        ProductionNativeControlAcknowledgementService
    )
    production_transient_approvals: ProductionTransientApprovalService
    mcp_administration: McpAdministrationService
    mcp_transport: McpHttpTransport


def create_app(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    broker: Any,
    readiness: ReadinessTaskRegistry | None = None,
    reset_activity: ResetActivityRegistry | None = None,
    auth: WebAuthenticationService | None = None,
    throttler: LoginThrottler | None = None,
    coordinator_factory: ReadinessCoordinatorFactory | None = None,
    reset_service_factory: ResetServiceFactory | None = None,
    snapshot_service_factory: SnapshotServiceFactory | None = None,
    snapshot_restore_service_factory: SnapshotRestoreServiceFactory | None = None,
    snapshot_delete_service_factory: SnapshotDeleteServiceFactory | None = None,
    instance_delete_service_factory: ManagedInstanceDeleteServiceFactory | None = None,
    development_source_deployment_service_factory: (
        DevelopmentSourceDeploymentServiceFactory | None
    ) = None,
    addon_inventory_adapter_factory: InventoryAdapterFactory | None = None,
    development_source_enablement_service: DevelopmentSourceEnablementService
    | None = None,
    development_source_smoke_test_service_factory: (
        DevelopmentSourceSmokeTestServiceFactory | None
    ) = None,
    addon_zip_installation_service_factory: (
        AddonZipInstallationServiceFactory | None
    ) = None,
    addon_zip_readiness_factory: AddonZipReadinessCoordinatorFactory | None = None,
    addon_zip_enablement_service: AddonZipEnablementService | None = None,
    addon_zip_smoke_test_service_factory: AddonZipSmokeTestServiceFactory | None = None,
    development_source_administration_service: (
        DevelopmentSourceAdministrationService | None
    ) = None,
    development_source_attachment_service: DevelopmentSourceAttachmentService
    | None = None,
    addon_repository_administration_service: (
        AddonRepositoryAdministrationService | None
    ) = None,
    addon_repository_refresh_coordinator: AddonRepositoryRefreshCoordinator
    | None = None,
    addon_repository_fetch_coordinator: AddonRepositoryFetchCoordinator | None = None,
    repository_replacement_service_factory: (
        RepositoryReplacementServiceFactory | None
    ) = None,
    agent_workspace_service: AgentWorkspaceService | None = None,
    agent_workspace_promotion_service: AgentWorkspacePromotionService | None = None,
    promotion_verification_service: PromotionVerificationService | None = None,
    agent_workspace_cleanup_service: AgentWorkspaceCleanupService | None = None,
    managed_screenshot_service: ManagedScreenshotService | None = None,
    browser_remote_service: BrowserRemoteService | None = None,
    browser_remote_rate_limiter: BrowserRemoteRateLimiter | None = None,
    browser_remote_adapter_factory: RemoteAdapterFactory | None = None,
    managed_live_display_service: ManagedLiveDisplayService | None = None,
    external_target_service: ExternalTargetAdministrationService | None = None,
    production_target_service: ExternalTargetAdministrationService | None = None,
    external_bridge_service: ExternalBridgeService | None = None,
    external_target_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build an app entirely from injected storage and typed broker boundaries."""

    task_registry = readiness or ReadinessTaskRegistry()
    reset_registry = reset_activity or ResetActivityRegistry()
    stale_dispatcher = StaleOperationReconciliationDispatcher(
        session_factory,
        broker=broker,
        readiness=task_registry,
        data_root=settings.data_root,
    )
    reconciliation = ManagedInstanceReconciliationService(
        session_factory,
        broker=broker,
        data_root=settings.data_root,
        readiness=task_registry,
    )
    snapshot_reconciliation = SnapshotReconciliationService(
        session_factory, data_root=settings.data_root
    )
    lifecycle_operations = ManagedInstanceLifecycleOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
    )
    reset_operations = ResetOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
    )
    status_service = ManagedRuntimeStatusService(
        session_factory, broker, task_registry, reconciliation
    )
    screenshots = managed_screenshot_service or ManagedScreenshotService(
        session_factory,
        broker,
        status_service,
        reconciliation,
        data_root=settings.data_root,
    )
    addon_inventory = ManagedInstanceAddonInventoryService(
        session_factory,
        broker,
        adapter_factory=addon_inventory_adapter_factory,
    )
    browser_remote = browser_remote_service or BrowserRemoteService(
        session_factory,
        broker,
        status_service,
        reconciliation,
        screenshots,
        adapter_factory=(browser_remote_adapter_factory or KodiBrowserRemoteAdapter),
    )
    remote_rate_limiter = browser_remote_rate_limiter or BrowserRemoteRateLimiter()
    agent_workspaces = agent_workspace_service or AgentWorkspaceService(
        session_factory,
        data_root=settings.data_root,
        development_root=settings.development_root,
    )
    agent_workspace_executor = AgentWorkspaceCreationExecutor(agent_workspaces)
    agent_workspace_cleanups = (
        agent_workspace_cleanup_service
        or AgentWorkspaceCleanupService(session_factory, data_root=settings.data_root)
    )
    agent_workspace_cleanup_executor = AgentWorkspaceCleanupExecutor(
        agent_workspace_cleanups
    )
    mcp_workspace_development = McpWorkspaceDevelopmentService(
        session_factory,
        broker,
        addon_inventory,
        agent_workspaces,
        data_root=settings.data_root,
    )
    mcp_workspace_retention = McpWorkspaceRetentionService(
        session_factory,
        agent_workspaces,
        agent_workspace_cleanups,
        agent_workspace_cleanup_executor.submit,
        data_root=settings.data_root,
        development_blocker=mcp_workspace_development.retention_blocker,
    )
    mcp_workspace_creation = McpWorkspaceCreationService(
        session_factory, agent_workspaces, agent_workspace_executor.submit
    )
    mcp_administration = McpAdministrationService(
        session_factory,
        retention=mcp_workspace_retention,
        development=mcp_workspace_development,
    )
    external_endpoint_policy = ExternalEndpointPolicy(
        allowed_ports=settings.external_target_allowed_ports,
        ipv6_ula_networks=settings.external_target_ipv6_ula_networks,
        test_loopback_enabled=settings.external_target_test_loopback_enabled,
    )
    external_credential_store = ExternalCredentialStore(
        settings.data_root / "secrets" / "external-targets",
        master_key=decode_master_key(
            None
            if settings.external_target_master_key is None
            else settings.external_target_master_key.get_secret_value()
        ),
        key_version=settings.external_target_master_key_version,
    )
    external_bridge_endpoint_policy = ExternalEndpointPolicy(
        allowed_ports=settings.external_bridge_allowed_ports,
        ipv6_ula_networks=settings.external_target_ipv6_ula_networks,
        test_loopback_enabled=settings.external_target_test_loopback_enabled,
    )
    external_bridge_secret_store = ExternalBridgeSecretStore(
        settings.data_root / "secrets" / "external-bridges",
        master_key=decode_master_key(
            None
            if settings.external_target_master_key is None
            else settings.external_target_master_key.get_secret_value()
        ),
        key_version=settings.external_target_master_key_version,
    )
    external_bridges = external_bridge_service or ExternalBridgeService(
        session_factory,
        endpoint_policy=external_bridge_endpoint_policy,
        secret_store=external_bridge_secret_store,
        client=ExternalBridgeClient(transport=external_target_transport),
    )
    external_targets = external_target_service or ExternalTargetAdministrationService(
        session_factory,
        endpoint_policy=external_endpoint_policy,
        credential_store=external_credential_store,
    )
    production_targets = (
        production_target_service
        or ExternalTargetAdministrationService(
            session_factory,
            endpoint_policy=external_endpoint_policy,
            credential_store=external_credential_store,
            target_type=TargetType.EXTERNAL_PRODUCTION,
        )
    )
    mcp_registry = build_mcp_tool_registry()
    managed_target_authority = InternalManagedTargetAuthorityProvider(
        session_factory,
        reconciliation=reconciliation,
        screenshots=screenshots,
        status=status_service,
    )
    mcp_target_adapters = KodiTargetAdapterService(
        session_factory,
        managed_status=status_service,
        managed_inventory=addon_inventory,
        managed_remote=browser_remote,
        managed_screenshots=screenshots,
        managed_authority=managed_target_authority,
        external_bridges=external_bridges,
        endpoint_policy=external_endpoint_policy,
        credential_store=external_credential_store,
        external_transport=external_target_transport,
    )
    mcp_workspace_development.configure_external(
        bridges=external_bridges,
        targets=mcp_target_adapters,
    )
    remote_receipts = McpRemoteActionReceiptService(
        session_factory, mcp_target_adapters, remote_rate_limiter
    )
    production_transient_approvals = ProductionTransientApprovalService(
        session_factory,
        remote_receipts,
        preflight=mcp_target_adapters.preflight_production_remote,
    )
    mcp_authorization = McpAuthorizationService(
        session_factory,
        mcp_registry,
        targets=mcp_target_adapters,
    )
    production_screenshots = ProductionScreenshotService(
        session_factory, external_bridges
    )
    production_native_control_acknowledgements = (
        ProductionNativeControlAcknowledgementService(session_factory)
    )
    live_display = managed_live_display_service or ManagedLiveDisplayService(
        broker, status_service, reconciliation, screenshots
    )
    reset_status_service = ResetStatusService(
        session_factory, broker, reset_registry, reset_operations
    )
    service_factory = reset_service_factory or (
        lambda observer: DisposableInstanceResetService(
            session_factory, broker, activity=observer
        )
    )
    reset_executor = ResetExecutor(reset_operations, reset_registry, service_factory)
    snapshot_operations = SnapshotCreateOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
    )
    snapshot_factory = snapshot_service_factory or (
        lambda lease: DisposableSnapshotCreationService(
            session_factory,
            broker,
            snapshot_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_executor = SnapshotCreateExecutor(snapshot_operations, snapshot_factory)
    snapshot_restore_operations = SnapshotRestoreOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
        snapshot_reconciliation=snapshot_reconciliation,
    )
    snapshot_restore_factory = snapshot_restore_service_factory or (
        lambda lease: DisposableSnapshotRestoreService(
            session_factory,
            broker,
            snapshot_restore_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_restore_executor = SnapshotRestoreExecutor(
        snapshot_restore_operations, snapshot_restore_factory
    )
    snapshot_delete_operations = SnapshotDeleteOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        snapshot_reconciliation=snapshot_reconciliation,
    )
    snapshot_delete_factory = snapshot_delete_service_factory or (
        lambda lease: DisposableSnapshotDeletionService(
            session_factory,
            broker,
            snapshot_delete_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_delete_executor = SnapshotDeleteExecutor(
        snapshot_delete_operations, snapshot_delete_factory
    )
    instance_delete_operations = ManagedInstanceDeleteOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
        snapshot_reconciliation=snapshot_reconciliation,
    )
    instance_delete_factory = instance_delete_service_factory or (
        lambda lease: DisposableManagedInstanceDeletionService(
            session_factory,
            broker,
            task_registry,
            instance_delete_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    instance_delete_executor = ManagedInstanceDeleteExecutor(
        instance_delete_operations, instance_delete_factory
    )
    activity = ManagedInstanceActivityService(session_factory)
    snapshot_status = SnapshotStatusService(session_factory)
    development_sources = DevelopmentSourcePresentationService(session_factory)
    development_source_validator = DevelopmentSourceValidator(settings.development_root)
    development_source_administration = (
        development_source_administration_service
        or DevelopmentSourceAdministrationService(
            session_factory, development_source_validator
        )
    )
    development_source_attachments = (
        development_source_attachment_service
        or DevelopmentSourceAttachmentService(
            session_factory,
            development_source_validator,
            mutation_coordinator=ManagedInstanceMutationCoordinator(
                session_factory,
                stale_dispatcher=stale_dispatcher,
                reconciliation=reconciliation,
            ),
        )
    )
    development_source_deployment_operations = (
        DevelopmentSourceDeploymentOperationCoordinator(
            session_factory,
            broker,
            development_source_validator,
            stale_dispatcher=stale_dispatcher,
            reconciliation=reconciliation,
        )
    )
    development_source_deployment_factory = (
        development_source_deployment_service_factory
        or (
            lambda lease: DevelopmentSourceDeploymentService(
                broker, development_source_deployment_operations, lease
            )
        )
    )
    development_source_deployment_executor = DevelopmentSourceDeploymentExecutor(
        development_source_deployment_operations,
        development_source_deployment_factory,
    )
    development_source_enablement = (
        development_source_enablement_service
        or DevelopmentSourceEnablementService(
            session_factory,
            broker,
            development_source_validator,
            coordinator=DevelopmentSourceEnablementOperationCoordinator(
                session_factory,
                stale_dispatcher=stale_dispatcher,
                reconciliation=reconciliation,
            ),
        )
    )
    development_source_smoke_test_operations = (
        DevelopmentSourceSmokeTestOperationCoordinator(
            session_factory,
            stale_dispatcher=stale_dispatcher,
            reconciliation=reconciliation,
        )
    )
    development_source_smoke_test_factory = (
        development_source_smoke_test_service_factory
        or (
            lambda lease: DevelopmentSourceSmokeTestService(
                session_factory,
                broker,
                development_source_validator,
                development_source_smoke_test_operations,
                lease,
            )
        )
    )
    development_source_smoke_test_executor = DevelopmentSourceSmokeTestExecutor(
        development_source_smoke_test_operations,
        development_source_smoke_test_factory,
    )
    addon_zips = AddonZipPresentationService(session_factory)
    addon_zip_imports = AddonZipImportCoordinator(
        session_factory,
        data_root=settings.data_root,
        stale_dispatcher=stale_dispatcher,
    )
    addon_zip_import_executor = AddonZipImportExecutor(addon_zip_imports)
    addon_zip_installations = AddonZipInstallationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
    )
    addon_zip_installation_factory = addon_zip_installation_service_factory or (
        lambda lease: AddonZipInstallationService(
            session_factory,
            broker,
            addon_zip_installations,
            lease,
            inventory_adapter_factory=addon_inventory_adapter_factory,
            readiness_factory=addon_zip_readiness_factory or KodiReadinessCoordinator,
        )
    )
    addon_zip_installation_executor = AddonZipInstallationExecutor(
        addon_zip_installations, addon_zip_installation_factory
    )
    addon_zip_enablement = addon_zip_enablement_service or AddonZipEnablementService(
        session_factory,
        broker,
        coordinator=AddonZipEnablementOperationCoordinator(
            session_factory,
            stale_dispatcher=stale_dispatcher,
            reconciliation=reconciliation,
        ),
        inventory_adapter_factory=addon_inventory_adapter_factory,
    )
    addon_zip_smoke_test_operations = AddonZipSmokeTestOperationCoordinator(
        session_factory,
        stale_dispatcher=stale_dispatcher,
        reconciliation=reconciliation,
    )
    addon_zip_smoke_test_factory = addon_zip_smoke_test_service_factory or (
        lambda lease: AddonZipSmokeTestService(
            session_factory,
            broker,
            addon_zip_smoke_test_operations,
            lease,
            inventory_adapter_factory=addon_inventory_adapter_factory,
        )
    )
    addon_zip_smoke_test_executor = AddonZipSmokeTestExecutor(
        addon_zip_smoke_test_operations, addon_zip_smoke_test_factory
    )
    addon_repositories = (
        addon_repository_administration_service
        or AddonRepositoryAdministrationService(
            session_factory,
            mutation_coordinator=ManagedInstanceMutationCoordinator(
                session_factory,
                stale_dispatcher=stale_dispatcher,
                reconciliation=reconciliation,
            ),
        )
    )
    addon_repository_refreshes = (
        addon_repository_refresh_coordinator
        or AddonRepositoryRefreshCoordinator(session_factory)
    )
    addon_repository_fetches = (
        addon_repository_fetch_coordinator
        or AddonRepositoryFetchCoordinator(
            session_factory, data_root=settings.data_root
        )
    )
    addon_repository_refresh_executor = AddonRepositoryExecutor(
        addon_repository_refreshes, thread_name="repository-refresh"
    )
    addon_repository_fetch_executor = AddonRepositoryExecutor(
        addon_repository_fetches, thread_name="repository-fetch"
    )
    repository_replacements = RepositoryReplacementCoordinator(
        session_factory,
        broker,
        inventory_factory=addon_inventory_adapter_factory,
        reconciliation=reconciliation,
    )
    repository_replacement_factory = repository_replacement_service_factory or (
        lambda lease: RepositoryReplacementService(
            session_factory,
            broker,
            repository_replacements,
            lease,
            inventory_factory=addon_inventory_adapter_factory,
        )
    )
    repository_replacement_executor = RepositoryReplacementExecutor(
        repository_replacements, repository_replacement_factory
    )

    def require_promotion_verification_reconciliation(instance_id: UUID) -> None:
        reconciliation.require_allowed(
            instance_id, ReconciliationAction.PROMOTION_VERIFICATION
        )

    promotion_verifications = (
        promotion_verification_service
        or PromotionVerificationService(
            session_factory,
            broker,
            addon_inventory,
            reconciliation_gate=require_promotion_verification_reconciliation,
        )
    )

    def require_promotion_reconciliation(instance_id: UUID) -> None:
        reconciliation.require_allowed(
            instance_id, ReconciliationAction.AGENT_WORKSPACE_PROMOTION
        )

    def read_promotion_enabled_state(instance_id: UUID, addon_id: str) -> bool:
        inventory = addon_inventory.get(instance_id)
        item = next(
            (
                candidate
                for candidate in inventory.items
                if candidate.addon_id == addon_id
            ),
            None,
        )
        if not inventory.complete or item is None:
            raise ValueError("target enabled state unavailable")
        return item.enabled

    agent_workspace_promotions = (
        agent_workspace_promotion_service
        or AgentWorkspacePromotionService(
            session_factory,
            data_root=settings.data_root,
            development_root=settings.development_root,
            reconciliation_gate=require_promotion_reconciliation,
            verification_publisher=promotion_verifications.publish_durable,
            verification_starter=promotion_verifications.queue_and_run,
            enabled_state_reader=read_promotion_enabled_state,
        )
    )
    mcp_promotion_requests = McpPromotionRequestService(
        session_factory, agent_workspace_promotions
    )
    mcp_transport = build_mcp_http_transport(
        authentication=McpAuthenticationService(session_factory),
        authorization=mcp_authorization,
        registry=mcp_registry,
        tools=McpTargetToolService(
            session_factory,
            mcp_authorization,
            status_service,
            addon_inventory,
            screenshots,
            remote_receipts,
            agent_workspaces,
            mcp_workspace_creation,
            mcp_workspace_retention,
            mcp_workspace_development,
            mcp_promotion_requests,
            target_adapters=mcp_target_adapters,
            production_screenshots=production_screenshots,
            production_transient_approvals=production_transient_approvals,
        ),
    )
    instance_detail = ManagedInstanceDetailService(
        session_factory,
        status_service,
        snapshot_status,
        activity,
        reconciliation,
        development_sources,
        addon_inventory,
    )
    deleted_history = DeletedManagedInstanceHistoryService(
        session_factory, activity, snapshot_status
    )
    context = WebApplicationContext(
        settings=settings,
        auth=auth or WebAuthenticationService(session_factory),
        throttler=throttler or LoginThrottler(),
        status=status_service,
        lifecycle=ManagedRuntimeLifecycleService(
            session_factory,
            broker,
            task_registry,
            status_service,
            coordinator_factory=coordinator_factory or KodiReadinessCoordinator,
            reset_operations=reset_operations,
            lifecycle_operations=lifecycle_operations,
        ),
        matrix=RuntimeMatrixService(
            session_factory,
            trusted_custom_builds_enabled=(settings.trusted_custom_builds_enabled),
        ),
        readiness=task_registry,
        reset_activity=reset_registry,
        reset_status=reset_status_service,
        reset_operations=reset_operations,
        reset_executor=reset_executor,
        snapshot_status=snapshot_status,
        snapshot_operations=snapshot_operations,
        snapshot_executor=snapshot_executor,
        snapshot_restore_operations=snapshot_restore_operations,
        snapshot_restore_executor=snapshot_restore_executor,
        snapshot_delete_operations=snapshot_delete_operations,
        snapshot_delete_executor=snapshot_delete_executor,
        instance_delete_operations=instance_delete_operations,
        instance_delete_executor=instance_delete_executor,
        reconciliation=reconciliation,
        snapshot_reconciliation=snapshot_reconciliation,
        stale_dispatcher=stale_dispatcher,
        lifecycle_operations=lifecycle_operations,
        activity=activity,
        addon_inventory=addon_inventory,
        instance_detail=instance_detail,
        deleted_history=deleted_history,
        development_sources=development_sources,
        development_source_administration=development_source_administration,
        development_source_attachments=development_source_attachments,
        development_source_deployment_operations=(
            development_source_deployment_operations
        ),
        development_source_deployment_executor=(development_source_deployment_executor),
        development_source_enablement=development_source_enablement,
        development_source_smoke_test_operations=(
            development_source_smoke_test_operations
        ),
        development_source_smoke_test_executor=(development_source_smoke_test_executor),
        addon_zips=addon_zips,
        addon_zip_imports=addon_zip_imports,
        addon_zip_import_executor=addon_zip_import_executor,
        addon_zip_installations=addon_zip_installations,
        addon_zip_installation_executor=addon_zip_installation_executor,
        addon_zip_enablement=addon_zip_enablement,
        addon_zip_smoke_test_operations=addon_zip_smoke_test_operations,
        addon_zip_smoke_test_executor=addon_zip_smoke_test_executor,
        addon_repositories=addon_repositories,
        addon_repository_refreshes=addon_repository_refreshes,
        addon_repository_refresh_executor=addon_repository_refresh_executor,
        addon_repository_fetches=addon_repository_fetches,
        addon_repository_fetch_executor=addon_repository_fetch_executor,
        repository_replacements=repository_replacements,
        repository_replacement_executor=repository_replacement_executor,
        agent_workspaces=agent_workspaces,
        agent_workspace_executor=agent_workspace_executor,
        agent_workspace_cleanups=agent_workspace_cleanups,
        agent_workspace_cleanup_executor=agent_workspace_cleanup_executor,
        mcp_workspace_retention=mcp_workspace_retention,
        mcp_workspace_development=mcp_workspace_development,
        mcp_promotion_requests=mcp_promotion_requests,
        agent_workspace_promotions=agent_workspace_promotions,
        promotion_verifications=promotion_verifications,
        screenshots=screenshots,
        browser_remote=browser_remote,
        browser_remote_rate_limiter=remote_rate_limiter,
        live_display=live_display,
        external_targets=external_targets,
        production_targets=production_targets,
        external_bridges=external_bridges,
        production_native_control_acknowledgements=(
            production_native_control_acknowledgements
        ),
        production_transient_approvals=production_transient_approvals,
        mcp_administration=mcp_administration,
        mcp_transport=mcp_transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        context.agent_workspace_promotions.sweep_startup()
        context.promotion_verifications.sweep_startup()
        context.mcp_workspace_development.sweep_startup()
        context.stale_dispatcher.sweep_startup()
        context.reset_executor.start()
        context.snapshot_executor.start()
        context.snapshot_restore_executor.start()
        context.snapshot_delete_executor.start()
        context.instance_delete_executor.start()
        context.development_source_deployment_executor.start()
        context.development_source_smoke_test_executor.start()
        context.addon_zip_import_executor.start()
        context.addon_zip_installation_executor.start()
        context.addon_zip_smoke_test_executor.start()
        context.addon_repository_refresh_executor.start()
        context.addon_repository_fetch_executor.start()
        context.repository_replacement_executor.start()
        context.agent_workspace_executor.start()
        context.agent_workspace_cleanup_executor.start()
        context.mcp_workspace_retention.sweep(force=True)
        async with context.mcp_transport.server.session_manager.run():
            try:
                yield
            finally:
                context.reset_executor.close()
                context.snapshot_executor.close()
                context.snapshot_restore_executor.close()
                context.snapshot_delete_executor.close()
                context.instance_delete_executor.close()
                context.development_source_deployment_executor.close()
                context.development_source_smoke_test_executor.close()
                context.addon_zip_import_executor.close()
                context.addon_zip_installation_executor.close()
                context.addon_zip_smoke_test_executor.close()
                context.addon_repository_refresh_executor.close()
                context.addon_repository_fetch_executor.close()
                context.repository_replacement_executor.close()
                context.agent_workspace_executor.close()
                context.agent_workspace_cleanup_executor.close()
                context.readiness.close()

    app = FastAPI(
        title="Kodi Lab",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.kodi_lab = context
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[settings.web_host, "localhost"],
    )
    app.add_middleware(_WorkspaceMutationBodyLimitMiddleware)
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    app.router.routes.append(
        Route(
            "/mcp",
            context.mcp_transport.app,
            methods=["POST"],
            name="mcp",
            include_in_schema=False,
        )
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        context.mcp_workspace_retention.sweep()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def current_session(request: Request) -> AuthenticatedWebSession:
        token = request.cookies.get(settings.web_session_cookie_name)
        authenticated = context.auth.resolve(token)
        if authenticated is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return authenticated

    def mutation_session(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AuthenticatedWebSession:
        _require_same_origin(request)
        supplied = request.headers.get("X-Kodi-Lab-CSRF")
        if not context.auth.csrf_matches(authenticated, supplied):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return authenticated

    @app.exception_handler(ManagedInstanceNotFoundError)
    async def instance_not_found(
        _request: Request, _error: ManagedInstanceNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404, "managed_instance_not_found", "Managed instance not found."
        )

    @app.exception_handler(ScreenshotServiceError)
    async def screenshot_error(
        _request: Request, error: ScreenshotServiceError
    ) -> JSONResponse:
        status_code = {
            "screenshot_capture_in_progress": 409,
            "visual_rate_limited": 429,
            "visual_instance_not_running": 409,
            "visual_kodi_not_ready": 409,
            "screenshot_unsupported": 409,
            "screenshot_invalid_result": 502,
            "screenshot_capture_unavailable": 503,
        }.get(error.code, 503)
        return _json_error(status_code, error.code, error.safe_message)

    @app.exception_handler(BrowserRemoteError)
    async def browser_remote_error(
        _request: Request, error: BrowserRemoteError
    ) -> JSONResponse:
        status_code = {
            "visual_rate_limited": 429,
            "visual_instance_not_running": 409,
            "visual_kodi_not_ready": 409,
            "remote_action_unsupported": 409,
            "remote_action_conflict": 409,
            "remote_no_active_player": 409,
            "remote_kodi_rejected": 502,
            "remote_action_ambiguous": 503,
        }.get(error.code, 503)
        return _json_error(status_code, error.code, error.safe_message)

    @app.exception_handler(TargetNotFoundError)
    async def target_not_found(
        _request: Request, _error: TargetNotFoundError
    ) -> JSONResponse:
        return _json_error(404, "target_not_found", "Target not found.")

    @app.exception_handler(DevelopmentSourceNotFoundError)
    async def development_source_not_found(
        _request: Request, _error: DevelopmentSourceNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404, "development_source_not_found", "Development source not found."
        )

    @app.exception_handler(AgentWorkspaceNotFoundError)
    async def agent_workspace_not_found(
        _request: Request, error: AgentWorkspaceNotFoundError
    ) -> JSONResponse:
        return _json_error(404, error.code, error.safe_message)

    @app.exception_handler(AgentWorkspaceStorageError)
    async def agent_workspace_storage_failed(
        _request: Request, error: AgentWorkspaceStorageError
    ) -> JSONResponse:
        http_status = 404 if error.code == "agent_workspace_path_unavailable" else 422
        return _json_error(http_status, error.code, error.safe_message)

    @app.exception_handler(AgentWorkspaceError)
    async def agent_workspace_failed(
        _request: Request, error: AgentWorkspaceError
    ) -> JSONResponse:
        http_status = 409 if isinstance(error, AgentWorkspaceConflictError) else 422
        return _json_error(http_status, error.code, error.safe_message)

    @app.exception_handler(AgentWorkspacePromotionError)
    async def agent_workspace_promotion_failed(
        _request: Request, error: AgentWorkspacePromotionError
    ) -> JSONResponse:
        http_status = (
            409
            if isinstance(error, AgentWorkspacePromotionConflictError)
            else 404
            if error.code.endswith("not_found")
            else 422
        )
        return _json_error(http_status, error.code, error.safe_message)

    @app.exception_handler(AgentWorkspaceCleanupError)
    async def agent_workspace_cleanup_failed(
        _request: Request, error: AgentWorkspaceCleanupError
    ) -> JSONResponse:
        return _json_error(
            404 if error.code.endswith("not_found") else 409,
            error.code,
            error.safe_message,
        )

    @app.exception_handler(PromotionVerificationError)
    async def promotion_verification_failed(
        _request: Request, error: PromotionVerificationError
    ) -> JSONResponse:
        return _json_error(
            404 if error.code.endswith("not_found") else 409,
            error.code,
            error.safe_message,
        )

    @app.exception_handler(AddonRepositoryNotFoundError)
    async def addon_repository_not_found(
        _request: Request, error: AddonRepositoryNotFoundError
    ) -> JSONResponse:
        return _json_error(404, error.code, "Repository resource not found.")

    @app.exception_handler(AddonRepositoryConflictError)
    async def addon_repository_conflict(
        _request: Request, error: AddonRepositoryConflictError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(AddonRepositoryError)
    async def addon_repository_policy_failed(
        _request: Request, error: AddonRepositoryError
    ) -> JSONResponse:
        return _json_error(422, error.code, error.safe_message)

    @app.exception_handler(AddonRepositoryServiceError)
    async def addon_repository_failed(
        _request: Request, error: AddonRepositoryServiceError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(RepositoryReplacementError)
    async def repository_replacement_failed(
        _request: Request, error: RepositoryReplacementError
    ) -> JSONResponse:
        http_status = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if isinstance(error, RepositoryReplacementUnsupportedError)
            else status.HTTP_409_CONFLICT
        )
        return _json_error(http_status, error.code, str(error)[:256])

    @app.exception_handler(RequestValidationError)
    async def bounded_request_validation(
        request: Request, error: RequestValidationError
    ) -> Response:
        path = request.url.path
        closed_schema_mutation = request.method == "POST" and (
            path in {"/api/development-sources", "/api/agent-workspaces"}
            or path == "/api/addon-repositories"
            or "/addon-repositories/" in path
            or "/repository-artifacts/" in path
            or "/replacement-plans/" in path
            or "/promotion-plans" in path
            or (
                "/development-sources/" in path
                and path.endswith(("/attach", "/detach"))
            )
        )
        if closed_schema_mutation:
            return _json_error(
                422, "request_validation_failed", "Request body is invalid."
            )
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(DevelopmentSourceValidationError)
    async def development_source_registration_validation_failed(
        _request: Request, error: DevelopmentSourceValidationError
    ) -> JSONResponse:
        messages = {
            "root_not_configured": "Development-source registration is not configured.",
            "root_invalid": "Development-source registration is unavailable.",
            "root_unavailable": (
                "The configured development-source root is unavailable."
            ),
            "locator_invalid": "The development-source locator is unsafe or invalid.",
            "source_unavailable": (
                "The development source is missing, unreadable, or link-unsafe."
            ),
            "addon_xml_missing": "The development source has no root-level addon.xml.",
            "addon_xml_unsafe": (
                "The development source addon.xml is unreadable or unsafe."
            ),
            "addon_xml_too_large": (
                "The development source addon.xml exceeds the limit."
            ),
            "addon_xml_malformed": "The development source addon.xml is malformed.",
            "addon_metadata_invalid": (
                "The development source addon.xml metadata is invalid."
            ),
        }
        http_status = (
            status.HTTP_409_CONFLICT
            if error.code.value.startswith("root_")
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        return _json_error(http_status, error.code.value, messages[error.code.value])

    @app.exception_handler(DevelopmentSourceDataValidationError)
    async def development_source_registration_data_invalid(
        _request: Request, _error: DevelopmentSourceDataValidationError
    ) -> JSONResponse:
        return _json_error(
            422,
            "development_source_registration_invalid",
            "Development-source registration fields are invalid.",
        )

    @app.exception_handler(DevelopmentSourceConflictError)
    async def development_source_registration_conflict(
        _request: Request, error: DevelopmentSourceConflictError
    ) -> JSONResponse:
        codes = {
            "logical name": (
                "development_source_logical_name_exists",
                "A development source with that logical name already exists.",
            ),
            "relative locator": (
                "development_source_already_registered",
                "That development source is already registered.",
            ),
            "add-on ID": (
                "development_source_addon_id_exists",
                "A development source for that add-on ID already exists.",
            ),
            "catalog identity": (
                "development_source_registration_conflict",
                "Development-source registration conflicted with existing data.",
            ),
        }
        code, message = codes.get(
            error.field,
            (
                "development_source_registration_conflict",
                "Development-source registration conflicted with existing data.",
            ),
        )
        return _json_error(409, code, message)

    @app.exception_handler(DevelopmentSourceAttachmentError)
    async def development_source_attachment_failed(
        _request: Request, error: DevelopmentSourceAttachmentError
    ) -> JSONResponse:
        if isinstance(error, DevelopmentSourceAlreadyAttachedError):
            code = "development_source_already_attached"
            message = "Development source is already attached to this instance."
        elif isinstance(error, DevelopmentSourceNotAttachedError):
            code = "development_source_not_attached"
            message = "Development source is not attached to this instance."
        elif isinstance(error, DevelopmentSourceAddonConflictError):
            code = "development_source_addon_conflict"
            message = "The instance already has a source for this add-on ID."
        elif isinstance(error, DevelopmentSourceIdentityChangedError):
            code = error.code
            message = "Development source add-on identity changed after registration."
        elif isinstance(error, DevelopmentSourceAttachmentValidationError):
            code = "development_source_attachment_validation_failed"
            message = "Development source validation failed before attachment."
        elif isinstance(error, PersistentDevelopmentSourceAttachmentUnsupportedError):
            code = "development_source_attachment_unsupported"
            message = (
                "Development-source attachment is supported only for disposable "
                "instances."
            )
        else:
            code = "development_source_attachment_conflict"
            message = "Development-source attachment could not be changed."
        return _json_error(409, code, message)

    @app.exception_handler(DevelopmentSourceDeploymentConflictError)
    async def development_source_deployment_conflict(
        _request: Request, error: DevelopmentSourceDeploymentConflictError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceDeploymentUnsupportedError)
    async def development_source_deployment_unsupported(
        _request: Request, error: DevelopmentSourceDeploymentUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceDeploymentError)
    async def development_source_deployment_failed(
        _request: Request, error: DevelopmentSourceDeploymentError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceEnablementError)
    async def development_source_enablement_failed(
        _request: Request, error: DevelopmentSourceEnablementError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceSmokeTestNotFoundError)
    async def development_source_smoke_test_not_found(
        _request: Request, error: DevelopmentSourceSmokeTestNotFoundError
    ) -> JSONResponse:
        return _json_error(404, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceSmokeTestConflictError)
    async def development_source_smoke_test_conflict(
        _request: Request, error: DevelopmentSourceSmokeTestConflictError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(DevelopmentSourceSmokeTestError)
    async def development_source_smoke_test_failed(
        _request: Request, error: DevelopmentSourceSmokeTestError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(AddonZipImportConflictError)
    async def addon_zip_import_conflict(
        _request: Request, error: AddonZipImportConflictError
    ) -> JSONResponse:
        return _json_error(409, "addon_zip_import_conflict", str(error)[:256])

    @app.exception_handler(AddonZipValidationError)
    async def addon_zip_upload_invalid(
        _request: Request, error: AddonZipValidationError
    ) -> JSONResponse:
        return _json_error(422, error.code, error.safe_message)

    @app.exception_handler(AddonZipInstallationError)
    async def addon_zip_installation_failed(
        _request: Request, error: AddonZipInstallationError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(AddonZipEnablementError)
    async def addon_zip_enablement_failed(
        _request: Request, error: AddonZipEnablementError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(AddonZipSmokeTestNotFoundError)
    async def addon_zip_smoke_test_not_found(
        _request: Request, error: AddonZipSmokeTestNotFoundError
    ) -> JSONResponse:
        return _json_error(404, error.code, str(error)[:256])

    @app.exception_handler(AddonZipSmokeTestConflictError)
    async def addon_zip_smoke_test_conflict(
        _request: Request, error: AddonZipSmokeTestConflictError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(AddonZipSmokeTestError)
    async def addon_zip_smoke_test_failed(
        _request: Request, error: AddonZipSmokeTestError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(LifecycleConflictError)
    async def lifecycle_conflict(
        _request: Request, error: LifecycleConflictError
    ) -> JSONResponse:
        return _json_error(409, "lifecycle_conflict", str(error)[:256])

    @app.exception_handler(ResetOperationConflictError)
    async def reset_operation_conflict(
        _request: Request, error: ResetOperationConflictError
    ) -> JSONResponse:
        return _json_error(409, "reset_operation_conflict", str(error)[:256])

    @app.exception_handler(ManagedInstanceMutationConflictError)
    async def mutation_conflict(
        _request: Request, error: ManagedInstanceMutationConflictError
    ) -> JSONResponse:
        return _json_error(409, "managed_instance_mutation_conflict", str(error)[:256])

    @app.exception_handler(LifecycleOperationConflictError)
    async def lifecycle_operation_conflict(
        _request: Request, error: LifecycleOperationConflictError
    ) -> JSONResponse:
        return _json_error(409, "lifecycle_operation_conflict", str(error)[:256])

    @app.exception_handler(DeletedManagedInstanceHistoryNotFoundError)
    async def deleted_history_not_found(
        _request: Request, _error: DeletedManagedInstanceHistoryNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404,
            "deleted_instance_history_not_found",
            "Deleted instance history was not found.",
        )

    @app.exception_handler(ManagedInstanceReconciliationRequiredError)
    async def reconciliation_required(
        _request: Request, error: ManagedInstanceReconciliationRequiredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": error.code,
                    "message": error.status.safe_reason,
                    "reconciliation": error.status.model_dump(mode="json"),
                }
            },
        )

    @app.exception_handler(CoordinationCorruptionError)
    async def coordination_corruption(
        _request: Request, error: CoordinationCorruptionError
    ) -> JSONResponse:
        return _json_error(409, error.code, str(error)[:256])

    @app.exception_handler(SnapshotCreateConflictError)
    async def snapshot_create_conflict(
        _request: Request, error: SnapshotCreateConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_create_conflict", str(error)[:256])

    @app.exception_handler(SnapshotCreateUnsupportedError)
    async def snapshot_create_unsupported(
        _request: Request, error: SnapshotCreateUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_create_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotRestoreConflictError)
    async def snapshot_restore_conflict(
        _request: Request, error: SnapshotRestoreConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_restore_conflict", str(error)[:256])

    @app.exception_handler(SnapshotRestoreUnsupportedError)
    async def snapshot_restore_unsupported(
        _request: Request, error: SnapshotRestoreUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_restore_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotSourceInstanceDeletedError)
    async def snapshot_source_deleted(
        _request: Request, _error: SnapshotSourceInstanceDeletedError
    ) -> JSONResponse:
        return _json_error(
            409,
            "source_instance_deleted",
            "The preserved Snapshot source ManagedInstance has been deleted.",
        )

    @app.exception_handler(SnapshotDeleteConflictError)
    async def snapshot_delete_conflict(
        _request: Request, error: SnapshotDeleteConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_delete_conflict", str(error)[:256])

    @app.exception_handler(SnapshotDeleteUnsupportedError)
    async def snapshot_delete_unsupported(
        _request: Request, error: SnapshotDeleteUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_delete_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotDeleteNotFoundError)
    async def snapshot_delete_not_found(
        _request: Request, _error: SnapshotDeleteNotFoundError
    ) -> JSONResponse:
        return _json_error(404, "snapshot_not_found", "Snapshot not found.")

    @app.exception_handler(ManagedInstanceDeleteConflictError)
    async def instance_delete_conflict(
        _request: Request, error: ManagedInstanceDeleteConflictError
    ) -> JSONResponse:
        return _json_error(409, "instance_delete_conflict", str(error)[:256])

    @app.exception_handler(ManagedInstanceDeleteUnsupportedError)
    async def instance_delete_unsupported(
        _request: Request, error: ManagedInstanceDeleteUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "instance_delete_unsupported", str(error)[:256])

    @app.exception_handler(ManagedInstanceDeleteNotFoundError)
    async def instance_delete_not_found(
        _request: Request, _error: ManagedInstanceDeleteNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404, "managed_instance_not_found", "Managed instance not found."
        )

    @app.exception_handler(ClientError)
    async def broker_failure(_request: Request, error: ClientError) -> JSONResponse:
        if isinstance(error, (BrokerUnavailableError, ClientTransportError)):
            return _json_error(
                503, "runtime_service_unavailable", "Runtime service unavailable."
            )
        if isinstance(error, RemoteBrokerError):
            conflict_codes = {
                "managed_container_not_found",
                "ambiguous_managed_container",
                "container_identity_precondition_failed",
                "runtime_state_precondition_failed",
                "container_state_not_reconcilable",
                "container_state_not_stoppable",
                "runtime_revision_precondition_failed",
                "container_must_be_stopped",
                "provisioning_conflict",
            }
            code = 409 if error.code in conflict_codes else 503
            return _json_error(code, error.code, error.safe_message[:256])
        return _json_error(
            503, "runtime_service_unavailable", "Runtime service unavailable."
        )

    @app.exception_handler(McpAdministrationError)
    async def mcp_administration_failure(
        _request: Request, error: McpAdministrationError
    ) -> JSONResponse:
        status_code = 404 if error.code.endswith("_not_found") else 409
        return _json_error(status_code, error.code, "MCP administration rejected.")

    @app.exception_handler(ExternalTargetError)
    async def external_target_failure(
        _request: Request, error: ExternalTargetError
    ) -> JSONResponse:
        status_code = {
            "target_not_found": 404,
            "endpoint_not_allowed": 422,
            "target_unreachable": 503,
            "authentication_failed": 502,
            "invalid_kodi_response": 502,
            "unsupported_kodi_version": 409,
            "identity_mismatch": 409,
            "needs_reverification": 409,
            "secret_unavailable": 503,
            "verification_required": 409,
        }.get(error.code, 409)
        return _json_error(status_code, error.code, "External target request rejected.")

    @app.exception_handler(ExternalBridgeServiceError)
    async def external_bridge_failure(
        _request: Request, error: ExternalBridgeServiceError
    ) -> JSONResponse:
        status_code = {
            "target_not_found": 404,
            "bridge_not_paired": 409,
            "bridge_unreachable": 503,
            "bridge_authentication_failed": 502,
            "bridge_identity_mismatch": 409,
            "bridge_protocol_unsupported": 409,
            "bridge_capability_unavailable": 409,
            "bridge_replay_rejected": 409,
            "screenshot_invalid": 502,
            "screenshot_too_large": 502,
            "needs_reverification": 409,
        }.get(error.code, 409)
        return _json_error(status_code, error.code, "External bridge request rejected.")

    @app.exception_handler(ProductionInspectionError)
    async def production_control_acknowledgement_failure(
        _request: Request, error: ProductionInspectionError
    ) -> JSONResponse:
        status_code = (
            422 if error.code == "transport_acknowledgement_not_required" else 409
        )
        return _json_error(
            status_code, error.code, "Production control acknowledgement rejected."
        )

    @app.exception_handler(ProductionApprovalError)
    async def production_approval_failure(
        _request: Request, error: ProductionApprovalError
    ) -> JSONResponse:
        status_code = 404 if error.code == "approval_not_found" else 409
        if error.code in {"bridge_unreachable", "target_unreachable"}:
            status_code = 503
        return _json_error(status_code, error.code, "Production approval rejected.")

    @app.exception_handler(McpWorkspaceDevelopmentError)
    async def external_development_failure(
        _request: Request, error: McpWorkspaceDevelopmentError
    ) -> JSONResponse:
        status_code = 503 if error.code == "deployment_ambiguous" else 409
        return _json_error(
            status_code, error.code, "External development request rejected."
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        token = request.cookies.get(settings.web_session_cookie_name)
        if context.auth.resolve(token) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": None,
                "credentials_configured": context.auth.credentials_configured(),
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        _require_same_origin(request)
        form = await request.form()
        password_value = form.get("password")
        password = password_value if isinstance(password_value, str) else ""
        source = "unknown" if request.client is None else request.client.host
        if not context.throttler.allowed(source):
            return _login_failure(templates, request, context, status_code=401)
        created = context.auth.login(password)
        if created is None:
            context.throttler.record_failure(source)
            return _login_failure(templates, request, context, status_code=401)
        context.throttler.record_success(source)
        context.auth.logout(request.cookies.get(settings.web_session_cookie_name))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            settings.web_session_cookie_name,
            created.token,
            max_age=settings.web_session_absolute_seconds,
            httponly=True,
            secure=settings.web_cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        _require_same_origin(request)
        authenticated = current_session(request)
        form = await request.form()
        csrf_value = form.get("csrf_token")
        csrf_token = csrf_value if isinstance(csrf_value, str) else None
        if not context.auth.csrf_matches(authenticated, csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        context.browser_remote_rate_limiter.retire_session(authenticated.csrf_token)
        context.auth.logout(request.cookies.get(settings.web_session_cookie_name))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            settings.web_session_cookie_name,
            path="/",
            secure=settings.web_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def main_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        reset_statuses = {
            item.managed_instance_id: item
            for item in context.reset_status.list_statuses()
        }
        instances = context.status.list_statuses()
        reconciliation_statuses = {
            item.managed_instance_id: context.reconciliation.status(
                item.managed_instance_id
            )
            for item in instances
        }
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "instances": instances,
                "reset_statuses": reset_statuses,
                "reconciliation_statuses": reconciliation_statuses,
                "csrf_token": authenticated.csrf_token,
                "poll_interval_ms": 2000,
            },
        )

    @app.get("/mcp-clients", response_class=HTMLResponse)
    async def mcp_clients_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        production_bridge_statuses: dict[UUID, ExternalBridgeStatus] = {}
        for target in context.mcp_administration.list_target_policies():
            if target.target_class == "external_production":
                with suppress(ExternalBridgeServiceError):
                    production_bridge_statuses[target.target_id] = (
                        context.external_bridges.status(target.target_id)
                    )
        return templates.TemplateResponse(
            request,
            "mcp-clients.html",
            {
                "csrf_token": authenticated.csrf_token,
                "clients": context.mcp_administration.list_clients(),
                "presets": context.mcp_administration.list_presets(),
                "targets": context.mcp_administration.list_target_policies(),
                "production_scopes": (
                    context.mcp_administration.list_production_inspection_scopes()
                ),
                "production_bridge_statuses": production_bridge_statuses,
                "development_sources": (
                    context.development_source_administration.list_sources()
                ),
                "eligible_workspaces": {
                    client.id: (
                        context.mcp_administration.list_eligible_unbound_workspaces(
                            client.id
                        )
                    )
                    for client in context.mcp_administration.list_clients()
                },
            },
        )

    @app.get("/external-targets", response_class=HTMLResponse)
    async def external_targets_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        targets = context.external_targets.list_targets()
        return templates.TemplateResponse(
            request,
            "external-targets.html",
            {
                "csrf_token": authenticated.csrf_token,
                "targets": targets,
                "bridge_statuses": {
                    target.target_id: context.external_bridges.status(target.target_id)
                    for target in targets
                },
                "allowed_ports": settings.external_target_allowed_ports,
                "bridge_allowed_ports": settings.external_bridge_allowed_ports,
                "test_loopback_enabled": (
                    settings.external_target_test_loopback_enabled
                ),
            },
        )

    @app.get("/production-targets", response_class=HTMLResponse)
    async def production_targets_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        targets = context.production_targets.list_targets()
        bridge_statuses: dict[UUID, ExternalBridgeStatus] = {}
        native_control_acknowledgements: dict[
            UUID, ProductionNativeControlAcknowledgementRead
        ] = {}
        for target in targets:
            with suppress(ExternalBridgeServiceError):
                bridge_statuses[target.target_id] = context.external_bridges.status(
                    target.target_id
                )
            with suppress(ProductionInspectionError):
                acknowledgement = (
                    context.production_native_control_acknowledgements.current(
                        target.target_id
                    )
                )
                if acknowledgement is not None:
                    native_control_acknowledgements[target.target_id] = acknowledgement
        return templates.TemplateResponse(
            request,
            "production-targets.html",
            {
                "csrf_token": authenticated.csrf_token,
                "targets": targets,
                "bridge_statuses": bridge_statuses,
                "native_control_acknowledgements": (native_control_acknowledgements),
                "production_scopes": (
                    context.mcp_administration.list_production_inspection_scopes()
                ),
                "approval_queue": context.production_transient_approvals.list_queue(),
                "reviewed_builds": context.external_bridges.list_reviewed_builds(),
                "mutation_control": context.production_targets.mutation_control(),
                "allowed_ports": settings.external_target_allowed_ports,
                "test_loopback_enabled": (
                    settings.external_target_test_loopback_enabled
                ),
                "bridge_allowed_ports": settings.external_bridge_allowed_ports,
            },
        )

    @app.get("/runtimes", response_class=HTMLResponse)
    async def runtime_matrix_page(
        request: Request,
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
    ) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        matrix = context.matrix.list_matrix(
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )
        return templates.TemplateResponse(
            request,
            "runtimes.html",
            {"matrix": matrix},
        )

    @app.get("/development-sources", response_class=HTMLResponse)
    async def development_source_list_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "development-sources.html",
            {
                "sources": context.development_sources.list_sources(),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/development-sources/{source_id}", response_class=HTMLResponse)
    async def development_source_detail_page(
        request: Request, source_id: UUID
    ) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "development-source-detail.html",
            {"detail": context.development_sources.get_source(source_id)},
        )

    @app.get("/agent-workspaces", response_class=HTMLResponse)
    async def agent_workspace_list_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "agent-workspaces.html",
            {
                "workspaces": context.agent_workspaces.list_workspaces(),
                "sources": context.development_sources.list_sources(),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/agent-workspaces/{workspace_id}", response_class=HTMLResponse)
    async def agent_workspace_detail_page(
        request: Request, workspace_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        workspace = context.agent_workspaces.get(workspace_id)
        entries = (
            context.agent_workspaces.inspect_directory(workspace_id, "")
            if workspace.state.value == "ready"
            else []
        )
        changes = (
            context.agent_workspaces.list_changes(workspace_id)
            if workspace.state.value == "ready"
            else None
        )
        activity = context.agent_workspaces.list_activity(workspace_id)
        return templates.TemplateResponse(
            request,
            "agent-workspace-detail.html",
            {
                "workspace": workspace,
                "entries": entries,
                "changes": changes,
                "activity": activity,
                "lifecycle_activity": context.agent_workspaces.list_lifecycle_activity(
                    workspace_id
                ),
                "promotion_targets": (
                    context.agent_workspace_promotions.eligible_targets(workspace_id)
                    if workspace.state.value == "ready"
                    else []
                ),
                "promotion_plan": context.agent_workspace_promotions.active_plan(
                    workspace_id
                ),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get(
        "/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-plans/{plan_id}",
        response_class=HTMLResponse,
    )
    async def agent_workspace_promotion_review_page(
        request: Request, instance_id: UUID, workspace_id: UUID, plan_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        plan = context.agent_workspace_promotions.get_plan(
            instance_id, workspace_id, plan_id
        )
        return templates.TemplateResponse(
            request,
            "agent-workspace-promotion.html",
            {"plan": plan, "csrf_token": authenticated.csrf_token},
        )

    @app.get("/addon-zips", response_class=HTMLResponse)
    async def addon_zip_list_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "addon-zips.html",
            {
                "artifacts": context.addon_zips.list_artifacts(),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/addon-repositories", response_class=HTMLResponse)
    async def addon_repository_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        repositories = context.addon_repositories.list()
        return templates.TemplateResponse(
            request,
            "addon-repositories.html",
            {
                "repository_cards": [
                    (
                        item,
                        context.addon_repository_refreshes.candidates(
                            item.repository_id
                        ),
                    )
                    for item in repositories
                ],
                "instances": context.status.list_statuses(),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}", response_class=HTMLResponse)
    async def instance_detail_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "instance-detail.html",
            {
                "detail": context.instance_detail.get(instance_id),
                "visual": context.screenshots.get_status(instance_id),
                "remote_available": context.browser_remote.available(instance_id),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}/history", response_class=HTMLResponse)
    async def instance_history_page(request: Request, instance_id: UUID) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        return templates.TemplateResponse(
            request,
            "instance-history.html",
            {
                "instance": instance,
                "activity": context.activity.list_for_live_instance(instance_id),
            },
        )

    @app.get("/history/instances", response_class=HTMLResponse)
    async def deleted_instance_list_page(request: Request) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "deleted-instances.html",
            {"instances": context.deleted_history.list()},
        )

    @app.get("/history/instances/{original_instance_id}", response_class=HTMLResponse)
    async def deleted_instance_history_page(
        request: Request, original_instance_id: UUID
    ) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "deleted-instance-history.html",
            {"history": context.deleted_history.get(original_instance_id)},
        )

    @app.get("/instances/{instance_id}/reset", response_class=HTMLResponse)
    async def reset_confirmation_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        reset_status = context.reset_status.get(instance_id)
        instance = context.status.get(instance_id)
        return templates.TemplateResponse(
            request,
            "reset-confirmation.html",
            {
                "instance": instance,
                "reset": reset_status,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}/delete", response_class=HTMLResponse)
    async def instance_delete_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshots = context.snapshot_status.list_for_instance(instance_id)
        with session_factory() as session:
            persisted = ManagedInstanceRepository(session).get(instance_id)
        if persisted is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        counts = {
            state_value: sum(item.state.value == state_value for item in snapshots)
            for state_value in ("available", "failed", "quarantined")
        }
        return templates.TemplateResponse(
            request,
            "instance-delete.html",
            {
                "instance": instance,
                "snapshot_count": len(snapshots),
                "aggregate_snapshot_bytes": sum(
                    item.archive_size or 0 for item in snapshots
                ),
                "snapshot_counts": counts,
                "notes_present": persisted.notes is not None,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/snapshots/preserved", response_class=HTMLResponse)
    async def preserved_snapshot_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "preserved-snapshots.html",
            {
                "snapshots": context.snapshot_status.list_preserved(),
                "reconciliation_statuses": {
                    item.snapshot_id: context.snapshot_reconciliation.status(
                        item.snapshot_id
                    )
                    for item in context.snapshot_status.list_preserved()
                },
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/snapshots/preserved/{snapshot_id}/delete", response_class=HTMLResponse)
    async def preserved_snapshot_delete_page(
        request: Request, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_preserved()
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if snapshot.state not in {
            SnapshotState.AVAILABLE,
            SnapshotState.FAILED,
            SnapshotState.QUARANTINED,
        }:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return templates.TemplateResponse(
            request,
            "preserved-snapshot-delete.html",
            {
                "snapshot": snapshot,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}/snapshots", response_class=HTMLResponse)
    async def snapshot_history_page(request: Request, instance_id: UUID) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        return templates.TemplateResponse(
            request,
            "snapshots.html",
            {
                "instance": instance,
                "snapshots": context.snapshot_status.list_for_instance(instance_id),
                "reconciliation_statuses": {
                    item.snapshot_id: context.snapshot_reconciliation.status(
                        item.snapshot_id
                    )
                    for item in context.snapshot_status.list_for_instance(instance_id)
                },
            },
        )

    @app.get("/instances/{instance_id}/snapshots/new", response_class=HTMLResponse)
    async def snapshot_create_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "snapshot-create.html",
            {
                "instance": context.status.get(instance_id),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get(
        "/instances/{instance_id}/snapshots/{snapshot_id}/restore",
        response_class=HTMLResponse,
    )
    async def snapshot_restore_page(
        request: Request, instance_id: UUID, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_for_instance(instance_id)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )

        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        with session_factory() as session:
            persisted_instance = ManagedInstanceRepository(session).get(instance_id)
        fixture_id = (
            None
            if persisted_instance is None
            else persisted_instance.fixture_definition_id
        )
        return templates.TemplateResponse(
            request,
            "snapshot-restore.html",
            {
                "instance": instance,
                "snapshot": snapshot,
                "runtime_match": (
                    instance.runtime_revision_id == snapshot.runtime_revision_id
                ),
                "fixture_match": fixture_id == snapshot.fixture_definition_id,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get(
        "/instances/{instance_id}/snapshots/{snapshot_id}/delete",
        response_class=HTMLResponse,
    )
    async def snapshot_delete_page(
        request: Request, instance_id: UUID, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_for_instance(instance_id)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if snapshot.state not in {
            SnapshotState.AVAILABLE,
            SnapshotState.FAILED,
            SnapshotState.QUARANTINED,
        }:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return templates.TemplateResponse(
            request,
            "snapshot-delete.html",
            {
                "instance": instance,
                "snapshot": snapshot,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/api/mcp/presets", response_model=list[McpPresetRead])
    def list_mcp_presets(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpPresetRead]:
        return context.mcp_administration.list_presets()

    @app.post(
        "/api/mcp/presets",
        response_model=McpPresetRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_mcp_preset(
        payload: McpPresetCreateRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpPresetRead:
        return context.mcp_administration.create_custom_preset(
            display_name=payload.display_name,
            template_preset_id=payload.template_preset_id,
        )

    @app.get("/api/mcp/clients", response_model=list[McpClientRead])
    def list_mcp_clients(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpClientRead]:
        return context.mcp_administration.list_clients()

    @app.post(
        "/api/mcp/clients",
        response_model=McpClientRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_mcp_client(
        payload: McpClientCreateRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.create_client(
            display_name=payload.display_name, preset_id=payload.preset_id
        )

    @app.get("/api/mcp/clients/{client_id}", response_model=McpClientRead)
    def get_mcp_client(
        client_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> McpClientRead:
        return context.mcp_administration.get_client(client_id)

    @app.post("/api/mcp/clients/{client_id}/rename", response_model=McpClientRead)
    def rename_mcp_client(
        client_id: UUID,
        payload: McpClientRenameRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.rename_client(client_id, payload.display_name)

    @app.post("/api/mcp/clients/{client_id}/enabled", response_model=McpClientRead)
    def set_mcp_client_enabled(
        client_id: UUID,
        payload: McpClientEnabledRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.set_client_enabled(client_id, payload.enabled)

    @app.post("/api/mcp/clients/{client_id}/preset", response_model=McpClientRead)
    def assign_mcp_client_preset(
        client_id: UUID,
        payload: McpPresetAssignmentRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.assign_client_preset(
            client_id, payload.preset_id
        )

    @app.post(
        "/api/mcp/clients/{client_id}/credentials",
        response_model=McpIssuedCredential,
        status_code=status.HTTP_201_CREATED,
    )
    def issue_mcp_credential(
        client_id: UUID,
        payload: McpCredentialIssueRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpIssuedCredential:
        lifetime = (
            None
            if payload.expiration_seconds is None
            else timedelta(seconds=payload.expiration_seconds)
        )
        return context.mcp_administration.issue_credential(client_id, lifetime=lifetime)

    @app.post(
        "/api/mcp/clients/{client_id}/credentials/{credential_id}/revoke",
        response_model=McpCredentialRead,
    )
    def revoke_mcp_credential(
        client_id: UUID,
        credential_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpCredentialRead:
        return context.mcp_administration.revoke_credential(client_id, credential_id)

    @app.post(
        "/api/mcp/clients/{client_id}/credentials/{credential_id}/rotate",
        response_model=McpIssuedCredential,
    )
    def rotate_mcp_credential(
        client_id: UUID,
        credential_id: UUID,
        payload: McpCredentialIssueRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpIssuedCredential:
        lifetime = (
            None
            if payload.expiration_seconds is None
            else timedelta(seconds=payload.expiration_seconds)
        )
        return context.mcp_administration.rotate_credential(
            client_id, credential_id, lifetime=lifetime
        )

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}",
        response_model=McpClientRead,
    )
    def grant_mcp_target(
        client_id: UUID,
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.grant_target(client_id, target_id)

    @app.delete(
        "/api/mcp/clients/{client_id}/targets/{target_id}",
        response_model=McpClientRead,
    )
    def remove_mcp_target_grant(
        client_id: UUID,
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.remove_target_grant(client_id, target_id)

    @app.get(
        "/api/mcp/production-inspection-scopes",
        response_model=list[McpProductionScopeRead],
    )
    def list_mcp_production_inspection_scopes(
        client_id: UUID | None = None,
        target_id: UUID | None = None,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpProductionScopeRead]:
        return context.mcp_administration.list_production_inspection_scopes(
            client_id=client_id, target_id=target_id
        )

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}/production-inspection-scopes",
        response_model=McpProductionScopeRead,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_mcp_production_inspection_scope(
        client_id: UUID,
        target_id: UUID,
        payload: McpProductionScopeGrantRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionScopeRead:
        return context.mcp_administration.grant_production_inspection_scope(
            client_id,
            target_id,
            identity_generation=payload.identity_generation,
        )

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}/production-visual-scopes",
        response_model=McpProductionScopeRead,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_mcp_production_visual_scope(
        client_id: UUID,
        target_id: UUID,
        payload: McpProductionVisualScopeGrantRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionScopeRead:
        return context.mcp_administration.grant_production_visual_scope(
            client_id,
            target_id,
            identity_generation=payload.identity_generation,
            bridge_trust_revision=payload.bridge_trust_revision,
        )

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}/production-navigate-scopes",
        response_model=McpProductionScopeRead,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_mcp_production_navigate_scope(
        client_id: UUID,
        target_id: UUID,
        payload: McpProductionVisualScopeGrantRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionScopeRead:
        return context.mcp_administration.grant_production_navigate_scope(
            client_id,
            target_id,
            identity_generation=payload.identity_generation,
            bridge_trust_revision=payload.bridge_trust_revision,
        )

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}/production-transient-control-scopes",
        response_model=McpProductionScopeRead,
        status_code=status.HTTP_201_CREATED,
    )
    def grant_mcp_production_transient_control_scope(
        client_id: UUID,
        target_id: UUID,
        payload: McpProductionVisualScopeGrantRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionScopeRead:
        return context.mcp_administration.grant_production_transient_control_scope(
            client_id,
            target_id,
            identity_generation=payload.identity_generation,
            bridge_trust_revision=payload.bridge_trust_revision,
        )

    @app.post(
        "/api/mcp/production-inspection-scopes/{scope_id}/revoke",
        response_model=McpProductionScopeRead,
    )
    def revoke_mcp_production_inspection_scope(
        scope_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionScopeRead:
        return context.mcp_administration.revoke_production_inspection_scope(scope_id)

    @app.get(
        "/api/mcp/clients/{client_id}/development-sources",
        response_model=list[UUID],
    )
    def list_mcp_development_source_grants(
        client_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[UUID]:
        return list(
            context.mcp_administration.get_client(client_id).development_source_grants
        )

    @app.post(
        "/api/mcp/clients/{client_id}/development-sources/{source_id}",
        response_model=McpClientRead,
    )
    def grant_mcp_development_source(
        client_id: UUID,
        source_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.grant_development_source(client_id, source_id)

    @app.delete(
        "/api/mcp/clients/{client_id}/development-sources/{source_id}",
        response_model=McpClientRead,
    )
    def revoke_mcp_development_source(
        client_id: UUID,
        source_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.remove_development_source_grant(
            client_id, source_id
        )

    @app.get(
        "/api/mcp/clients/{client_id}/workspaces/eligible",
        response_model=list[McpEligibleWorkspaceRead],
    )
    def list_mcp_eligible_workspaces(
        client_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpEligibleWorkspaceRead]:
        return context.mcp_administration.list_eligible_unbound_workspaces(client_id)

    @app.get(
        "/api/mcp/clients/{client_id}/workspace-bindings",
        response_model=list[McpWorkspaceBindingRead],
    )
    def list_mcp_workspace_bindings(
        client_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpWorkspaceBindingRead]:
        return context.mcp_administration.list_workspace_bindings(client_id)

    @app.post(
        "/api/mcp/clients/{client_id}/workspaces/{workspace_id}/binding",
        response_model=McpWorkspaceBindingRead,
        status_code=status.HTTP_201_CREATED,
    )
    def bind_mcp_workspace(
        client_id: UUID,
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpWorkspaceBindingRead:
        return context.mcp_administration.bind_workspace(client_id, workspace_id)

    @app.get("/api/mcp/target-policies", response_model=list[McpTargetPolicyRead])
    def list_mcp_target_policies(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[McpTargetPolicyRead]:
        return context.mcp_administration.list_target_policies()

    @app.post(
        "/api/mcp/target-policies/{target_id}", response_model=McpTargetPolicyRead
    )
    def assign_mcp_target_policy(
        target_id: UUID,
        payload: McpPresetAssignmentRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpTargetPolicyRead:
        return context.mcp_administration.assign_target_preset(
            target_id, payload.preset_id
        )

    @app.get("/api/external-targets", response_model=list[ExternalTargetRead])
    def list_external_targets(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetRead]:
        return context.external_targets.list_targets()

    @app.post(
        "/api/external-targets",
        response_model=ExternalTargetRead,
        status_code=status.HTTP_201_CREATED,
    )
    def register_external_target(
        payload: ExternalTargetRegistrationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.external_targets.register(
            logical_name=payload.logical_name,
            protocol_kind=payload.protocol_kind,
            host=payload.host,
            port=payload.port,
            authentication_mode=payload.authentication_mode,
            configured_capabilities=payload.configured_capabilities,
            username=payload.username,
            password=payload.password,
        )

    @app.get("/api/external-targets/{target_id}", response_model=ExternalTargetRead)
    def get_external_target(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ExternalTargetRead:
        return context.external_targets.get(target_id)

    @app.get(
        "/api/external-targets/{target_id}/events",
        response_model=list[ExternalTargetEventRead],
    )
    def list_external_target_events(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetEventRead]:
        return context.external_targets.events(target_id)

    @app.put(
        "/api/external-targets/{target_id}/endpoint",
        response_model=ExternalTargetRead,
    )
    def edit_external_target_endpoint(
        target_id: UUID,
        payload: ExternalTargetEndpointRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        result = context.external_targets.edit_endpoint(
            target_id,
            protocol_kind=payload.protocol_kind,
            host=payload.host,
            port=payload.port,
        )
        try:
            context.external_bridges.mark_needs_reverification(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        return result

    @app.post(
        "/api/external-targets/{target_id}/credentials",
        response_model=ExternalTargetRead,
    )
    def rotate_external_target_credentials(
        target_id: UUID,
        payload: ExternalTargetCredentialRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        result = context.external_targets.rotate_credentials(
            target_id,
            authentication_mode=payload.authentication_mode,
            username=payload.username,
            password=payload.password,
        )
        try:
            context.external_bridges.mark_needs_reverification(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        return result

    @app.post(
        "/api/external-targets/{target_id}/probe",
        response_model=ExternalTargetRead,
    )
    def probe_external_target(
        target_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.external_targets.probe(target_id)

    @app.post(
        "/api/external-targets/{target_id}/confirm",
        response_model=ExternalTargetRead,
    )
    def confirm_external_target(
        target_id: UUID,
        payload: ExternalTargetConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        result = context.external_targets.confirm(target_id, payload.observation_id)
        try:
            context.external_bridges.mark_needs_reverification(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        return result

    @app.post(
        "/api/external-targets/{target_id}/enabled",
        response_model=ExternalTargetRead,
    )
    def set_external_target_enabled(
        target_id: UUID,
        payload: ExternalTargetEnabledRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.external_targets.set_enabled(target_id, enabled=payload.enabled)

    @app.post(
        "/api/external-targets/{target_id}/deactivate",
        response_model=ExternalTargetRead,
    )
    def deactivate_external_target(
        target_id: UUID,
        _payload: AgentWorkspacePromotionConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        try:
            context.external_bridges.revoke(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        return context.external_targets.deactivate(target_id)

    @app.get("/api/production-targets", response_model=list[ExternalTargetRead])
    def list_production_targets(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetRead]:
        return context.production_targets.list_targets()

    @app.get(
        "/api/production-operation-approvals",
        response_model=list[ProductionApprovalRead],
    )
    def list_production_operation_approvals(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ProductionApprovalRead]:
        return context.production_transient_approvals.list_queue()

    @app.post(
        "/api/production-operation-approvals/{approval_id}/approve",
        response_model=ProductionApprovalRead,
    )
    def approve_production_operation(
        approval_id: UUID,
        payload: ProductionApprovalConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionApprovalRead:
        return context.production_transient_approvals.approve(
            approval_id, action=payload.action
        )

    @app.post(
        "/api/production-operation-approvals/{approval_id}/deny",
        response_model=ProductionApprovalRead,
    )
    def deny_production_operation(
        approval_id: UUID,
        _payload: ProductionApprovalDecisionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionApprovalRead:
        return context.production_transient_approvals.deny(approval_id)

    @app.post(
        "/api/production-operation-approvals/{approval_id}/cancel",
        response_model=ProductionApprovalRead,
    )
    def cancel_production_operation(
        approval_id: UUID,
        _payload: ProductionApprovalDecisionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionApprovalRead:
        return context.production_transient_approvals.cancel(approval_id)

    @app.post(
        "/api/production-targets",
        response_model=ExternalTargetRead,
        status_code=status.HTTP_201_CREATED,
    )
    def register_production_target(
        payload: ExternalTargetRegistrationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.production_targets.register(
            logical_name=payload.logical_name,
            protocol_kind=payload.protocol_kind,
            host=payload.host,
            port=payload.port,
            authentication_mode=payload.authentication_mode,
            configured_capabilities=payload.configured_capabilities,
            username=payload.username,
            password=payload.password,
        )

    @app.get(
        "/api/production-targets/mutation-control",
        response_model=ProductionMutationControlRead,
    )
    def get_production_mutation_control(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ProductionMutationControlRead:
        return context.production_targets.mutation_control()

    @app.post(
        "/api/production-targets/mutation-control",
        response_model=ProductionMutationControlRead,
    )
    def set_production_mutation_control(
        payload: ProductionMutationBlockRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionMutationControlRead:
        return context.production_targets.set_mutation_block(blocked=payload.blocked)

    @app.get("/api/production-targets/{target_id}", response_model=ExternalTargetRead)
    def get_production_target(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ExternalTargetRead:
        return context.production_targets.get(target_id)

    @app.get(
        "/api/production-targets/{target_id}/events",
        response_model=list[ExternalTargetEventRead],
    )
    def list_production_target_events(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetEventRead]:
        return context.production_targets.events(target_id)

    @app.put(
        "/api/production-targets/{target_id}/endpoint",
        response_model=ExternalTargetRead,
    )
    def edit_production_target_endpoint(
        target_id: UUID,
        payload: ExternalTargetEndpointRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        result = context.production_targets.edit_endpoint(
            target_id,
            protocol_kind=payload.protocol_kind,
            host=payload.host,
            port=payload.port,
        )
        context.external_bridges.invalidate_production_native_identity(target_id)
        return result

    @app.post(
        "/api/production-targets/{target_id}/credentials",
        response_model=ExternalTargetRead,
    )
    def rotate_production_target_credentials(
        target_id: UUID,
        payload: ExternalTargetCredentialRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        result = context.production_targets.rotate_credentials(
            target_id,
            authentication_mode=payload.authentication_mode,
            username=payload.username,
            password=payload.password,
        )
        context.external_bridges.invalidate_production_native_identity(target_id)
        return result

    @app.post(
        "/api/production-targets/{target_id}/probe",
        response_model=ExternalTargetRead,
    )
    def probe_production_target(
        target_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        before = context.production_targets.get(target_id)
        result = context.production_targets.probe(target_id)
        if (
            before.production_policy is not None
            and result.production_policy is not None
            and before.production_policy.identity_generation
            != result.production_policy.identity_generation
        ):
            context.external_bridges.invalidate_production_native_identity(target_id)
        return result

    @app.post(
        "/api/production-targets/{target_id}/confirm-native",
        response_model=ExternalTargetRead,
    )
    def confirm_production_native_identity(
        target_id: UUID,
        payload: ExternalTargetConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.production_targets.confirm(target_id, payload.observation_id)

    @app.post(
        "/api/production-targets/{target_id}/confirm-designation",
        response_model=ExternalTargetRead,
    )
    def confirm_production_designation(
        target_id: UUID,
        payload: ProductionDesignationConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.production_targets.confirm_production_designation(
            target_id, payload.observation_id
        )

    @app.post(
        "/api/production-targets/{target_id}/enabled",
        response_model=ExternalTargetRead,
    )
    def set_production_target_enabled(
        target_id: UUID,
        payload: ExternalTargetEnabledRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.production_targets.set_enabled(
            target_id, enabled=payload.enabled
        )

    @app.post(
        "/api/production-targets/{target_id}/mutation-suspension",
        response_model=ExternalTargetRead,
    )
    def set_production_target_mutation_suspension(
        target_id: UUID,
        payload: ProductionTargetMutationSuspensionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return context.production_targets.set_target_mutation_suspension(
            target_id, suspended=payload.suspended
        )

    @app.post(
        "/api/production-targets/{target_id}/deactivate",
        response_model=ExternalTargetRead,
    )
    def deactivate_production_target(
        target_id: UUID,
        _payload: AgentWorkspacePromotionConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        try:
            context.external_bridges.revoke(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        return context.production_targets.deactivate(target_id)

    @app.get(
        "/api/production-bridge-builds",
        response_model=list[ReviewedBridgeBuildRead],
    )
    def list_production_bridge_builds(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ReviewedBridgeBuildRead]:
        return context.external_bridges.list_reviewed_builds()

    @app.post(
        "/api/production-bridge-builds",
        response_model=ReviewedBridgeBuildRead,
        status_code=status.HTTP_201_CREATED,
    )
    def register_production_bridge_build(
        payload: ReviewedBridgeBuildRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReviewedBridgeBuildRead:
        return context.external_bridges.register_reviewed_build(
            semantic_fingerprint=payload.semantic_fingerprint,
            zip_sha256=payload.zip_sha256,
            addon_id=payload.addon_id,
            addon_version=payload.addon_version,
            protocol_version=payload.protocol_version,
            capabilities=payload.capabilities,
            supported_kodi_versions=payload.supported_kodi_versions,
            source_reference=payload.source_reference,
        )

    @app.post(
        "/api/production-bridge-builds/{build_id}/retire",
        response_model=ReviewedBridgeBuildRead,
    )
    def retire_production_bridge_build(
        build_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReviewedBridgeBuildRead:
        return context.external_bridges.retire_reviewed_build(build_id)

    @app.get(
        "/api/production-targets/{target_id}/bridge",
        response_model=ExternalBridgeStatus,
    )
    def get_production_bridge_status(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.status(target_id)

    @app.post(
        "/api/production-targets/{target_id}/bridge/http-acknowledgements",
        response_model=ProductionHttpAcknowledgementRead,
        status_code=status.HTTP_201_CREATED,
    )
    def acknowledge_production_bridge_http_risk(
        target_id: UUID,
        payload: ProductionBridgeHttpAcknowledgementRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionHttpAcknowledgementRead:
        return context.external_bridges.acknowledge_production_http_risk(
            target_id, bridge_port=payload.bridge_port
        )

    @app.post(
        "/api/production-targets/{target_id}/native-control/http-acknowledgements",
        response_model=ProductionNativeControlAcknowledgementRead,
        status_code=status.HTTP_201_CREATED,
    )
    def acknowledge_production_native_control_http_risk(
        target_id: UUID,
        _payload: ProductionNativeControlHttpAcknowledgementRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionNativeControlAcknowledgementRead:
        return context.production_native_control_acknowledgements.acknowledge(target_id)

    @app.post(
        "/api/production-targets/{target_id}/bridge/pairing-token",
        response_model=PairingTokenResult,
        status_code=status.HTTP_201_CREATED,
    )
    def generate_production_bridge_pairing_token(
        target_id: UUID,
        payload: ExternalBridgePairingTokenRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> PairingTokenResult:
        return context.external_bridges.generate_pairing_token(
            target_id, bridge_port=payload.bridge_port
        )

    @app.post(
        "/api/production-targets/{target_id}/bridge/pair",
        response_model=ExternalBridgeStatus,
    )
    def pair_production_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.attempt_pairing(target_id)

    @app.post(
        "/api/production-targets/{target_id}/bridge/confirm",
        response_model=ExternalBridgeStatus,
    )
    def confirm_production_bridge(
        target_id: UUID,
        _payload: ProductionBridgeConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.confirm_pairing(target_id)

    @app.post(
        "/api/production-targets/{target_id}/bridge/reverify",
        response_model=ExternalBridgeStatus,
    )
    def reverify_production_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.reverify(target_id)

    @app.post(
        "/api/production-targets/{target_id}/bridge/revoke",
        response_model=ExternalBridgeStatus,
    )
    def revoke_production_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.revoke(target_id)

    @app.get(
        "/api/external-targets/{target_id}/bridge",
        response_model=ExternalBridgeStatus,
    )
    def get_external_bridge_status(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.status(target_id)

    @app.post(
        "/api/external-targets/{target_id}/bridge/pairing-token",
        response_model=PairingTokenResult,
        status_code=status.HTTP_201_CREATED,
    )
    def generate_external_bridge_pairing_token(
        target_id: UUID,
        payload: ExternalBridgePairingTokenRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> PairingTokenResult:
        return context.external_bridges.generate_pairing_token(
            target_id, bridge_port=payload.bridge_port
        )

    @app.post(
        "/api/external-targets/{target_id}/bridge/pair",
        response_model=ExternalBridgeStatus,
    )
    def pair_external_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.attempt_pairing(target_id)

    @app.post(
        "/api/external-targets/{target_id}/bridge/confirm",
        response_model=ExternalBridgeStatus,
    )
    def confirm_external_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.confirm_pairing(target_id)

    @app.post(
        "/api/external-targets/{target_id}/bridge/reverify",
        response_model=ExternalBridgeStatus,
    )
    def reverify_external_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.reverify(target_id)

    @app.post(
        "/api/external-targets/{target_id}/bridge/needs-reverification",
        response_model=ExternalBridgeStatus,
    )
    def mark_external_bridge_needs_reverification(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.mark_needs_reverification(target_id)

    @app.post(
        "/api/external-targets/{target_id}/bridge/revoke",
        response_model=ExternalBridgeStatus,
    )
    def revoke_external_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.revoke(target_id)

    @app.post(
        "/api/external-targets/{target_id}/development-baselines",
        response_model=ExternalBaselineResult,
        status_code=status.HTTP_201_CREATED,
    )
    def establish_external_development_baseline(
        target_id: UUID,
        payload: ExternalBaselineRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBaselineResult:
        return context.mcp_workspace_development.establish_external_baseline(
            target_id=target_id, source_id=payload.source_id
        )

    @app.post("/api/external-targets/{target_id}/deployments/{operation_id}/reconcile")
    def reconcile_external_deployment(
        target_id: UUID,
        operation_id: UUID,
        _payload: ExternalDeploymentReconciliationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, str]:
        with session_factory() as session:
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(operation_id)
            )
        if operation is None or operation.target_id != str(target_id):
            raise HTTPException(status_code=404, detail="deployment_not_found")
        classification = (
            context.mcp_workspace_development.reconcile_external_deployment(
                operation_id
            )
        )
        return {"classification": classification}

    @app.get("/api/runtime-matrix", response_model=RuntimeMatrixResponse)
    def runtime_matrix(
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> RuntimeMatrixResponse:
        return context.matrix.list_matrix(
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )

    @app.get(
        "/api/targets/{target_id}/runtime-selection",
        response_model=TargetRuntimeSelection,
    )
    def target_runtime_selection(
        target_id: UUID,
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> TargetRuntimeSelection:
        return context.matrix.resolve_target(
            target_id,
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )

    @app.get("/api/instances", response_model=list[ManagedRuntimeStatus])
    def list_instances(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ManagedRuntimeStatus]:
        return context.status.list_statuses()

    @app.get("/api/development-sources", response_model=list[DevelopmentSourceSummary])
    def list_development_sources(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[DevelopmentSourceSummary]:
        return context.development_sources.list_sources()

    @app.get("/api/agent-workspaces", response_model=list[AgentWorkspaceRead])
    def list_agent_workspaces(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspaceRead]:
        return context.agent_workspaces.list_workspaces()

    @app.post(
        "/api/agent-workspaces",
        response_model=AgentWorkspaceCreationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_agent_workspace(
        body: AgentWorkspaceCreationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceCreationAccepted:
        operation = context.agent_workspaces.queue_creation(body.development_source_id)
        context.agent_workspace_executor.submit(operation.operation_id)
        return AgentWorkspaceCreationAccepted(
            operation=operation,
            status_url=f"/api/agent-workspace-creation-operations/{operation.operation_id}",
            workspace_url=f"/api/agent-workspaces/{operation.workspace_id}",
        )

    @app.get(
        "/api/agent-workspace-creation-operations/{operation_id}",
        response_model=AgentWorkspaceCreationStatus,
    )
    def get_agent_workspace_creation(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspaceCreationStatus:
        return context.agent_workspaces.creation_status(operation_id)

    @app.get("/api/agent-workspaces/{workspace_id}", response_model=AgentWorkspaceRead)
    def get_agent_workspace(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspaceRead:
        return context.agent_workspaces.get(workspace_id)

    @app.post(
        "/api/agent-workspaces/{workspace_id}/abandon",
        response_model=AgentWorkspaceRead,
    )
    def abandon_agent_workspace(
        workspace_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceRead:
        return context.agent_workspaces.abandon(workspace_id)

    @app.post(
        "/api/agent-workspaces/{workspace_id}/cleanup",
        response_model=AgentWorkspaceCleanupAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def cleanup_agent_workspace(
        workspace_id: UUID,
        _body: AgentWorkspaceCleanupConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceCleanupAccepted:
        operation = context.agent_workspace_cleanups.queue(workspace_id)
        if operation.state.value == "queued":
            context.agent_workspace_cleanup_executor.submit(operation.operation_id)
        return AgentWorkspaceCleanupAccepted(
            operation=operation,
            status_url=f"/api/agent-workspace-cleanup-operations/{operation.operation_id}",
        )

    @app.get(
        "/api/agent-workspace-cleanup-operations/{operation_id}",
        response_model=AgentWorkspaceCleanupStatus,
    )
    def get_agent_workspace_cleanup(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspaceCleanupStatus:
        return context.agent_workspace_cleanups.get(operation_id)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/tree",
        response_model=list[AgentWorkspaceTreeEntryRead],
    )
    def inspect_agent_workspace_root(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspaceTreeEntryRead]:
        return context.agent_workspaces.inspect_directory(workspace_id, "")

    @app.get(
        "/api/agent-workspaces/{workspace_id}/tree/{relative_path:path}",
        response_model=list[AgentWorkspaceTreeEntryRead],
    )
    def inspect_agent_workspace_directory(
        workspace_id: UUID,
        relative_path: str,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspaceTreeEntryRead]:
        return context.agent_workspaces.inspect_directory(workspace_id, relative_path)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/file/{relative_path:path}",
        response_model=AgentWorkspaceFileRead,
    )
    def read_agent_workspace_file(
        workspace_id: UUID,
        relative_path: str,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspaceFileRead:
        return context.agent_workspaces.read_file(workspace_id, relative_path)

    @app.post(
        "/api/agent-workspaces/{workspace_id}/files",
        response_model=AgentWorkspaceMutationRead,
        status_code=status.HTTP_200_OK,
    )
    def create_agent_workspace_file(
        workspace_id: UUID,
        body: AgentWorkspaceTextWriteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceMutationRead:
        return context.agent_workspaces.create_text_file(
            workspace_id,
            expected_generation=body.expected_generation,
            relative_path=body.path,
            content=body.content,
        )

    @app.put(
        "/api/agent-workspaces/{workspace_id}/files",
        response_model=AgentWorkspaceMutationRead,
        status_code=status.HTTP_200_OK,
    )
    def replace_agent_workspace_file(
        workspace_id: UUID,
        body: AgentWorkspaceTextWriteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceMutationRead:
        return context.agent_workspaces.replace_text_file(
            workspace_id,
            expected_generation=body.expected_generation,
            relative_path=body.path,
            content=body.content,
        )

    @app.post(
        "/api/agent-workspaces/{workspace_id}/patches",
        response_model=AgentWorkspaceMutationRead,
        status_code=status.HTTP_200_OK,
    )
    def patch_agent_workspace_file(
        workspace_id: UUID,
        body: AgentWorkspaceExactPatchRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspaceMutationRead:
        return context.agent_workspaces.apply_exact_patch(
            workspace_id,
            expected_generation=body.expected_generation,
            relative_path=body.path,
            expected_text=body.expected_text,
            replacement_text=body.replacement_text,
        )

    @app.get(
        "/api/agent-workspaces/{workspace_id}/changes",
        response_model=AgentWorkspaceChangesRead,
    )
    def list_agent_workspace_changes(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspaceChangesRead:
        return context.agent_workspaces.list_changes(workspace_id)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/activity",
        response_model=list[AgentWorkspaceActivityRead],
    )
    def list_agent_workspace_activity(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspaceActivityRead]:
        return context.agent_workspaces.list_activity(workspace_id)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/lifecycle-activity",
        response_model=list[AgentWorkspaceLifecycleActivityRead],
    )
    def list_agent_workspace_lifecycle_activity(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspaceLifecycleActivityRead]:
        return context.agent_workspaces.list_lifecycle_activity(workspace_id)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/promotion-targets",
        response_model=list[AgentWorkspacePromotionTargetRead],
    )
    def list_agent_workspace_promotion_targets(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AgentWorkspacePromotionTargetRead]:
        return context.agent_workspace_promotions.eligible_targets(workspace_id)

    @app.post(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-plans",
        response_model=AgentWorkspacePromotionPlanRead,
        status_code=status.HTTP_201_CREATED,
    )
    def prepare_agent_workspace_promotion(
        instance_id: UUID,
        workspace_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspacePromotionPlanRead:
        return context.agent_workspace_promotions.prepare(instance_id, workspace_id)

    @app.get(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-plans/{plan_id}",
        response_model=AgentWorkspacePromotionPlanRead,
    )
    def get_agent_workspace_promotion_plan(
        instance_id: UUID,
        workspace_id: UUID,
        plan_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspacePromotionPlanRead:
        return context.agent_workspace_promotions.get_plan(
            instance_id, workspace_id, plan_id
        )

    @app.post(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-plans/{plan_id}/approve-and-execute",
        response_model=AgentWorkspacePromotionOperationStatus,
        status_code=status.HTTP_200_OK,
    )
    def approve_agent_workspace_promotion(
        instance_id: UUID,
        workspace_id: UUID,
        plan_id: UUID,
        _body: AgentWorkspacePromotionConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspacePromotionOperationStatus:
        return context.agent_workspace_promotions.approve_and_execute(
            instance_id, workspace_id, plan_id
        )

    @app.post(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-plans/{plan_id}/reject",
        response_model=AgentWorkspacePromotionPlanRead,
        status_code=status.HTTP_200_OK,
    )
    def reject_agent_workspace_promotion(
        instance_id: UUID,
        workspace_id: UUID,
        plan_id: UUID,
        _body: AgentWorkspacePromotionConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AgentWorkspacePromotionPlanRead:
        return context.agent_workspace_promotions.reject(
            instance_id, workspace_id, plan_id
        )

    @app.get(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-operations/{operation_id}",
        response_model=AgentWorkspacePromotionOperationStatus,
    )
    def get_agent_workspace_promotion_operation(
        instance_id: UUID,
        workspace_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AgentWorkspacePromotionOperationStatus:
        return context.agent_workspace_promotions.get_operation(
            instance_id, workspace_id, operation_id
        )

    @app.get(
        "/api/instances/{instance_id}/agent-workspaces/{workspace_id}/promotion-operations/{operation_id}/verification",
        response_model=PromotionVerificationStatus,
    )
    def get_agent_workspace_promotion_verification(
        instance_id: UUID,
        workspace_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> PromotionVerificationStatus:
        result = context.promotion_verifications.get_for_promotion(operation_id)
        if (
            result.target_instance_id != instance_id
            or result.workspace_id != workspace_id
        ):
            raise PromotionVerificationError(
                "promotion_verification_not_found", "Kodi verification was not found."
            )
        return result

    @app.get("/api/addon-repositories", response_model=list[AddonRepositorySourceRead])
    def list_addon_repositories(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AddonRepositorySourceRead]:
        return context.addon_repositories.list()

    @app.post(
        "/api/addon-repositories",
        response_model=AddonRepositorySourceRead,
        status_code=status.HTTP_201_CREATED,
    )
    def register_addon_repository(
        body: AddonRepositoryRegistrationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonRepositorySourceRead:
        return context.addon_repositories.register(body.logical_name, body.index_url)

    @app.get(
        "/api/addon-repositories/{repository_id}",
        response_model=AddonRepositorySourceRead,
    )
    def get_addon_repository(
        repository_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonRepositorySourceRead:
        return context.addon_repositories.get(repository_id)

    @app.get(
        "/api/instances/{instance_id}/addon-repositories",
        response_model=list[AddonRepositorySourceRead],
    )
    def list_instance_addon_repositories(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AddonRepositorySourceRead]:
        return context.addon_repositories.list_for_instance(instance_id)

    @app.post(
        "/api/instances/{instance_id}/addon-repositories/{repository_id}/attach",
        response_model=AddonRepositorySourceRead,
    )
    def attach_addon_repository(
        instance_id: UUID,
        repository_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonRepositorySourceRead:
        return context.addon_repositories.attach(instance_id, repository_id)

    @app.post(
        "/api/instances/{instance_id}/addon-repositories/{repository_id}/detach",
        response_model=AddonRepositorySourceRead,
    )
    def detach_addon_repository(
        instance_id: UUID,
        repository_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonRepositorySourceRead:
        return context.addon_repositories.detach(instance_id, repository_id)

    @app.post(
        "/api/addon-repositories/{repository_id}/refresh",
        response_model=AddonRepositoryRefreshAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def refresh_addon_repository(
        repository_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonRepositoryRefreshAccepted:
        operation = context.addon_repository_refreshes.queue(repository_id)
        context.addon_repository_refresh_executor.submit(operation.operation_id)
        return AddonRepositoryRefreshAccepted(
            operation=operation,
            status_url=f"/api/addon-repository-refresh-operations/{operation.operation_id}",
        )

    @app.get(
        "/api/addon-repository-refresh-operations/{operation_id}",
        response_model=AddonRepositoryRefreshStatus,
    )
    def addon_repository_refresh_status(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonRepositoryRefreshStatus:
        return context.addon_repository_refreshes.get(operation_id)

    @app.get(
        "/api/addon-repositories/{repository_id}/candidates",
        response_model=list[AddonRepositoryCandidateRead],
    )
    def list_addon_repository_candidates(
        repository_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AddonRepositoryCandidateRead]:
        return context.addon_repository_refreshes.candidates(repository_id)

    @app.post(
        "/api/addon-repositories/{repository_id}/candidates/{candidate_id}/fetch",
        response_model=AddonRepositoryFetchAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def fetch_addon_repository_candidate(
        repository_id: UUID,
        candidate_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonRepositoryFetchAccepted:
        operation = context.addon_repository_fetches.queue(repository_id, candidate_id)
        context.addon_repository_fetch_executor.submit(operation.operation_id)
        return AddonRepositoryFetchAccepted(
            operation=operation,
            status_url=f"/api/addon-repository-fetch-operations/{operation.operation_id}",
        )

    @app.get(
        "/api/addon-repository-fetch-operations/{operation_id}",
        response_model=AddonRepositoryFetchStatus,
    )
    def addon_repository_fetch_status(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonRepositoryFetchStatus:
        return context.addon_repository_fetches.get(operation_id)

    @app.post(
        "/api/development-sources",
        response_model=DevelopmentSourceRead,
        status_code=status.HTTP_201_CREATED,
    )
    def register_development_source(
        body: DevelopmentSourceRegistrationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> DevelopmentSourceRead:
        return context.development_source_administration.register_source(
            logical_name=body.logical_name,
            relative_locator=body.relative_locator,
        )

    @app.get("/api/addon-zips", response_model=list[AddonZipArtifactSummary])
    def list_addon_zips(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[AddonZipArtifactSummary]:
        return context.addon_zips.list_artifacts()

    @app.get(
        "/api/addon-zip-import-operations/{operation_id}",
        response_model=AddonZipImportStatus,
    )
    def addon_zip_import_operation(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonZipImportStatus:
        operation = context.addon_zip_imports.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.addon_zips.import_status(operation)

    @app.post(
        "/api/addon-zips",
        response_model=AddonZipImportAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def upload_addon_zip(
        request: Request,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonZipImportAccepted:
        artifact_id = uuid4()
        logical_label = await _stream_addon_zip_multipart(
            request, settings.data_root, artifact_id
        )
        operation = context.addon_zip_imports.queue(artifact_id, logical_label)
        context.addon_zip_import_executor.submit(operation.id)
        return AddonZipImportAccepted(
            operation=context.addon_zips.import_status(operation),
            status_url=f"/api/addon-zip-import-operations/{operation.id}",
        )

    @app.get(
        "/api/development-sources/{source_id}",
        response_model=DevelopmentSourceDetail,
    )
    def get_development_source(
        source_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> DevelopmentSourceDetail:
        return context.development_sources.get_source(source_id)

    @app.get("/api/instances/{instance_id}", response_model=ManagedRuntimeStatus)
    def get_instance(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedRuntimeStatus:
        return context.status.get(instance_id)

    @app.get(
        "/api/instances/{instance_id}/visual-status",
        response_model=ManagedScreenshotStatus,
    )
    def get_instance_visual_status(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedScreenshotStatus:
        return context.screenshots.get_status(instance_id)

    @app.websocket("/api/instances/{instance_id}/display")
    async def stream_instance_display(websocket: WebSocket, instance_id: UUID) -> None:
        token = websocket.cookies.get(settings.web_session_cookie_name)
        if context.auth.resolve(token) is None:
            await websocket.close(code=4401, reason="display_session_expired")
            return
        if websocket.scope.get("query_string", b""):
            await websocket.close(code=4403, reason="display_unavailable")
            return
        if not _websocket_same_origin(websocket):
            await websocket.close(code=4403, reason="display_unavailable")
            return
        try:
            display_session = context.live_display.begin(instance_id)
        except ManagedInstanceNotFoundError:
            await websocket.close(code=4404, reason="managed_instance_not_found")
            return
        except LiveDisplayError as error:
            await websocket.close(code=4409, reason=error.code)
            return

        end_reason = "display_accept_failed"
        try:
            await websocket.accept()
            context.live_display.mark_established(display_session)
            end_reason = "display_unavailable"
            while True:
                if context.auth.resolve(token) is None:
                    raise LiveDisplayError(
                        "display_session_expired",
                        "The authenticated Web session is no longer valid.",
                    )
                capture_started = asyncio.get_running_loop().time()
                try:
                    frame = await asyncio.to_thread(
                        context.live_display.capture, display_session
                    )
                except LiveDisplayFrameSkipped:
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=1.0
                        )
                    except TimeoutError:
                        continue
                    if message["type"] == "websocket.disconnect":
                        end_reason = "display_stopped"
                        break
                    raise LiveDisplayError(
                        "display_unavailable",
                        "Browser data messages are not accepted on live display.",
                    ) from None
                if context.auth.resolve(token) is None:
                    raise LiveDisplayError(
                        "display_session_expired",
                        "The authenticated Web session is no longer valid.",
                    )
                try:
                    await asyncio.wait_for(websocket.send_bytes(frame.png), timeout=2.0)
                except TimeoutError as error:
                    raise LiveDisplayError(
                        "display_backend_failed",
                        "The browser did not accept the frame in time.",
                    ) from error
                context.live_display.record_delivery(display_session, frame.byte_size)
                delay = max(
                    0.05,
                    capture_started + 1.0 - asyncio.get_running_loop().time(),
                )
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=delay)
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    end_reason = "display_stopped"
                    break
                raise LiveDisplayError(
                    "display_unavailable",
                    "Browser data messages are not accepted on live display.",
                )
        except LiveDisplayError as error:
            end_reason = error.code
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1008, reason=error.code)
        except WebSocketDisconnect:
            end_reason = "display_stopped"
        finally:
            context.live_display.end(display_session, end_reason)

    @app.post(
        "/api/instances/{instance_id}/screenshots",
        response_model=ManagedScreenshotCaptureResult,
    )
    def capture_instance_screenshot(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedScreenshotCaptureResult:
        return context.screenshots.capture(instance_id)

    @app.post(
        "/api/instances/{instance_id}/remote-actions",
        response_model=BrowserRemoteResult,
    )
    def invoke_browser_remote(
        instance_id: UUID,
        body: BrowserRemoteRequest,
        authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> BrowserRemoteResult:
        context.browser_remote_rate_limiter.require_allowed(
            authenticated.csrf_token, instance_id
        )
        return context.browser_remote.invoke(
            instance_id, body.action, actor=RemoteActorContext.web_local_admin()
        )

    @app.get("/api/instances/{instance_id}/screenshots/latest")
    def get_latest_instance_screenshot(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        return Response(
            content=context.screenshots.latest_png(instance_id),
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            },
        )

    @app.get(
        "/api/instances/{instance_id}/addons",
        response_model=ManagedInstanceAddonInventory,
    )
    def get_instance_addons(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceAddonInventory:
        return context.addon_inventory.get(instance_id)

    @app.get(
        "/api/instances/{instance_id}/activity",
        response_model=list[ManagedInstanceActivityItem],
    )
    def get_instance_activity(
        instance_id: UUID,
        limit: int | None = Query(default=None, ge=1, le=1000),
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ManagedInstanceActivityItem]:
        return context.activity.list_for_live_instance(instance_id, limit=limit)

    @app.get(
        "/api/instances/{instance_id}/development-source-deployment-operations/"
        "{operation_id}",
        response_model=DevelopmentSourceDeploymentStatus,
    )
    def development_source_deployment_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> DevelopmentSourceDeploymentStatus:
        operation = context.development_source_deployment_operations.get(operation_id)
        if operation is None or operation.original_managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.development_source_deployment_operations.to_status(operation)

    @app.post(
        "/api/instances/{instance_id}/development-sources/{source_id}/attach",
        response_model=AttachedDevelopmentSourceRead,
        status_code=status.HTTP_200_OK,
    )
    def attach_development_source(
        instance_id: UUID,
        source_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AttachedDevelopmentSourceRead:
        return context.development_source_attachments.attach_source(
            instance_id, source_id
        )

    @app.post(
        "/api/instances/{instance_id}/development-sources/{source_id}/detach",
        response_model=AttachedDevelopmentSourceRead,
        status_code=status.HTTP_200_OK,
    )
    def detach_development_source(
        instance_id: UUID,
        source_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AttachedDevelopmentSourceRead:
        return context.development_source_attachments.detach_source(
            instance_id, source_id
        )

    @app.post(
        "/api/instances/{instance_id}/development-sources/{source_id}/copy-sync",
        response_model=DevelopmentSourceDeploymentAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def copy_sync_development_source(
        instance_id: UUID,
        source_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> DevelopmentSourceDeploymentAccepted:
        operation = context.development_source_deployment_operations.queue(
            instance_id, source_id
        )
        context.development_source_deployment_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/"
            "development-source-deployment-operations/"
            f"{operation.operation_id}"
        )
        return DevelopmentSourceDeploymentAccepted(
            operation=operation, status_url=status_url
        )

    @app.get(
        "/api/instances/{instance_id}/addon-zip-installation-operations/{operation_id}",
        response_model=AddonZipInstallationStatus,
    )
    def addon_zip_installation_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonZipInstallationStatus:
        operation = context.addon_zip_installations.get(operation_id)
        if operation is None or operation.original_managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.addon_zip_installations.to_status(operation)

    @app.post(
        "/api/instances/{instance_id}/addon-zips/{artifact_id}/install",
        response_model=AddonZipInstallationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_addon_zip(
        instance_id: UUID,
        artifact_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonZipInstallationAccepted:
        operation = context.addon_zip_installations.queue(instance_id, artifact_id)
        context.addon_zip_installation_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/addon-zip-installation-operations/"
            f"{operation.operation_id}"
        )
        return AddonZipInstallationAccepted(operation=operation, status_url=status_url)

    @app.post(
        "/api/instances/{instance_id}/repository-artifacts/{artifact_id}/install",
        response_model=AddonZipInstallationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_addon_repository_artifact(
        instance_id: UUID,
        artifact_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonZipInstallationAccepted:
        operation = context.addon_zip_installations.queue_repository(
            instance_id, artifact_id
        )
        context.addon_zip_installation_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/addon-zip-installation-operations/"
            f"{operation.operation_id}"
        )
        return AddonZipInstallationAccepted(operation=operation, status_url=status_url)

    @app.post(
        "/api/instances/{instance_id}/repository-artifacts/{artifact_id}/replacement-plans",
        response_model=RepositoryReplacementPlanRead,
        status_code=status.HTTP_201_CREATED,
    )
    def prepare_repository_replacement(
        instance_id: UUID,
        artifact_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> RepositoryReplacementPlanRead:
        return context.repository_replacements.prepare(instance_id, artifact_id)

    @app.get(
        "/api/instances/{instance_id}/replacement-plans/{plan_id}",
        response_model=RepositoryReplacementPlanRead,
    )
    def repository_replacement_plan(
        instance_id: UUID,
        plan_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> RepositoryReplacementPlanRead:
        return context.repository_replacements.get_plan_for(instance_id, plan_id)

    @app.post(
        "/api/instances/{instance_id}/replacement-plans/{plan_id}/approve-and-execute",
        response_model=RepositoryReplacementAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_repository_replacement(
        instance_id: UUID,
        plan_id: UUID,
        _body: RepositoryReplacementConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> RepositoryReplacementAccepted:
        operation = context.repository_replacements.approve_and_queue(
            instance_id, plan_id
        )
        context.repository_replacement_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/repository-replacement-operations/"
            f"{operation.operation_id}"
        )
        return RepositoryReplacementAccepted(operation=operation, status_url=status_url)

    @app.get(
        "/api/instances/{instance_id}/repository-replacement-operations/{operation_id}",
        response_model=RepositoryReplacementStatus,
    )
    def repository_replacement_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> RepositoryReplacementStatus:
        return context.repository_replacements.get_for_instance(
            instance_id, operation_id
        )

    @app.post(
        "/api/instances/{instance_id}/addon-zips/{artifact_id}/enable",
        response_model=AddonZipEnablementStatus,
        status_code=status.HTTP_200_OK,
    )
    def enable_addon_zip(
        instance_id: UUID,
        artifact_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonZipEnablementStatus:
        return context.addon_zip_enablement.enable(instance_id, artifact_id)

    @app.get(
        "/api/instances/{instance_id}/addon-zip-smoke-test-operations/{operation_id}",
        response_model=AddonZipSmokeTestStatus,
    )
    def addon_zip_smoke_test_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AddonZipSmokeTestStatus:
        operation = context.addon_zip_smoke_test_operations.get(operation_id)
        if operation is None or operation.original_managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.addon_zip_smoke_test_operations.to_status(operation)

    @app.post(
        "/api/instances/{instance_id}/addon-zips/{artifact_id}/smoke-test",
        response_model=AddonZipSmokeTestAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def smoke_test_addon_zip(
        instance_id: UUID,
        artifact_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> AddonZipSmokeTestAccepted:
        operation = context.addon_zip_smoke_test_operations.queue(
            instance_id, artifact_id
        )
        context.addon_zip_smoke_test_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/addon-zip-smoke-test-operations/"
            f"{operation.operation_id}"
        )
        return AddonZipSmokeTestAccepted(operation=operation, status_url=status_url)

    @app.post(
        "/api/instances/{instance_id}/development-sources/{source_id}/enable",
        response_model=DevelopmentSourceEnablementStatus,
        status_code=status.HTTP_200_OK,
    )
    def enable_development_source(
        instance_id: UUID,
        source_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> DevelopmentSourceEnablementStatus:
        return context.development_source_enablement.enable(instance_id, source_id)

    @app.get(
        "/api/instances/{instance_id}/development-source-smoke-test-operations/"
        "{operation_id}",
        response_model=DevelopmentSourceSmokeTestStatus,
    )
    def development_source_smoke_test_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> DevelopmentSourceSmokeTestStatus:
        operation = context.development_source_smoke_test_operations.get(operation_id)
        if operation is None or operation.original_managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.development_source_smoke_test_operations.to_status(operation)

    @app.post(
        "/api/instances/{instance_id}/development-sources/{source_id}/smoke-test",
        response_model=DevelopmentSourceSmokeTestAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def smoke_test_development_source(
        instance_id: UUID,
        source_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> DevelopmentSourceSmokeTestAccepted:
        operation = context.development_source_smoke_test_operations.queue(
            instance_id, source_id
        )
        context.development_source_smoke_test_executor.submit(operation.operation_id)
        status_url = (
            f"/api/instances/{instance_id}/"
            "development-source-smoke-test-operations/"
            f"{operation.operation_id}"
        )
        return DevelopmentSourceSmokeTestAccepted(
            operation=operation, status_url=status_url
        )

    @app.get(
        "/api/instances/{instance_id}/reset-status",
        response_model=ManagedInstanceResetStatus,
    )
    def get_instance_reset_status(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceResetStatus:
        return context.reset_status.get(instance_id)

    @app.get(
        "/api/instances/{instance_id}/reconciliation",
        response_model=ManagedInstanceReconciliationStatus,
    )
    def get_instance_reconciliation(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceReconciliationStatus:
        return context.reconciliation.status(instance_id)

    @app.get(
        "/api/snapshots/{snapshot_id}/reconciliation",
        response_model=SnapshotReconciliationStatus,
    )
    def get_snapshot_reconciliation(
        snapshot_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotReconciliationStatus:
        return context.snapshot_reconciliation.status(snapshot_id)

    @app.get(
        "/api/instances/{instance_id}/snapshots",
        response_model=list[SnapshotHistoryItem],
    )
    def list_snapshots(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[SnapshotHistoryItem]:
        return context.snapshot_status.list_for_instance(instance_id)

    @app.get("/api/snapshots/preserved", response_model=list[SnapshotHistoryItem])
    def list_preserved_snapshots(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[SnapshotHistoryItem]:
        return context.snapshot_status.list_preserved()

    @app.get(
        "/api/instance-delete-operations/{operation_id}",
        response_model=ManagedInstanceDeleteStatus,
    )
    def instance_delete_operation(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceDeleteStatus:
        operation = context.instance_delete_operations.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.instance_delete_operations.to_status(operation)
        if operation.reconciliation_required:
            result = result.model_copy(
                update={
                    "reconciliation": (
                        context.instance_delete_operations.inspect_reconciliation(
                            operation, broker=broker, data_root=settings.data_root
                        )
                    )
                }
            )
        return result

    @app.get(
        "/api/instances/{instance_id}/snapshot-create-operations/{operation_id}",
        response_model=SnapshotCreateStatus,
    )
    def snapshot_create_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotCreateStatus:
        operation = context.snapshot_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.snapshot_operations.to_status(operation)

    @app.get(
        "/api/instances/{instance_id}/snapshot-restore-operations/{operation_id}",
        response_model=SnapshotRestoreStatus,
    )
    def snapshot_restore_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotRestoreStatus:
        operation = context.snapshot_restore_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.snapshot_restore_operations.to_status(operation)
        if operation.reconciliation_required:
            observation = context.snapshot_restore_operations.inspect_reconciliation(
                operation, broker
            )
            result = result.model_copy(update={"reconciliation_state": observation})
        return result

    @app.get(
        "/api/instances/{instance_id}/snapshot-delete-operations/{operation_id}",
        response_model=SnapshotDeleteStatus,
    )
    def snapshot_delete_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotDeleteStatus:
        operation = context.snapshot_delete_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.snapshot_delete_operations.to_status(operation)
        if operation.reconciliation_required:
            result = result.model_copy(
                update={
                    "reconciliation_state": (
                        context.snapshot_delete_operations.inspect_reconciliation(
                            operation, data_root=settings.data_root
                        )
                    )
                }
            )
        return result

    @app.post(
        "/api/instances/{instance_id}/snapshots",
        response_model=SnapshotCreateAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_create(
        instance_id: UUID,
        body: SnapshotCreateRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotCreateAccepted:
        context.status.get(instance_id)
        queued = context.snapshot_operations.queue(instance_id, label=body.label)
        context.snapshot_executor.submit(queued.operation.operation_id)
        return SnapshotCreateAccepted(
            snapshot_id=queued.snapshot.id,
            operation=queued.operation,
        )

    @app.post(
        "/api/instances/{instance_id}/snapshots/{snapshot_id}/restore",
        response_model=SnapshotRestoreAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_restore(
        instance_id: UUID,
        snapshot_id: UUID,
        _body: SnapshotRestoreRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotRestoreAccepted:
        with session_factory() as session:
            routed_snapshot = SnapshotRepository(session).get(snapshot_id)
        if (
            routed_snapshot is not None
            and routed_snapshot.source_binding is SnapshotSourceBinding.DELETED_SOURCE
        ):
            raise SnapshotSourceInstanceDeletedError("source_instance_deleted")
        current = context.status.get(instance_id)
        if (
            current.provision_state.value != "provisioned"
            or current.container_state
            not in {ContainerRuntimeState.RUNNING, ContainerRuntimeState.STOPPED}
        ):
            raise SnapshotRestoreUnsupportedError(
                "Restore requires one exactly running or stopped managed container."
            )
        operation = context.snapshot_restore_operations.queue(instance_id, snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_restore_executor.submit(operation.operation_id)
        return SnapshotRestoreAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/instances/{instance_id}/snapshots/{snapshot_id}/delete",
        response_model=SnapshotDeleteAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_delete(
        instance_id: UUID,
        snapshot_id: UUID,
        _body: SnapshotDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotDeleteAccepted:
        context.status.get(instance_id)
        operation = context.snapshot_delete_operations.queue(instance_id, snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_delete_executor.submit(operation.operation_id)
        return SnapshotDeleteAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/snapshots/preserved/{snapshot_id}/delete",
        response_model=SnapshotDeleteAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_preserved_snapshot_delete(
        snapshot_id: UUID,
        _body: SnapshotDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotDeleteAccepted:
        operation = context.snapshot_delete_operations.queue_preserved(snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_delete_executor.submit(operation.operation_id)
        return SnapshotDeleteAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/instances/{instance_id}/delete",
        response_model=ManagedInstanceDeleteStatus,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_instance_delete(
        instance_id: UUID,
        body: ManagedInstanceDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedInstanceDeleteStatus:
        operation = context.instance_delete_operations.queue(
            instance_id, preserve_snapshots=body.preserve_snapshots
        )
        if operation.state is ManagedInstanceDeleteOperationState.QUEUED:
            context.instance_delete_executor.submit(operation.operation_id)
        return operation

    @app.post(
        "/api/instances/{instance_id}/reset",
        response_model=QueuedResetOperation,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_instance_reset(
        instance_id: UUID,
        _body: ResetConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> QueuedResetOperation:
        existing = context.reset_operations.active_for_instance(instance_id)
        if existing is not None:
            return QueuedResetOperation(
                context.reset_operations.to_status(existing), False
            )
        current = context.reset_status.get(instance_id)
        if current.eligibility.value != "supported":
            raise ResetOperationConflictError(current.eligibility_detail)
        queued = context.reset_operations.queue(instance_id)
        if queued.created:
            context.reset_executor.submit(queued.operation.operation_id)
        return queued

    @app.post(
        "/api/instances/{instance_id}/provision",
        response_model=ManagedRuntimeStatus,
    )
    def provision(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.provision(instance_id)

    @app.post("/api/instances/{instance_id}/start", response_model=ManagedRuntimeStatus)
    def start(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.start(instance_id)

    @app.post("/api/instances/{instance_id}/stop", response_model=ManagedRuntimeStatus)
    def stop(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.stop(instance_id)

    @app.post(
        "/api/instances/{instance_id}/deprovision",
        response_model=ManagedRuntimeStatus,
    )
    def deprovision(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.deprovision(instance_id)

    return app


def _require_same_origin(request: Request) -> None:
    expected = (request.url.scheme.lower(), request.url.netloc.lower())
    origin = request.headers.get("origin")
    if origin is not None:
        parsed = urlsplit(origin)
        if (
            parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme.lower(), parsed.netloc.lower()) != expected
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return
    referer = request.headers.get("referer")
    if referer is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    parsed = urlsplit(referer)
    if (parsed.scheme.lower(), parsed.netloc.lower()) != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _websocket_same_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        return False
    parsed = urlsplit(origin)
    expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
    return (
        parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.scheme.lower() == expected_scheme
        and parsed.netloc.lower() == websocket.url.netloc.lower()
    )


def _selection_bool(value: Literal["false", "true"]) -> bool:
    return value == "true"


async def _stream_addon_zip_multipart(
    request: Request, data_root: Path, artifact_id: UUID
) -> str:
    """Stream exactly one ZIP part into UUID staging; never retain its filename."""

    media_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if (
        media_type != b"multipart/form-data"
        or boundary is None
        or not 1 <= len(boundary) <= 200
    ):
        raise AddonZipValidationError(
            "addon_zip_multipart_invalid",
            "A bounded multipart ZIP upload is required.",
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_COMPRESSED_BYTES + 1024 * 1024:
                raise AddonZipValidationError(
                    "addon_zip_compressed_size_exceeded",
                    "Uploaded ZIP exceeds the 256 MiB compressed limit.",
                )
        except ValueError as error:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid", "Multipart length is invalid."
            ) from error

    fd, _path = create_upload_stage(data_root, artifact_id)
    current_header_name = bytearray()
    current_header_value = bytearray()
    headers: dict[bytes, bytes] = {}
    current_part: str | None = None
    zip_parts = 0
    label_parts = 0
    zip_bytes = 0
    label_bytes = bytearray()
    complete = False

    def part_begin() -> None:
        nonlocal current_part
        current_part = None
        headers.clear()

    def header_begin() -> None:
        current_header_name.clear()
        current_header_value.clear()

    def header_field(data: bytes, start: int, end: int) -> None:
        current_header_name.extend(data[start:end])

    def header_value(data: bytes, start: int, end: int) -> None:
        current_header_value.extend(data[start:end])

    def header_end() -> None:
        name = bytes(current_header_name).lower()
        if name in headers:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid", "Duplicate multipart header."
            )
        headers[name] = bytes(current_header_value)

    def headers_finished() -> None:
        nonlocal current_part, label_parts, zip_parts
        disposition = headers.get(b"content-disposition")
        disposition_type, disposition_options = parse_options_header(disposition)
        if disposition_type != b"form-data":
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid",
                "Multipart content disposition is invalid.",
            )
        raw_name = disposition_options.get(b"name")
        if raw_name == b"zip":
            zip_parts += 1
            current_part = "zip"
        elif raw_name == b"logical_label":
            label_parts += 1
            current_part = "logical_label"
        else:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid",
                "Only one ZIP and one logical label are accepted.",
            )
        if zip_parts > 1 or label_parts > 1:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid", "Multipart fields must be unique."
            )

    def part_data(data: bytes, start: int, end: int) -> None:
        nonlocal zip_bytes
        chunk = data[start:end]
        if current_part == "zip":
            zip_bytes = write_upload_chunk(fd, chunk, zip_bytes)
        elif current_part == "logical_label":
            if len(label_bytes) + len(chunk) > 1024:
                raise AddonZipValidationError(
                    "addon_zip_label_invalid", "Logical label is too long."
                )
            label_bytes.extend(chunk)
        else:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid", "Multipart state is invalid."
            )

    def ended() -> None:
        nonlocal complete
        complete = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": part_begin,
            "on_header_begin": header_begin,
            "on_header_field": header_field,
            "on_header_value": header_value,
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": part_data,
            "on_end": ended,
        },
        max_size=MAX_COMPRESSED_BYTES + 1024 * 1024,
        max_header_count=4,
        max_header_size=1024,
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
        if not complete or zip_parts != 1 or label_parts != 1:
            raise AddonZipValidationError(
                "addon_zip_multipart_invalid",
                "Exactly one ZIP and one logical label are required.",
            )
        try:
            label = label_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise AddonZipValidationError(
                "addon_zip_label_invalid", "Logical label must be UTF-8."
            ) from error
        try:
            normalized = normalize_development_source_logical_name(label)
        except ValueError as error:
            raise AddonZipValidationError(
                "addon_zip_label_invalid", "Logical label is invalid."
            ) from error
        finish_upload_stage(fd, zip_bytes)
        return normalized
    except Exception:
        remove_upload_stage(data_root, artifact_id)
        raise
    finally:
        parser.close()
        os.close(fd)


def _login_failure(
    templates: Jinja2Templates,
    request: Request,
    context: WebApplicationContext,
    *,
    status_code: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Authentication failed.",
            "credentials_configured": context.auth.credentials_configured(),
        },
        status_code=status_code,
    )


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )

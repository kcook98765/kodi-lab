"""Explicit FastAPI application factory for the Phase 4 Web slice."""

# FastAPI intentionally represents dependencies and body metadata as defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    status,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupExecutor,
    AgentWorkspaceCleanupService,
    AgentWorkspaceCleanupStatus,
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
)
from kodi_lab.application.development_capability_amendments import (
    DevelopmentCapabilityAmendmentError,
    DevelopmentCapabilityAmendmentRead,
    DevelopmentCapabilityAmendmentService,
)
from kodi_lab.application.development_source_presentation import (
    DevelopmentSourceDetail,
    DevelopmentSourcePresentationService,
    DevelopmentSourceSummary,
)
from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
    DevelopmentSourceRead,
)
from kodi_lab.application.errors import (
    DevelopmentSourceConflictError,
    DevelopmentSourceDataValidationError,
    DevelopmentSourceNotFoundError,
    TargetNotFoundError,
)
from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalBridgeStatus,
    ProductionHttpAcknowledgementRead,
    ReviewedBridgeAdoptionRead,
    ReviewedBridgeBuildRead,
    ReviewedBridgeUpdatePolicyRead,
)
from kodi_lab.application.external_onboarding import (
    ExternalKodiOnboardingError,
    ExternalKodiOnboardingResult,
    ExternalKodiOnboardingService,
)
from kodi_lab.application.external_reconnect import (
    ExternalReconnectService,
    ReconnectConfirmation,
    ReconnectResult,
    ReconnectStartRequest,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetEventRead,
    ExternalTargetRead,
    ProductionMutationControlRead,
)
from kodi_lab.application.household_onboarding import (
    HouseholdKodiOnboardingService,
    HouseholdOnboardingResult,
    HouseholdResumeRequest,
    HouseholdStartRequest,
)
from kodi_lab.application.local_projects import (
    LocalProjectOnboardingService,
)
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    EXPLICIT_ADDON_SETTINGS_PERMISSIONS,
    EXPLICIT_DEVELOPMENT_LAUNCH_PERMISSIONS,
    EXPLICIT_MANAGED_LIFECYCLE_PERMISSIONS,
    EXPLICIT_MANAGED_RESET_PERMISSIONS,
    EXPLICIT_MANAGED_VERSION_PERMISSIONS,
    EXPLICIT_SOURCE_CAPTURE_PERMISSIONS,
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpClientRead,
    McpCredentialRead,
    McpEligibleWorkspaceRead,
    McpIssuedCredential,
    McpPresetRead,
    McpProductionReadOnlyRead,
    McpProductionReadOnlyRequest,
    McpProductionScopeRead,
    McpTargetPolicyRead,
    McpWorkspaceBindingRead,
)
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
from kodi_lab.application.official_bridge_builds import recognize_official_bridge
from kodi_lab.application.production_capability_amendments import (
    ProductionCapabilityAmendmentError,
    ProductionCapabilityAmendmentRead,
    ProductionCapabilityAmendmentService,
)
from kodi_lab.application.production_inspection import ProductionInspectionError
from kodi_lab.application.production_navigation import (
    NavigationPreferenceRead,
    NavigationPreferenceUpdate,
    ProductionNativeControlAcknowledgementRead,
    ProductionNativeControlAcknowledgementService,
    ProductionNavigationPreferenceService,
)
from kodi_lab.application.production_screenshots import ProductionScreenshotService
from kodi_lab.application.production_transient_approvals import (
    ProductionApprovalError,
    ProductionApprovalRead,
    ProductionTransientApprovalService,
)
from kodi_lab.application.runtime_installations import (
    GENERATIONS as REVIEWED_INSTALL_GENERATIONS,
)
from kodi_lab.application.runtime_installations import (
    PreviewActionApproval,
    RuntimeInstallationActionConflict,
    RuntimeInstallationBusy,
    RuntimeInstallationService,
    RuntimeInstallationUnavailable,
)
from kodi_lab.application.runtime_matrix import (
    RuntimeMatrixResponse,
    RuntimeMatrixService,
)
from kodi_lab.application.screenshot_preferences import (
    ScreenshotPreferenceError,
    ScreenshotPreferenceRead,
    ScreenshotPreferenceService,
    ScreenshotPreferenceUpdate,
)
from kodi_lab.application.workspace_capture_presentation import (
    WorkspaceCapturePresentationService,
    WorkspaceCaptureStatus,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    DeploymentPreflightResult,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureError,
    WorkspaceSourceCaptureResult,
    WorkspaceSourceCaptureService,
)
from kodi_lab.auth import (
    AuthenticatedWebSession,
    LoginThrottler,
    WebAuthenticationService,
)
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.config import Settings
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.domain import (
    MAX_DEVELOPMENT_SOURCE_LOCATOR,
    MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME,
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.bridge import (
    BRIDGE_CAPABILITIES,
    SUPPORTED_V1_BRIDGE_ADDON_VERSION,
    ExternalBridgeClient,
)
from kodi_lab.kodi.external import ExternalEndpointPolicy, NativeExternalKodiVerifier
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.window_resolver import WindowResolver
from kodi_lab.managed.broker import BrokerClient as ManagedBrokerClient
from kodi_lab.mcp.authorization import (
    McpAuthorizationService,
)
from kodi_lab.mcp.lan import McpLanStatus, check_http_proxy, check_lan_endpoint
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.mcp.transport import McpHttpTransport, build_mcp_http_transport
from kodi_lab.repository.development import (
    DevelopmentRepositoryService,
)
from kodi_lab.repository.development import (
    load_current as load_development_current,
)
from kodi_lab.repository.stable import REPOSITORY_ID, REPOSITORY_VERSION, load_current
from kodi_lab.repository.workspace_package import bridge_manifest_for_zip
from kodi_lab.storage.models import (
    ExternalDevelopmentRepositoryReadinessRow,
    ExternalSecretRetirementRow,
    McpWorkspaceDeploymentOperationRow,
    RepositoryWorkspaceDeploymentRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    ExternalCredentialStore,
    SecretStoreError,
    decode_master_key,
)
from kodi_lab.web.admin_views import (
    ADMIN_VIEW_COOKIE,
    admin_view_context,
    admin_view_return_path,
)
from kodi_lab.web.development_targets import development_target_view
from kodi_lab.web.environment_identity import environment_identity
from kodi_lab.web.kodi_views import KodiViews
from kodi_lab.web.managed import managed_router
from kodi_lab.web.presentation import (
    ERROR_MESSAGES,
    STATE_LABELS,
    state_label,
)
from kodi_lab.web.storage_inventory import StorageInventory, byte_size
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


class AgentWorkspaceCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_generation: int = Field(ge=0)


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


class McpClientCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    preset_id: UUID
    trusted_managed_lab: StrictBool = False


class McpPresetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    template_preset_id: UUID
    permissions: set[str] | None = Field(default=None, min_length=1, max_length=12)


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


class ExternalKodiOnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=2, max_length=64)
    code: str = Field(min_length=4, max_length=4)
    kodi_port: int = Field(default=8080, ge=1, le=65_535)
    bridge_port: int = Field(default=8765, ge=1, le=65_535)
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


_ONBOARDING_ERROR_MESSAGES = {
    "invalid_name": "Enter a name for this Kodi.",
    "invalid_address": "Enter a private Kodi IP address.",
    "invalid_port": "Enter a valid Kodi or bridge port.",
    "otp_invalid_format": "Enter the four-character code shown in Kodi.",
    "invalid_kodi_credentials": "Enter both the Kodi username and password.",
    "target_already_exists": (
        "A Kodi with this name already exists. Choose another name."
    ),
    "kodi_unreachable": "Kodi could not be reached at this address.",
    "bridge_unreachable": (
        "Kodi was reached, but the Kodi Lab bridge could not be reached."
    ),
    "otp_invalid": "Code not accepted. Check the code shown in Kodi.",
    "otp_expired": "Code expired. Generate a new code in Kodi.",
    "otp_locked": (
        "Pairing temporarily locked. Generate a new code in Kodi after the cooldown."
    ),
    "bridge_already_paired": "This Kodi Lab bridge is already paired.",
    "bootstrap_version_unsupported": (
        "Update the Kodi Lab bridge before using code pairing."
    ),
    "bridge_identity_mismatch": "The bridge identity changed. Pairing was stopped.",
    "kodi_identity_mismatch": "Kodi identity changed. Pairing was stopped.",
    "pairing_uncertain": "Pairing may have started. Resume pairing to check its state.",
    "pairing_commit_failed": (
        "Pairing could not finish. Resume pairing to check its state."
    ),
    "target_persistence_failed": (
        "Kodi Lab could not save pairing. Resume pairing if offered."
    ),
    "pairing_in_progress": "Another pairing is in progress. Try again shortly.",
}


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


class ExternalTargetRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class ExternalTargetEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class DevelopmentCapabilityAmendmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_capabilities: set[str] = Field(min_length=1, max_length=6)
    requested_capabilities: set[str] = Field(min_length=1, max_length=6)
    expected_bridge_id: UUID
    expected_bridge_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    confirmed: Literal[True]


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


class ProductionCapabilityAmendmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["add", "remove"]
    capability: Literal["screenshot"]
    expected_configured_capabilities: set[
        Literal["target_inspect", "addon_inventory", "semantic_remote", "screenshot"]
    ] = Field(min_length=1, max_length=4)
    expected_policy_revision: int = Field(ge=1)
    confirmed: Literal[True]
    acknowledgement: Literal[
        "I confirm this exact screenshot capability amendment and its displayed "
        "consequences."
    ]


class ProductionApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class ProductionApprovalConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    action: BrowserRemoteAction


class BridgeOtpPairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    bridge_port: int = Field(ge=1, le=65_535)
    code: str = Field(min_length=4, max_length=4)


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
    capabilities: tuple[str, ...] = Field(
        min_length=1, max_length=len(BRIDGE_CAPABILITIES)
    )
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


class ReviewedBridgeUpdatePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]
    enabled: StrictBool
    expected_revision: int = Field(ge=0)


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


class ReviewedBridgeResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class WebApplicationContext:
    settings: Settings
    auth: WebAuthenticationService
    throttler: LoginThrottler
    matrix: RuntimeMatrixService
    development_sources: DevelopmentSourcePresentationService
    development_source_administration: DevelopmentSourceAdministrationService
    agent_workspaces: AgentWorkspaceService
    workspace_source_captures: WorkspaceSourceCaptureService | None
    workspace_capture_presentation: WorkspaceCapturePresentationService | None
    agent_workspace_executor: AgentWorkspaceCreationExecutor
    agent_workspace_cleanups: AgentWorkspaceCleanupService
    agent_workspace_cleanup_executor: AgentWorkspaceCleanupExecutor
    mcp_workspace_retention: McpWorkspaceRetentionService
    mcp_workspace_development: McpWorkspaceDevelopmentService
    local_projects: LocalProjectOnboardingService
    browser_remote_rate_limiter: BrowserRemoteRateLimiter
    external_targets: ExternalTargetAdministrationService
    external_onboarding: ExternalKodiOnboardingService
    household_onboarding: HouseholdKodiOnboardingService
    external_reconnect: ExternalReconnectService
    production_targets: ExternalTargetAdministrationService
    production_capability_amendments: ProductionCapabilityAmendmentService
    development_capability_amendments: DevelopmentCapabilityAmendmentService
    external_bridges: ExternalBridgeService
    screenshot_preferences: ScreenshotPreferenceService
    navigation_preferences: ProductionNavigationPreferenceService
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
    auth: WebAuthenticationService | None = None,
    throttler: LoginThrottler | None = None,
    development_source_administration_service: (
        DevelopmentSourceAdministrationService | None
    ) = None,
    agent_workspace_service: AgentWorkspaceService | None = None,
    agent_workspace_cleanup_service: AgentWorkspaceCleanupService | None = None,
    browser_remote_rate_limiter: BrowserRemoteRateLimiter | None = None,
    external_target_service: ExternalTargetAdministrationService | None = None,
    production_target_service: ExternalTargetAdministrationService | None = None,
    external_bridge_service: ExternalBridgeService | None = None,
    external_target_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    """Build an app entirely from injected storage and typed broker boundaries."""
    repository_data_root = Path(
        os.getenv("KODI_LAB_REPOSITORY_DATA_ROOT", "/srv/kodi-lab-repository")
    )
    if any(
        (repository_data_root / name).exists()
        or (repository_data_root / name).is_symlink()
        for name in ("ORIGIN-MIGRATION.json",)
    ):
        raise ValueError(
            "repository origin migration is incomplete; resume it before Web startup"
        )

    remote_rate_limiter = browser_remote_rate_limiter or BrowserRemoteRateLimiter()
    agent_workspaces = agent_workspace_service or AgentWorkspaceService(
        session_factory,
        data_root=settings.data_root,
        development_root=settings.development_root,
    )
    workspace_source_captures = (
        None
        if settings.development_root is None
        else WorkspaceSourceCaptureService(
            session_factory,
            data_root=settings.data_root,
            development_root=settings.development_root,
        )
    )
    workspace_capture_presentation = (
        None
        if workspace_source_captures is None
        else WorkspaceCapturePresentationService(
            session_factory, workspace_source_captures
        )
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
    local_projects = LocalProjectOnboardingService(
        session_factory,
        development_root=settings.development_root,
        workspaces=agent_workspaces,
        creation=mcp_workspace_creation,
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
    external_onboarding = ExternalKodiOnboardingService(
        session_factory,
        targets=external_targets,
        bridges=external_bridges,
        kodi_endpoint_policy=external_endpoint_policy,
        bridge_endpoint_policy=external_bridge_endpoint_policy,
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
    production_capability_amendments = ProductionCapabilityAmendmentService(
        session_factory, bridge_secrets=external_bridge_secret_store
    )
    household_onboarding = HouseholdKodiOnboardingService(
        session_factory,
        targets=production_targets,
        bridges=external_bridges,
        validation=external_onboarding,
        administration=mcp_administration,
        lock_root=settings.data_root / "locks" / "household-onboarding",
    )
    external_reconnect = ExternalReconnectService(
        session_factory,
        development=external_targets,
        production=production_targets,
        bridges=external_bridges,
        lock_root=settings.data_root / "locks" / "external-reconnect",
    )
    screenshot_preferences = ScreenshotPreferenceService(session_factory)
    navigation_preferences = ProductionNavigationPreferenceService(session_factory)
    development_capability_amendments = DevelopmentCapabilityAmendmentService(
        session_factory,
        endpoint_policy=external_endpoint_policy,
        credential_store=external_credential_store,
        bridges=external_bridges,
        verifier=NativeExternalKodiVerifier(transport=external_target_transport),
    )
    mcp_registry = build_mcp_tool_registry()
    window_resolver = WindowResolver(local_root=settings.data_root / "window-mappings")

    mcp_target_adapters = KodiTargetAdapterService(
        session_factory,
        external_bridges=external_bridges,
        endpoint_policy=external_endpoint_policy,
        credential_store=external_credential_store,
        external_transport=external_target_transport,
        window_resolver=window_resolver,
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
    development_sources = DevelopmentSourcePresentationService(session_factory)
    development_source_validator = DevelopmentSourceValidator(settings.development_root)
    development_source_administration = (
        development_source_administration_service
        or DevelopmentSourceAdministrationService(
            session_factory, development_source_validator
        )
    )
    runtime_installations = RuntimeInstallationService(
        session_factory, broker, data_root=settings.data_root
    )
    lan_endpoints = [
        urlsplit(url)
        for url in (
            settings.mcp_lan_url,
            settings.mcp_lan_http_url if settings.mcp_lan_http_enabled else None,
        )
        if url
    ]
    lan_authorities = [
        (endpoint.scheme, authority)
        for endpoint in lan_endpoints
        for authority in (
            (endpoint.netloc, endpoint.hostname or "")
            if endpoint.port == (443 if endpoint.scheme == "https" else 80)
            else (endpoint.netloc,)
        )
    ]
    managed = ManagedBrokerClient(settings.runtime_root / "managed/control.sock")
    mcp_transport = build_mcp_http_transport(
        managed=managed,
        authentication=McpAuthenticationService(session_factory),
        authorization=mcp_authorization,
        tools=McpTargetToolService(
            session_factory,
            mcp_authorization,
            remote_receipts,
            mcp_workspace_development,
            target_adapters=mcp_target_adapters,
            production_screenshots=production_screenshots,
            production_transient_approvals=production_transient_approvals,
        ),
        additional_allowed_hosts=tuple(
            entry
            for host in (settings.web_host, *settings.web_allowed_hosts)
            for entry in (host, f"{host}:*")
        )
        + tuple(authority for _, authority in lan_authorities),
        additional_allowed_origins=tuple(
            entry
            for host in settings.web_allowed_hosts
            for entry in (f"http://{host}", f"http://{host}:*")
        )
        + tuple(f"{scheme}://{authority}" for scheme, authority in lan_authorities),
    )
    context = WebApplicationContext(
        settings=settings,
        auth=auth or WebAuthenticationService(session_factory),
        throttler=throttler or LoginThrottler(),
        matrix=RuntimeMatrixService(
            session_factory,
            trusted_custom_builds_enabled=(settings.trusted_custom_builds_enabled),
        ),
        development_sources=development_sources,
        development_source_administration=development_source_administration,
        agent_workspaces=agent_workspaces,
        workspace_source_captures=workspace_source_captures,
        workspace_capture_presentation=workspace_capture_presentation,
        agent_workspace_executor=agent_workspace_executor,
        agent_workspace_cleanups=agent_workspace_cleanups,
        agent_workspace_cleanup_executor=agent_workspace_cleanup_executor,
        mcp_workspace_retention=mcp_workspace_retention,
        mcp_workspace_development=mcp_workspace_development,
        local_projects=local_projects,
        browser_remote_rate_limiter=remote_rate_limiter,
        external_targets=external_targets,
        external_onboarding=external_onboarding,
        household_onboarding=household_onboarding,
        external_reconnect=external_reconnect,
        production_targets=production_targets,
        production_capability_amendments=production_capability_amendments,
        development_capability_amendments=development_capability_amendments,
        screenshot_preferences=screenshot_preferences,
        navigation_preferences=navigation_preferences,
        external_bridges=external_bridges,
        production_native_control_acknowledgements=(
            production_native_control_acknowledgements
        ),
        production_transient_approvals=production_transient_approvals,
        mcp_administration=mcp_administration,
        mcp_transport=mcp_transport,
    )

    repository_base_url = os.getenv("KODI_LAB_REPOSITORY_BASE_URL", "")
    repository_data_root = Path(
        os.getenv("KODI_LAB_REPOSITORY_DATA_ROOT", "/srv/kodi-lab-repository")
    )
    storage_inventory = StorageInventory(
        settings.runtime_root, settings.data_root, repository_data_root
    )
    development_repository = (
        DevelopmentRepositoryService(
            session_factory,
            repository_data_root,
            repository_base_url,
            max_artifacts=int(
                os.getenv("KODI_LAB_DEVELOPMENT_REPOSITORY_MAX_ARTIFACTS", "128")
            ),
            max_bytes=int(
                os.getenv("KODI_LAB_DEVELOPMENT_REPOSITORY_MAX_BYTES", "268435456")
            ),
        )
        if repository_base_url and (repository_data_root / "development-data").is_dir()
        else None
    )
    if development_repository is not None:
        mcp_workspace_development.configure_repository(development_repository)
    development_repository_error: str | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal development_repository_error
        bundle_pin = os.getenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", "")
        _app.state.official_bridge_recognition = "unconfigured"
        if repository_base_url and bundle_pin:
            try:
                _app.state.official_bridge_recognition = recognize_official_bridge(
                    session_factory,
                    repository_data_root,
                    base_url=repository_base_url,
                    expected_bundle_sha256=bundle_pin,
                )
            except (OSError, ValueError, KeyError, TypeError):
                _app.state.official_bridge_recognition = "unavailable"
            if _app.state.official_bridge_recognition not in {"registered", "existing"}:
                logging.getLogger(__name__).warning(
                    "Official Bridge recognition: %s; existing trust is unchanged",
                    _app.state.official_bridge_recognition,
                )
        if development_repository is not None:
            try:
                development_repository.reconcile()
                development_repository_error = None
            except (OSError, ValueError, KeyError):
                development_repository_error = (
                    "Development feed requires reconciliation."
                )
                (development_repository.root / "ACK").unlink(missing_ok=True)
        context.mcp_workspace_development.sweep_startup()
        storage_inventory.start()
        context.agent_workspace_executor.start()
        context.agent_workspace_cleanup_executor.start()
        context.mcp_workspace_retention.sweep(force=True)
        async with context.mcp_transport.server.session_manager.run():
            try:
                yield
            finally:
                storage_inventory.close()
                context.agent_workspace_executor.close()
                context.agent_workspace_cleanup_executor.close()

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
        allowed_hosts=[
            settings.web_host,
            "localhost",
            *settings.web_allowed_hosts,
            *(endpoint.hostname or "" for endpoint in lan_endpoints),
        ],
    )
    app.add_middleware(_WorkspaceMutationBodyLimitMiddleware)
    templates = Jinja2Templates(
        directory=_WEB_ROOT / "templates", context_processors=[admin_view_context]
    )
    templates.env.globals["add_kodi_enabled"] = True
    templates.env.globals.update(
        state_labels=STATE_LABELS,
        error_messages=ERROR_MESSAGES,
        state_label=state_label,
        environment_identity=environment_identity,
    )
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "static" / "favicon.ico",
            media_type="image/vnd.microsoft.icon",
        )

    app.router.routes.append(
        Route(
            "/mcp",
            context.mcp_transport.app,
            methods=["POST", "GET", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"],
            name="mcp",
            include_in_schema=False,
        )
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Uvicorn obtains server from getsockname(), independently of Host,
        # Origin and proxy headers. Only the normal socket admits MCP; missing
        # transport metadata also fails closed when LAN Web is enabled.
        server = request.scope.get("server")
        if (
            settings.web_access_mode == "lan-http"
            and request.url.path == "/mcp"
            and (server is None or server[1] != settings.web_port)
        ):
            return JSONResponse(
                {"detail": "mcp_requires_local_listener"}, status_code=403
            )
        if (
            settings.web_requires_https
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path != "/mcp"
            and request.url.scheme != "https"
        ):
            return JSONResponse({"detail": "web_requires_https"}, status_code=403)
        response = await call_next(request)
        context.mcp_workspace_retention.sweep()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        route_name = getattr(request.scope.get("route"), "name", None)
        if route_name == "managed_kodi_page" and response.status_code == 200:
            response.headers["Content-Security-Policy"] = response.headers[
                "Content-Security-Policy"
            ].replace("img-src 'self' blob:", "img-src 'self' blob: data:")
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(HTTPException)
    async def http_failure(request: Request, error: HTTPException) -> Response:
        # Route metadata keeps API/MCP authentication errors as JSON even when
        # their callers accept HTML. The fixed destination cannot redirect offsite.
        if (
            error.status_code == status.HTTP_401_UNAUTHORIZED
            and request.method == "GET"
            and getattr(request.scope.get("route"), "response_class", None)
            is HTMLResponse
            and "text/html" in request.headers.get("accept", "").lower()
        ):
            return RedirectResponse("/login", status_code=303)
        return await http_exception_handler(request, error)

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
            "human_control_active": 409,
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

    @app.exception_handler(WorkspaceSourceCaptureError)
    async def workspace_source_capture_failed(
        _request: Request, error: WorkspaceSourceCaptureError
    ) -> JSONResponse:
        return _json_error(409, error.code, "Source capture could not proceed safely.")

    @app.exception_handler(DevelopmentTreeError)
    async def workspace_source_scan_unsafe(
        _request: Request, _error: DevelopmentTreeError
    ) -> JSONResponse:
        return _json_error(422, "development_source_unsafe", "Source scan was unsafe.")

    @app.exception_handler(AgentWorkspaceCleanupError)
    async def agent_workspace_cleanup_failed(
        _request: Request, error: AgentWorkspaceCleanupError
    ) -> JSONResponse:
        return _json_error(
            404 if error.code.endswith("not_found") else 409,
            error.code,
            error.safe_message,
        )

    @app.exception_handler(RequestValidationError)
    async def bounded_request_validation(
        request: Request, error: RequestValidationError
    ) -> Response:
        path = request.url.path
        closed_schema_mutation = request.method == "POST" and (
            path in {"/api/development-sources", "/api/agent-workspaces"}
            or path.startswith("/api/household-onboarding")
            or path.startswith("/api/external-reconnect")
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
                "managed_version_slot_conflict",
                "managed_version_slot_pending",
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
        status_code = (
            410
            if error.code == "production_visual_scope_retired"
            else 404
            if error.code.endswith("_not_found")
            else 409
        )
        message = {
            "production_visual_scope_retired": (
                "Visual scopes are retired. Use Allow screenshots on the external "
                "Kodi, or revoke its inspection access. No visual scope was changed."
            ),
            "read_only_inspection_permission_required": (
                "The client preset lacks target.inspect."
            ),
            "read_only_preset_incompatible": (
                "The client or target preset is incompatible with production "
                "read-only access."
            ),
            "read_only_state_changed": (
                "Authorization state changed. Refresh and review before retrying."
            ),
            "read_only_client_credential_required": (
                "The client needs an active credential. Read-only authorization "
                "cannot restore credentials."
            ),
            "read_only_scope_review_required": (
                "Existing inspection scopes need administrator review."
            ),
            "read_only_target_policy_review_required": (
                "The existing target policy needs administrator review."
            ),
            "read_only_grant_review_required": (
                "The removed target grant needs administrator review."
            ),
            "read_only_existing_control_authority": (
                "This relationship already has control authority and cannot be "
                "designated read-only."
            ),
        }.get(error.code, "MCP administration rejected.")
        return _json_error(status_code, error.code, message)

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
            "connection_retirement_incomplete": 409,
            "connection_secret_retirement_failed": 503,
        }.get(error.code, 409)
        message = {
            "connection_secret_retirement_failed": (
                "The connection is retired, but protected Kodi credential cleanup "
                "failed. Open Connection history and retry credential cleanup."
            ),
            "connection_retirement_incomplete": (
                "The connection is retired, but onboarding cleanup is incomplete. "
                "Open Connection history and retry credential cleanup after any "
                "in-flight connection request finishes."
            ),
        }.get(error.code, "External target request rejected.")
        return _json_error(status_code, error.code, message)

    @app.exception_handler(DevelopmentCapabilityAmendmentError)
    async def development_capability_amendment_failure(
        _request: Request, error: DevelopmentCapabilityAmendmentError
    ) -> JSONResponse:
        status_code = (
            404
            if error.code == "target_not_found"
            else 422
            if error.code in {"capabilities_invalid", "administrator_invalid"}
            else 409
        )
        return _json_error(
            status_code, error.code, "Development capability amendment rejected."
        )

    @app.exception_handler(ExternalKodiOnboardingError)
    async def external_onboarding_failure(
        _request: Request, error: ExternalKodiOnboardingError
    ) -> JSONResponse:
        status_code = {
            "invalid_name": 422,
            "invalid_address": 422,
            "invalid_port": 422,
            "otp_invalid_format": 422,
            "invalid_kodi_credentials": 422,
            "target_already_exists": 409,
            "kodi_unreachable": 503,
            "bridge_unreachable": 503,
            "otp_invalid": 422,
            "otp_expired": 410,
            "otp_locked": 429,
            "bridge_already_paired": 409,
            "bootstrap_version_unsupported": 409,
            "bridge_identity_mismatch": 409,
            "pairing_uncertain": 409,
            "pairing_commit_failed": 409,
            "target_persistence_failed": 503,
        }.get(error.code, 409)
        detail: dict[str, str] = {
            "code": error.code,
            "message": _ONBOARDING_ERROR_MESSAGES.get(
                error.code, "Pairing could not be completed."
            ),
        }
        if error.target_id is not None:
            detail["target_id"] = str(error.target_id)
        return JSONResponse(status_code=status_code, content={"error": detail})

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
            "otp_invalid_format": 422,
            "wrong_code": 422,
            "pairing_code_expired": 410,
            "pairing_temporarily_locked": 429,
            "generation_cooldown": 429,
            "pairing_code_unavailable": 409,
            "bootstrap_outcome_uncertain": 409,
            "provisional_expired": 410,
            "transport_acknowledgement_required": 409,
            "bridge_secret_retirement_failed": 503,
        }.get(error.code, 409)
        message = {
            "bridge_secret_retirement_failed": (
                "Bridge authority is revoked, but credential cleanup needs a retry. "
                "For a retired connection, open Connection history and choose "
                "Retry credential cleanup."
            ),
            "otp_invalid_format": _ONBOARDING_ERROR_MESSAGES["otp_invalid_format"],
            "wrong_code": _ONBOARDING_ERROR_MESSAGES["otp_invalid"],
            "pairing_code_expired": _ONBOARDING_ERROR_MESSAGES["otp_expired"],
            "pairing_temporarily_locked": _ONBOARDING_ERROR_MESSAGES["otp_locked"],
            "generation_cooldown": (
                "Wait for the bridge code cooldown, then generate a new code in Kodi."
            ),
            "pairing_code_unavailable": "Generate a new four-character code in Kodi.",
            "bootstrap_outcome_uncertain": _ONBOARDING_ERROR_MESSAGES[
                "pairing_uncertain"
            ],
            "provisional_expired": _ONBOARDING_ERROR_MESSAGES["pairing_commit_failed"],
            "transport_acknowledgement_required": (
                "Review and acknowledge the current HTTP connection risks for "
                "this Kodi and Bridge port, then enter a current pairing code. "
                "Revoking a pairing invalidates its previous acknowledgement. "
                "The code was not sent."
            ),
            "reviewed_update_policy_changed": (
                "Reload this page before changing reviewed-update trust."
            ),
            "reviewed_update_state_changed": (
                "State changed during verification. Check current status "
                "before retrying."
            ),
            "reviewed_update_continuity_changed": (
                "Device, pairing or connection changed. Turn reviewed-update "
                "trust Off and use Reverify bridge for manual review."
            ),
            "reviewed_update_not_current_release": (
                "This is not the exact current reviewed release. Turn "
                "reviewed-update trust Off to review the installed build "
                "manually."
            ),
            "reviewed_update_not_successor": (
                "Automatic trust does not admit downgrades. Turn reviewed- "
                "update trust Off and review the rollback manually."
            ),
            "reviewed_update_capability_expansion": (
                "This update adds capabilities requiring manual review. Turn "
                "reviewed-update trust Off and use Reverify bridge."
            ),
            "bridge_build_not_reviewed": (
                "Install the compatible official Bridge from the Kodi Lab "
                "repository, or review this custom build in Advanced settings."
            ),
        }.get(error.code, "External bridge request rejected.")
        return _json_error(status_code, error.code, message)

    @app.exception_handler(ScreenshotPreferenceError)
    async def screenshot_preference_failure(
        _request: Request, error: ScreenshotPreferenceError
    ) -> JSONResponse:
        return _json_error(
            404 if error.code == "target_not_found" else 409,
            error.code,
            "Screenshot preference request rejected.",
        )

    @app.exception_handler(ProductionCapabilityAmendmentError)
    async def production_capability_amendment_failure(
        _request: Request, error: ProductionCapabilityAmendmentError
    ) -> JSONResponse:
        status_code = 404 if error.code == "target_not_found" else 409
        if error.code in {
            "capability_amendment_unsupported",
            "configured_capabilities_invalid",
            "capability_confirmation_required",
        }:
            status_code = 422
        if error.code == "bridge_secret_retirement_failed":
            status_code = 503
        return _json_error(
            status_code, error.code, "Production capability amendment rejected."
        )

    @app.exception_handler(ProductionInspectionError)
    async def production_control_acknowledgement_failure(
        _request: Request, error: ProductionInspectionError
    ) -> JSONResponse:
        status_code = (
            422 if error.code == "transport_acknowledgement_not_required" else 409
        )
        if error.code == "target_not_found":
            status_code = 404
        return _json_error(
            status_code,
            error.code,
            "Production navigation or control request rejected.",
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
            return RedirectResponse("/kodi", status_code=303)
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
        response = RedirectResponse("/kodi", status_code=303)
        response.set_cookie(
            settings.web_session_cookie_name,
            created.token,
            max_age=settings.web_session_absolute_seconds,
            httponly=True,
            secure=settings.web_requires_https,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/admin-view")
    async def change_admin_view(request: Request) -> Response:
        _require_same_origin(request)
        authenticated = current_session(request)
        form = await request.form()
        csrf_value = form.get("csrf_token")
        csrf_token = csrf_value if isinstance(csrf_value, str) else None
        if not context.auth.csrf_matches(authenticated, csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        mode = form.get("mode")
        if mode not in ("standard", "development"):
            raise HTTPException(status_code=400, detail="invalid_admin_view")
        destination = form.get("return_to")
        response = RedirectResponse(
            admin_view_return_path(
                destination if isinstance(destination, str) else "/"
            ),
            status_code=303,
        )
        response.delete_cookie(
            ADMIN_VIEW_COOKIE,
            httponly=True,
            secure=settings.web_requires_https,
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
            secure=settings.web_requires_https,
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
        return templates.TemplateResponse(
            request,
            "index.html",
            {"csrf_token": authenticated.csrf_token},
        )

    kodi_views = KodiViews(
        session_factory,
        context.production_targets,
        context.external_bridges,
        screenshot_preferences,
    )

    app.include_router(
        managed_router(
            managed,
            templates,
            current_session,
            mutation_session,
            resolve_session=context.auth.resolve,
            cookie_name=settings.web_session_cookie_name,
            same_origin=_websocket_same_origin,
            require_https=settings.web_requires_https,
        )
    )

    @app.get("/storage-maintenance", response_class=HTMLResponse)
    def storage_maintenance_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "storage-maintenance.html",
            {
                "csrf_token": authenticated.csrf_token,
                "inventory": storage_inventory.snapshot(),
                "byte_size": byte_size,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/storage-maintenance")
    def storage_maintenance_status(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        return JSONResponse(
            storage_inventory.snapshot(), headers={"Cache-Control": "no-store"}
        )

    @app.get("/kodi", response_class=HTMLResponse)
    def kodi_page(request: Request) -> Response:
        from kodi_lab import __version__
        from kodi_lab.web.overview import overview_summary

        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "kodi.html",
            {
                "csrf_token": authenticated.csrf_token,
                "overview": overview_summary(session_factory, kodi_views),
                "storage_warning": storage_inventory.snapshot()["warning"],
                "application_version": __version__,
            },
        )

    @app.get("/kodi/{target_id}", response_class=HTMLResponse)
    async def kodi_detail_page(
        request: Request,
        target_id: UUID,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        try:
            kodi = kodi_views.get(target_id)
        except ExternalTargetError as error:
            raise HTTPException(status_code=404) from error
        return templates.TemplateResponse(
            request,
            "kodi-detail.html",
            {"csrf_token": authenticated.csrf_token, "kodi": kodi},
        )

    @app.get("/development", response_class=HTMLResponse)
    @app.get("/settings", response_class=HTMLResponse)
    @app.get("/external-kodi", response_class=HTMLResponse)
    async def navigation_landing_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            f"{request.url.path.lstrip('/')}.html",
            {"csrf_token": authenticated.csrf_token},
        )

    @app.get("/mcp-clients", response_class=HTMLResponse)
    async def mcp_clients_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        mcp_lan = await check_lan_endpoint(
            settings.mcp_lan_url,
            instance_id=context.mcp_transport.instance_id,
            ca_file=settings.mcp_lan_ca_file,
        )
        mcp_lan_http = (
            await check_http_proxy(
                settings.mcp_lan_http_url,
                instance_id=context.mcp_transport.instance_id,
                bind_address=settings.mcp_lan_http_bind_address,
                readiness_socket=settings.mcp_lan_http_readiness_socket,
            )
            if settings.mcp_lan_http_enabled
            else McpLanStatus(
                None,
                False,
                "Trusted LAN HTTP is disabled. An administrator must explicitly "
                "enable it for a chosen private interface and trusted subnet.",
            )
        )
        agent_stdio_configuration = {
            "command": "kodi-lab-mcp-stdio",
            "args": [],
            "env": {
                "KODI_LAB_MCP_TOKEN": "PASTE_NEW_TOKEN_HERE",
                "KODI_LAB_MCP_URL": f"http://127.0.0.1:{settings.web_port}/mcp",
            },
        }
        agent_configuration = (
            {
                "type": "http",
                "url": mcp_lan.url,
                "headers": {"Authorization": "Bearer PASTE_NEW_TOKEN_HERE"},
            }
            if mcp_lan.ready
            else agent_stdio_configuration
        )
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
                "mcp_lan": mcp_lan,
                "mcp_lan_http": mcp_lan_http,
                "agent_configuration": agent_configuration,
                "agent_stdio_configuration": agent_stdio_configuration,
                "agent_stdio_port": settings.web_port,
                "clients": context.mcp_administration.list_clients(),
                "presets": context.mcp_administration.list_presets(),
                "custom_preset_permissions": sorted(
                    {
                        permission
                        for preset in context.mcp_administration.list_presets()
                        if preset.builtin
                        and preset.assignable
                        and preset.display_name
                        not in {
                            "Production Inspection",
                            "Production Visual Inspection",
                            "Production Remote Kodi",
                        }
                        for permission in preset.permissions
                    }
                    | EXPLICIT_SOURCE_CAPTURE_PERMISSIONS
                    | EXPLICIT_MANAGED_LIFECYCLE_PERMISSIONS
                    | EXPLICIT_MANAGED_VERSION_PERMISSIONS
                    | EXPLICIT_MANAGED_RESET_PERMISSIONS
                    | EXPLICIT_ADDON_SETTINGS_PERMISSIONS
                    | EXPLICIT_DEVELOPMENT_LAUNCH_PERMISSIONS
                ),
                "capture_permission_template_ids": {
                    ADDON_DEVELOPMENT_PRESET_ID,
                    FULL_LAB_CONTROL_PRESET_ID,
                },
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
        include_retired: bool = False,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        targets = context.external_targets.list_targets(include_retired=include_retired)
        amendments = context.development_capability_amendments
        bridge_statuses = {
            target.target_id: context.external_bridges.status(target.target_id)
            for target in targets
        }
        with session_factory() as session:
            cleanup_pending = set(
                session.scalars(select(ExternalSecretRetirementRow.target_id))
            )
        development_views = {}
        for target in targets:
            reconnect = context.external_reconnect.active_operation(target.target_id)
            development_views[target.target_id] = development_target_view(
                target,
                bridge_statuses[target.target_id],
                reconnect_stage=None if reconnect is None else reconnect.stage,
                cleanup_pending=str(target.target_id) in cleanup_pending,
            )
        return templates.TemplateResponse(
            request,
            "external-targets.html",
            {
                "csrf_token": authenticated.csrf_token,
                "targets": targets,
                "include_retired": include_retired,
                "bridge_statuses": bridge_statuses,
                "development_views": development_views,
                "capability_amendments": {
                    target.target_id: amendments.list_receipts(target.target_id)
                    for target in targets
                },
                "allowed_ports": settings.external_target_allowed_ports,
                "bridge_allowed_ports": settings.external_bridge_allowed_ports,
                "supported_bridge_version": SUPPORTED_V1_BRIDGE_ADDON_VERSION,
                "test_loopback_enabled": (
                    settings.external_target_test_loopback_enabled
                ),
            },
        )

    @app.get("/add-kodi", response_class=HTMLResponse)
    async def add_kodi_page(
        request: Request,
        mode: Literal["home", "development"] = "home",
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        # Same configured origin and bootstrap identity as repository delivery.
        # Rendering instructions never probes Kodi or the repository service.
        base_url = os.getenv("KODI_LAB_REPOSITORY_BASE_URL", "")
        repository_source_url = None
        try:
            origin = urlsplit(base_url)
            if (
                origin.scheme in {"http", "https"}
                and origin.hostname
                and origin.hostname not in {"localhost", "127.0.0.1", "::1"}
                and origin.username is None
                and origin.password is None
                and not origin.path
                and not origin.query
                and not origin.fragment
                and (origin.port is None or 1 <= origin.port <= 65535)
                and all(33 <= ord(char) <= 126 for char in base_url)
            ):
                repository_source_url = f"{base_url}/install/"
        except ValueError:
            pass
        repository_zip_name = f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip"
        data_root = Path(
            os.getenv("KODI_LAB_REPOSITORY_DATA_ROOT", "/srv/kodi-lab-repository")
        )
        if (data_root / "CURRENT").exists():
            try:
                _, feed = load_current(
                    data_root,
                    expected_base_url=base_url,
                    expected_bundle_sha256=os.getenv(
                        "KODI_LAB_REPOSITORY_BUNDLE_SHA256"
                    )
                    or None,
                )
                repository_zip_name = (
                    f"{REPOSITORY_ID}-{feed['repository_version']}.zip"
                )
            except (OSError, ValueError, KeyError):
                repository_source_url = None
        return templates.TemplateResponse(
            request,
            "add-kodi.html",
            {
                "initial_mode": mode,
                "repository_source_url": repository_source_url,
                "repository_zip_name": repository_zip_name,
                "required_home_bridge_version": SUPPORTED_V1_BRIDGE_ADDON_VERSION,
                "csrf_token": authenticated.csrf_token,
                "recovery_namespace": hashlib.sha256(
                    authenticated.csrf_token.encode()
                ).hexdigest(),
            },
        )

    @app.get("/reconnect-kodi/{target_id}", response_class=HTMLResponse)
    async def reconnect_kodi_page(
        request: Request,
        target_id: UUID,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        target = context.external_reconnect.target(target_id)
        bridge = context.external_bridges.status(target_id)
        active = context.external_reconnect.active_operation(target_id)
        return templates.TemplateResponse(
            request,
            "reconnect-kodi.html",
            {
                "target": target,
                "bridge_port": active.bridge_port
                if active is not None
                else bridge.bridge_port or 8765,
                "active_operation_id": None if active is None else active.id,
                "csrf_token": authenticated.csrf_token,
                "recovery_namespace": hashlib.sha256(
                    authenticated.csrf_token.encode()
                ).hexdigest(),
            },
        )

    @app.get("/pair-kodi", response_class=HTMLResponse)
    async def pair_kodi_page(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "pair-kodi.html",
            {"csrf_token": authenticated.csrf_token},
        )

    @app.get("/production-targets", response_class=HTMLResponse)
    async def production_targets_page(
        request: Request,
        include_retired: bool = False,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> Response:
        targets = context.production_targets.list_targets(
            include_retired=include_retired
        )
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
                "include_retired": include_retired,
                "bridge_statuses": bridge_statuses,
                "production_views": {
                    target.target_id: kodi_views.present(target) for target in targets
                },
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
                "supported_bridge_version": SUPPORTED_V1_BRIDGE_ADDON_VERSION,
            },
        )

    @app.get("/runtimes", response_class=HTMLResponse)
    async def runtime_matrix_page(
        request: Request,
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
        history_before: UUID | None = None,
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        matrix = context.matrix.list_matrix(
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )
        history = runtime_installations.records(
            include_preview=True,
            before=history_before,
            limit=257,
        )
        return templates.TemplateResponse(
            request,
            "runtimes.html",
            {
                "matrix": matrix,
                "installation_records": history[:256],
                "history_before": history_before,
                "history_next": history[255].operation_id
                if len(history) > 256
                else None,
                "installations": runtime_installations.list(include_preview=True),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/api/runtime-installations")
    def list_runtime_installations(
        request: Request,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> dict[str, object]:
        return {
            "installations": [
                asdict(item)
                for item in runtime_installations.list(include_preview=True)
            ]
        }

    @app.get("/api/runtime-installations/{operation_id}")
    def get_runtime_installation(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> dict[str, object]:
        try:
            item = runtime_installations.get(operation_id)
        except RuntimeInstallationUnavailable as error:
            raise HTTPException(
                status_code=503, detail="installation_unavailable"
            ) from error
        if item is None:
            raise HTTPException(status_code=404, detail="installation_not_found")
        return asdict(item)

    @app.post("/api/runtime-installations/{generation}")
    async def start_runtime_installation(
        request: Request,
        generation: str,
        action: Literal["install", "retry", "reacquire"] = "install",
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> JSONResponse:
        if generation not in (*REVIEWED_INSTALL_GENERATIONS, "piers"):
            raise HTTPException(status_code=404, detail="generation_not_supported")
        if generation == "piers":
            from pydantic import ValidationError

            try:
                approval = PreviewActionApproval.model_validate(await request.json())
            except (ValueError, ValidationError):
                raise HTTPException(
                    status_code=422, detail="exact_preview_acknowledgement_required"
                ) from None
        elif await request.body():
            raise HTTPException(status_code=400, detail="request_body_must_be_empty")
        try:
            if generation == "piers":
                result = runtime_installations.start_preview(
                    approval=approval,
                    action=action,
                )
                item = runtime_installations.get(UUID(result.operation_id))
                if item is None:
                    raise RuntimeInstallationUnavailable
            else:
                item = runtime_installations.start(generation, action=action)
        except RuntimeInstallationBusy as error:
            raise HTTPException(status_code=409, detail="installation_busy") from error
        except RuntimeInstallationActionConflict as error:
            raise HTTPException(
                status_code=409, detail="installation_action_conflict"
            ) from error
        except RuntimeInstallationUnavailable as error:
            raise HTTPException(
                status_code=503, detail="installation_unavailable"
            ) from error
        return JSONResponse(asdict(item), status_code=202)

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
                "development_root_available": DevelopmentSourceValidator(
                    context.settings.development_root
                ).root_available(),
            },
        )

    @app.get("/development-sources/{source_id}", response_class=HTMLResponse)
    async def development_source_detail_page(
        request: Request, source_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "development-source-detail.html",
            {
                "detail": context.development_sources.get_source(source_id),
                "csrf_token": authenticated.csrf_token,
            },
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
        capture_status = (
            None
            if context.workspace_capture_presentation is None
            else context.workspace_capture_presentation.status(workspace_id)
        )
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
        with session_factory() as session:
            repository_deployments = list(
                session.query(RepositoryWorkspaceDeploymentRow)
                .filter(
                    RepositoryWorkspaceDeploymentRow.workspace_id == str(workspace_id)
                )
                .order_by(RepositoryWorkspaceDeploymentRow.created_at.desc())
                .limit(20)
            )
        return templates.TemplateResponse(
            request,
            "agent-workspace-detail.html",
            {
                "workspace": workspace,
                "capture_status": capture_status,
                "entries": entries,
                "changes": changes,
                "activity": activity,
                "repository_deployments": repository_deployments,
                "lifecycle_activity": context.agent_workspaces.list_lifecycle_activity(
                    workspace_id
                ),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/kodi-repository", response_class=HTMLResponse)
    async def kodi_repository_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        base_url = os.getenv("KODI_LAB_REPOSITORY_BASE_URL", "")
        data_path = Path(
            os.getenv("KODI_LAB_REPOSITORY_DATA_ROOT", "/srv/kodi-lab-repository")
        )
        provenance: dict[str, Any] | None = None
        origin_reachability = "not_checked"
        healthy = False
        generated_base_url: str | None = None
        try:
            ipaddress.ip_address(urlsplit(base_url).hostname or "")
            literal_repository_address = True
        except ValueError:
            literal_repository_address = False
        if base_url and data_path.is_dir():
            try:
                _, provenance = load_current(
                    data_path,
                    expected_base_url=base_url,
                    expected_bundle_sha256=os.getenv(
                        "KODI_LAB_REPOSITORY_BUNDLE_SHA256"
                    )
                    or None,
                )
                generated_base_url = str(provenance["base_url"])
                async with httpx.AsyncClient(timeout=2) as client:
                    result = await client.get("http://repository:8110/health")
                    healthy = result.status_code == 200 and result.content == b"ok\n"
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                if provenance is None:
                    # Diagnose only: serving/admission still requires the exact
                    # configured origin above. Verify every package byte and
                    # candidate pin before displaying the stored origin.
                    try:
                        _, observed = load_current(
                            data_path,
                            expected_bundle_sha256=os.getenv(
                                "KODI_LAB_REPOSITORY_BUNDLE_SHA256"
                            )
                            or None,
                        )
                        generated_base_url = str(observed["base_url"])
                    except (OSError, ValueError, KeyError):
                        pass
        dev_provenance: dict[str, object] | None = None
        publications: list[Any] = []
        storage_usage = (0, 0)
        if development_repository is not None and development_repository_error is None:
            try:
                _, dev_provenance = load_development_current(
                    development_repository.root, expected_base_url=base_url
                )
                publications = development_repository.list_publications()
                storage_usage = development_repository.storage_usage()
            except (OSError, ValueError, KeyError):
                pass
        repository_deployments: list[dict[str, Any]] = []
        repository_targets: list[dict[str, Any]] = []
        existing_candidates: list[dict[str, str]] = []
        if development_repository is not None and dev_provenance is not None:
            for publication in publications:
                if (
                    publication.status != "current"
                    or publication.source_kind != "workspace"
                ):
                    continue
                artifact = (
                    development_repository.root
                    / "packages"
                    / publication.addon_id
                    / f"{publication.addon_id}-{publication.version}.zip"
                )
                try:
                    digest, _, _ = bridge_manifest_for_zip(
                        artifact.read_bytes(), publication.addon_id
                    )
                except (OSError, ValueError):
                    continue
                existing_candidates.append(
                    {
                        "publication_id": publication.id,
                        "addon_id": publication.addon_id,
                        "version": publication.version,
                        "zip_sha256": publication.zip_sha256,
                        "expected_manifest_digest": digest,
                    }
                )
        publication_by_id = {item.id: item for item in publications}
        current_by_addon = {
            item.addon_id: item.version
            for item in publications
            if item.status == "current"
        }
        with session_factory() as session:
            for linked in (
                session.query(RepositoryWorkspaceDeploymentRow)
                .order_by(RepositoryWorkspaceDeploymentRow.created_at.desc())
                .limit(50)
            ):
                operation = session.get(
                    McpWorkspaceDeploymentOperationRow, linked.operation_id
                )
                target = session.get(TargetRow, linked.target_id)
                if operation is None:
                    continue
                repository_deployments.append(
                    {
                        "operation_id": linked.operation_id,
                        "publication_id": linked.publication_id,
                        "workspace_id": linked.workspace_id,
                        "generation": linked.workspace_generation,
                        "target_id": linked.target_id,
                        "target_name": "" if target is None else target.name,
                        "target_classification": (
                            "unknown" if target is None else target.target_type.value
                        ),
                        "target_kodi_version": linked.target_kodi_version,
                        "state": linked.state,
                        "origin": linked.origin,
                        "verified_by": linked.verified_by,
                        "addon_id": linked.addon_id,
                        "addon_version": linked.addon_version,
                        "zip_sha256": linked.zip_sha256,
                        "expected_manifest_digest": linked.expected_manifest_digest,
                        "installed_before_version": linked.installed_before_version,
                        "current_repository_version": current_by_addon.get(
                            linked.addon_id
                        ),
                        "compatible_generations": (
                            "unknown"
                            if linked.publication_id not in publication_by_id
                            else publication_by_id[
                                linked.publication_id
                            ].compatible_generations
                        ),
                        "failure_code": linked.failure_code,
                    }
                )
            for target in (
                session.query(TargetRow)
                .filter(TargetRow.target_type == TargetType.EXTERNAL_DEVELOPMENT)
                .order_by(TargetRow.name)
            ):
                ready = session.get(
                    ExternalDevelopmentRepositoryReadinessRow, target.id
                )
                repository_targets.append(
                    {
                        "id": target.id,
                        "name": target.name,
                        "ready": ready is not None
                        and ready.base_url == base_url
                        and dev_provenance is not None
                        and ready.repository_version
                        == dev_provenance["repository_version"]
                        and ready.bootstrap_sha256
                        == dev_provenance["bootstrap_zip_sha256"],
                        "confirmed_at": None if ready is None else ready.confirmed_at,
                    }
                )
        if provenance is not None and generated_base_url == base_url:
            from kodi_lab.repository.address import probe_advertised_origin

            origin_reachability = await probe_advertised_origin(
                base_url, str(provenance["addons_xml_sha256"])
            )
        return templates.TemplateResponse(
            request,
            "kodi-repository.html",
            {
                "base_url": base_url,
                "origin_reachability": origin_reachability,
                "healthy": healthy,
                "provenance": provenance,
                "generated_base_url": generated_base_url,
                "repository_origin_mismatch": generated_base_url is not None
                and generated_base_url != base_url,
                "literal_repository_address": literal_repository_address,
                "development_provenance": dev_provenance,
                "development_error": development_repository_error,
                "publications": publications,
                "repository_deployments": repository_deployments,
                "repository_targets": repository_targets,
                "existing_candidates": existing_candidates,
                "storage_usage": storage_usage,
                "development_max_artifacts": development_repository.max_artifacts
                if development_repository
                else 0,
                "development_max_bytes": development_repository.max_bytes
                if development_repository
                else 0,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.post("/api/kodi-repository/development/import")
    async def import_development_publication(
        request: Request,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, str]:
        if development_repository is None or development_repository_error is not None:
            raise HTTPException(
                status_code=503, detail="Development repository is unavailable"
            )
        if request.headers.get("content-type") != "application/zip":
            raise HTTPException(status_code=415, detail="A ZIP body is required")
        compatibility = tuple(
            item.strip()
            for item in request.headers.get("x-kodi-lab-compatibility", "").split(",")
        )
        parts: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > 32 * 1024 * 1024:
                raise HTTPException(
                    status_code=413, detail="Development ZIP exceeds 32 MiB"
                )
            parts.append(chunk)
        try:
            record = development_repository.prepare_import(
                b"".join(parts), compatibility
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "publication_id": record.id,
            "addon_id": record.addon_id,
            "version": record.version,
            "addon_type": record.addon_type,
            "source_kind": record.source_kind,
            "source_id": record.source_id,
            "compatible_generations": record.compatible_generations,
            "scope_kind": record.scope_kind,
            "zip_sha256": record.zip_sha256,
            "tree_sha256": record.tree_sha256,
            "status": record.status,
        }

    @app.post("/api/kodi-repository/development/{publication_id}/approve")
    async def approve_development_publication(
        request: Request,
        publication_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, str]:
        if development_repository is None or development_repository_error is not None:
            raise HTTPException(
                status_code=503, detail="Development repository is unavailable"
            )
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) != {
            "addon_id",
            "version",
            "zip_sha256",
        }:
            raise HTTPException(
                status_code=400, detail="Exact publication confirmation is required"
            )
        record = development_repository.get_publication(publication_id)
        if (
            record is None
            or record.addon_id != payload["addon_id"]
            or record.version != payload["version"]
            or record.source_kind != "web_import"
        ):
            raise HTTPException(status_code=409, detail="Publication identity changed")
        try:
            approved = development_repository.approve(
                publication_id, observed_sha256=payload["zip_sha256"]
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "publication_id": approved.id,
            "status": approved.status,
            "feed_generation": approved.feed_generation or "",
        }

    @app.post("/api/external-targets/{target_id}/development-repository/confirm")
    async def confirm_target_development_repository(
        request: Request,
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, str]:
        payload = await request.json()
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"base_url", "reachable_in_kodi", "manual_updates_confirmed"}
            or not isinstance(payload["base_url"], str)
            or payload["reachable_in_kodi"] is not True
            or payload["manual_updates_confirmed"] is not True
        ):
            raise HTTPException(
                status_code=400, detail="Exact repository confirmation is required"
            )
        try:
            readiness = (
                context.mcp_workspace_development.confirm_development_repository(
                    target_id=target_id,
                    base_url=payload["base_url"],
                    reachable_in_kodi=True,
                    manual_updates_confirmed=True,
                )
            )
        except McpWorkspaceDevelopmentError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return {
            "target_id": readiness.target_id,
            "base_url": readiness.base_url,
            "bootstrap_sha256": readiness.bootstrap_sha256,
            "confirmed_at": readiness.confirmed_at,
        }

    @app.post(
        "/api/external-targets/{target_id}/development-repository/"
        "publications/{publication_id}/verify-existing"
    )
    async def verify_existing_repository_installation(
        request: Request,
        target_id: UUID,
        publication_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, Any]:
        # Preserve duplicate keys until validation; request.json() discards them.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(status_code=400, detail="Confirmation is too large")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate confirmation key")
                value[key] = item
            return value

        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError) as error:
            raise HTTPException(
                status_code=400, detail="Exact confirmation JSON is required"
            ) from error
        required = {
            "target_id",
            "publication_id",
            "addon_id",
            "addon_version",
            "zip_sha256",
            "expected_manifest_digest",
            "confirmed_existing",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload["target_id"] != str(target_id)
            or payload["publication_id"] != str(publication_id)
            or payload["confirmed_existing"] is not True
            or any(
                not isinstance(payload[key], str)
                for key in required - {"confirmed_existing"}
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Exact existing-installation confirmation is required",
            )
        try:
            service = context.mcp_workspace_development
            result = service.verify_existing_repository_installation(
                target_id=target_id,
                publication_id=publication_id,
                addon_id=payload["addon_id"],
                addon_version=payload["addon_version"],
                zip_sha256=payload["zip_sha256"],
                expected_manifest_digest=payload["expected_manifest_digest"],
            )
        except McpWorkspaceDevelopmentError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return result.model_dump(mode="json")

    @app.post(
        "/api/workspaces/{workspace_id}/repository-deployments/{operation_id}/approve"
    )
    async def approve_workspace_repository_deployment(
        request: Request,
        workspace_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, Any]:
        payload = await request.json()
        required = {
            "target_id",
            "generation",
            "addon_id",
            "addon_version",
            "zip_sha256",
            "expected_manifest_digest",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise HTTPException(
                status_code=400, detail="Exact deployment approval is required"
            )
        if type(payload["generation"]) is not int or any(
            not isinstance(payload[key], str) for key in required - {"generation"}
        ):
            raise HTTPException(
                status_code=400, detail="Exact deployment approval types are required"
            )
        try:
            result = context.mcp_workspace_development.approve_repository_deployment(
                operation_id,
                workspace_id=workspace_id,
                target_id=UUID(payload["target_id"]),
                generation=int(payload["generation"]),
                addon_id=str(payload["addon_id"]),
                addon_version=str(payload["addon_version"]),
                zip_sha256=str(payload["zip_sha256"]),
                expected_manifest_digest=str(payload["expected_manifest_digest"]),
            )
        except (ValueError, McpWorkspaceDevelopmentError) as error:
            raise HTTPException(
                status_code=409, detail=getattr(error, "code", str(error))
            ) from error
        return result.model_dump(mode="json")

    @app.post(
        "/api/workspaces/{workspace_id}/repository-deployments/{operation_id}/recheck"
    )
    async def recheck_workspace_repository_deployment(
        workspace_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> dict[str, Any]:
        with session_factory() as session:
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(operation_id)
            )
        if operation is None or operation.workspace_id != str(workspace_id):
            raise HTTPException(status_code=404, detail="deployment_not_found")
        try:
            result = context.mcp_workspace_development.recheck_repository_deployment(
                operation_id
            )
        except McpWorkspaceDevelopmentError as error:
            raise HTTPException(status_code=409, detail=error.code) from error
        return result.model_dump(mode="json")

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
            permissions=(
                None if payload.permissions is None else frozenset(payload.permissions)
            ),
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
            display_name=payload.display_name,
            preset_id=payload.preset_id,
            trusted_managed_lab=payload.trusted_managed_lab,
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

    @app.post(
        "/api/mcp/clients/{client_id}/managed-lab-role", response_model=McpClientRead
    )
    def set_mcp_client_managed_lab_role(
        client_id: UUID,
        payload: McpClientEnabledRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpClientRead:
        return context.mcp_administration.set_managed_lab_role(
            client_id, payload.enabled
        )

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

    @app.post(
        "/api/mcp/clients/{client_id}/targets/{target_id}/read-only-access",
        response_model=McpProductionReadOnlyRead,
    )
    def authorize_mcp_production_read_only(
        client_id: UUID,
        target_id: UUID,
        payload: McpProductionReadOnlyRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> McpProductionReadOnlyRead:
        return context.mcp_administration.authorize_production_read_only(
            client_id, target_id, payload, administrator_reference="web_admin"
        )

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

    @app.get(
        "/api/targets/{target_id}/screenshot-preference",
        response_model=ScreenshotPreferenceRead,
    )
    def read_screenshot_preference(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ScreenshotPreferenceRead:
        return context.screenshot_preferences.read(target_id)

    @app.put(
        "/api/targets/{target_id}/screenshot-preference",
        response_model=ScreenshotPreferenceRead,
    )
    def update_screenshot_preference(
        target_id: UUID,
        payload: ScreenshotPreferenceUpdate,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ScreenshotPreferenceRead:
        return context.screenshot_preferences.update(
            target_id,
            enabled=payload.enabled,
            expected_revision=payload.expected_revision,
            administrator_reference="web_admin",
        )

    @app.get(
        "/api/production-targets/{target_id}/navigation-preference",
        response_model=NavigationPreferenceRead,
    )
    def read_navigation_preference(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> NavigationPreferenceRead:
        return context.navigation_preferences.read(target_id)

    @app.put(
        "/api/production-targets/{target_id}/navigation-preference",
        response_model=NavigationPreferenceRead,
    )
    def update_navigation_preference(
        target_id: UUID,
        payload: NavigationPreferenceUpdate,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> NavigationPreferenceRead:
        return context.navigation_preferences.update(
            target_id,
            enabled=payload.enabled,
            expected_revision=payload.expected_revision,
        )

    @app.get("/api/external-targets", response_model=list[ExternalTargetRead])
    def list_external_targets(
        include_retired: bool = False,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetRead]:
        return context.external_targets.list_targets(include_retired=include_retired)

    @app.post("/api/external-reconnect/{target_id}", response_model=ReconnectResult)
    def start_external_reconnect(
        target_id: UUID,
        payload: ReconnectStartRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReconnectResult:
        return context.external_reconnect.start(target_id, payload)

    @app.post(
        "/api/external-reconnect/{target_id}/{operation_id}/resume",
        response_model=ReconnectResult,
    )
    def resume_external_reconnect(
        target_id: UUID,
        operation_id: UUID,
        payload: HouseholdResumeRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReconnectResult:
        return context.external_reconnect.resume(target_id, operation_id, payload)

    @app.post(
        "/api/external-reconnect/{target_id}/{operation_id}/replace-pairing",
        response_model=ReconnectResult,
    )
    def replace_external_reconnect_pairing(
        target_id: UUID,
        operation_id: UUID,
        _payload: ReconnectConfirmation,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReconnectResult:
        return context.external_reconnect.replace_pairing(target_id, operation_id)

    @app.post(
        "/api/external-reconnect/{target_id}/{operation_id}/cancel",
        response_model=ReconnectResult,
    )
    def cancel_external_reconnect(
        target_id: UUID,
        operation_id: UUID,
        _payload: ReconnectConfirmation,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReconnectResult:
        return context.external_reconnect.cancel(target_id, operation_id)

    @app.post("/api/household-onboarding", response_model=HouseholdOnboardingResult)
    def start_household_onboarding(
        payload: HouseholdStartRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> HouseholdOnboardingResult:
        return context.household_onboarding.start(payload)

    @app.post(
        "/api/household-onboarding/{operation_id}/resume",
        response_model=HouseholdOnboardingResult,
    )
    def resume_household_onboarding(
        operation_id: UUID,
        payload: HouseholdResumeRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> HouseholdOnboardingResult:
        return context.household_onboarding.resume(operation_id, payload)

    @app.post(
        "/api/household-onboarding/{operation_id}/abandon",
        response_model=HouseholdOnboardingResult,
    )
    def abandon_household_onboarding(
        operation_id: UUID,
        _payload: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> HouseholdOnboardingResult:
        return context.household_onboarding.abandon(operation_id)

    @app.post(
        "/api/external-targets/onboard-otp",
        response_model=ExternalKodiOnboardingResult,
        status_code=status.HTTP_201_CREATED,
    )
    def onboard_external_kodi(
        payload: ExternalKodiOnboardingRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalKodiOnboardingResult:
        return context.external_onboarding.onboard(**payload.model_dump())

    @app.post(
        "/api/external-targets/onboarding/{target_id}/resume",
        response_model=ExternalKodiOnboardingResult,
    )
    def resume_external_kodi_onboarding(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalKodiOnboardingResult:
        return context.external_onboarding.resume(target_id)

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

    @app.get(
        "/api/external-targets/{target_id}/capability-amendments",
        response_model=list[DevelopmentCapabilityAmendmentRead],
    )
    def list_development_capability_amendments(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> tuple[DevelopmentCapabilityAmendmentRead, ...]:
        return context.development_capability_amendments.list_receipts(target_id)

    @app.post(
        "/api/external-targets/{target_id}/capability-amendments",
        response_model=DevelopmentCapabilityAmendmentRead,
    )
    def amend_development_capabilities(
        target_id: UUID,
        payload: DevelopmentCapabilityAmendmentRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> DevelopmentCapabilityAmendmentRead:
        return context.development_capability_amendments.amend(
            target_id,
            expected_capabilities=frozenset(payload.expected_capabilities),
            requested_capabilities=frozenset(payload.requested_capabilities),
            expected_bridge_id=payload.expected_bridge_id,
            expected_bridge_fingerprint=payload.expected_bridge_fingerprint,
        )

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

    def retire_external_connection(
        service: ExternalTargetAdministrationService, target_id: UUID
    ) -> ExternalTargetRead:
        # Validate the exact registration class before retiring any authority.
        service.get(target_id)
        native_error = None
        try:
            result = service.deactivate(target_id)
        except ExternalTargetError as error:
            native_error = error
        except SecretStoreError:
            native_error = ExternalTargetError("connection_secret_retirement_failed")
        # Deactivation closes new pairing publication before the final revoke.
        # Even failed native secret deletion must retire Bridge authority.
        try:
            context.external_bridges.revoke(target_id)
        except ExternalBridgeServiceError as error:
            if error.code != "bridge_not_paired":
                raise
        if native_error is not None:
            raise native_error
        if service is context.production_targets:
            context.household_onboarding.finish_retired_connection(target_id)
        return result

    @app.post(
        "/api/external-targets/{target_id}/retire",
        response_model=ExternalTargetRead,
    )
    @app.post(
        "/api/external-targets/{target_id}/deactivate",
        response_model=ExternalTargetRead,
    )
    def deactivate_external_target(
        target_id: UUID,
        _payload: ExternalTargetRetirementRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return retire_external_connection(context.external_targets, target_id)

    @app.get("/api/production-targets", response_model=list[ExternalTargetRead])
    def list_production_targets(
        include_retired: bool = False,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ExternalTargetRead]:
        return context.production_targets.list_targets(include_retired=include_retired)

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

    @app.post(
        "/api/production-targets/{target_id}/capability-amendments",
        response_model=ProductionCapabilityAmendmentRead,
        status_code=status.HTTP_201_CREATED,
    )
    def amend_production_screenshot_capability(
        target_id: UUID,
        payload: ProductionCapabilityAmendmentRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ProductionCapabilityAmendmentRead:
        amendments = context.production_capability_amendments
        return amendments.amend_production_capability_maximum(
            target_id,
            operation=payload.operation,
            capability=payload.capability,
            expected_configured_capabilities=frozenset(
                payload.expected_configured_capabilities
            ),
            expected_policy_revision=payload.expected_policy_revision,
        )

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
        "/api/production-targets/{target_id}/retire",
        response_model=ExternalTargetRead,
    )
    @app.post(
        "/api/production-targets/{target_id}/deactivate",
        response_model=ExternalTargetRead,
    )
    def deactivate_production_target(
        target_id: UUID,
        _payload: ExternalTargetRetirementRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalTargetRead:
        return retire_external_connection(context.production_targets, target_id)

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
        "/api/production-targets/{target_id}/bridge/pair",
        response_model=ExternalBridgeStatus,
    )
    def pair_production_bridge(
        target_id: UUID,
        payload: BridgeOtpPairRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.begin_otp_pairing(
            target_id, bridge_port=payload.bridge_port, code=payload.code
        )

    @app.post(
        "/api/production-targets/{target_id}/bridge/confirm",
        response_model=ExternalBridgeStatus,
    )
    def confirm_production_bridge(
        target_id: UUID,
        _payload: ProductionBridgeConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.commit_otp_pairing(target_id)

    @app.get("/api/production-targets/{target_id}/bridge/adoptions/{receipt_id}")
    def get_reviewed_bridge_adoption(
        target_id: UUID,
        receipt_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ReviewedBridgeAdoptionRead:
        return context.external_bridges.reviewed_adoption(target_id, receipt_id)

    @app.get("/api/production-targets/{target_id}/bridge/reviewed-updates")
    def get_reviewed_bridge_update_policy(
        target_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ReviewedBridgeUpdatePolicyRead:
        return context.external_bridges.reviewed_update_policy(target_id)

    @app.post("/api/production-targets/{target_id}/bridge/reviewed-updates")
    def set_reviewed_bridge_update_policy(
        target_id: UUID,
        payload: ReviewedBridgeUpdatePolicyRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ReviewedBridgeUpdatePolicyRead:
        return context.external_bridges.set_reviewed_update_policy(
            target_id,
            enabled=payload.enabled,
            expected_revision=payload.expected_revision,
        )

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
        "/api/external-targets/{target_id}/bridge/pair",
        response_model=ExternalBridgeStatus,
    )
    def pair_external_bridge(
        target_id: UUID,
        payload: BridgeOtpPairRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.begin_otp_pairing(
            target_id, bridge_port=payload.bridge_port, code=payload.code
        )

    @app.post(
        "/api/external-targets/{target_id}/bridge/confirm",
        response_model=ExternalBridgeStatus,
    )
    def confirm_external_bridge(
        target_id: UUID,
        _payload: ExternalBridgeConfirmedRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ExternalBridgeStatus:
        return context.external_bridges.commit_otp_pairing(target_id)

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

    @app.get(
        "/api/agent-workspaces/{workspace_id}/deployment-preflight",
        response_model=DeploymentPreflightResult,
    )
    def workspace_deployment_preflight(
        workspace_id: UUID,
        target_id: UUID,
        expected_generation: int = Query(ge=0),
        deployment_mode: Literal["auto", "direct", "repository"] = "auto",
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> DeploymentPreflightResult:
        # This projection deliberately cannot reach runtime status/convergence.
        context.agent_workspaces.get(workspace_id)
        return WorkspaceDeploymentPreflightService(session_factory).inspect(
            DeploymentPreflightInput(
                workspace_id=workspace_id,
                target_id=target_id,
                expected_generation=expected_generation,
                deployment_mode=deployment_mode,
            )
        )

    @app.get(
        "/api/agent-workspaces/{workspace_id}/capture-status",
        response_model=WorkspaceCaptureStatus,
    )
    def get_workspace_capture_status(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> WorkspaceCaptureStatus:
        if context.workspace_capture_presentation is None:
            raise HTTPException(
                status_code=503, detail="Development source unavailable"
            )
        return context.workspace_capture_presentation.status(workspace_id)

    @app.get(
        "/api/agent-workspaces/{workspace_id}/source-scan",
        response_model=WorkspaceCaptureStatus,
    )
    def scan_workspace_source(
        workspace_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> WorkspaceCaptureStatus:
        if (
            context.workspace_source_captures is None
            or context.workspace_capture_presentation is None
        ):
            raise HTTPException(
                status_code=503, detail="Development source unavailable"
            )
        observed = context.workspace_source_captures.scan_source(workspace_id)
        return context.workspace_capture_presentation.status(
            workspace_id, scan=observed
        )

    @app.post(
        "/api/agent-workspaces/{workspace_id}/source-capture",
        response_model=WorkspaceSourceCaptureResult,
    )
    def capture_workspace_source(
        workspace_id: UUID,
        body: AgentWorkspaceCaptureRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> WorkspaceSourceCaptureResult:
        if context.workspace_source_captures is None:
            raise HTTPException(
                status_code=503, detail="Development source unavailable"
            )
        return context.workspace_source_captures.capture(
            workspace_id, expected_generation=body.expected_generation
        )

    @app.post(
        "/api/agent-workspace-capture-operations/{operation_id}/recover",
        response_model=WorkspaceSourceCaptureResult | None,
    )
    def recover_workspace_source_capture(
        operation_id: UUID,
        _body: EmptyActionRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> WorkspaceSourceCaptureResult | None:
        if context.workspace_source_captures is None:
            raise HTTPException(
                status_code=503, detail="Development source unavailable"
            )
        return context.workspace_source_captures.recover(operation_id)

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

    @app.get(
        "/api/development-sources/{source_id}",
        response_model=DevelopmentSourceDetail,
    )
    def get_development_source(
        source_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> DevelopmentSourceDetail:
        return context.development_sources.get_source(source_id)

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

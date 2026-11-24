"""Thin MCP tool adapters over existing Kodi Lab application services."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, overload
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    AddonInventoryUnavailableReason,
    KodiAddonProvenanceClassification,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.mcp_administration import McpPrincipal
from kodi_lab.application.mcp_promotion_requests import (
    McpPromotionRequestError,
    McpPromotionRequestService,
    PromotionRequestResult,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.mcp_target_adapters import (
    KodiTargetAdapterError,
    KodiTargetAdapterService,
)
from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentError,
    McpWorkspaceDevelopmentService,
    WorkspaceDeploymentResult,
    WorkspaceExecutionKind,
    WorkspaceExecutionResult,
)
from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_visual_state,
)
from kodi_lab.application.production_navigation import (
    ProductionActionClass,
    ProductionNavigationAuthority,
    classify_production_action,
)
from kodi_lab.application.production_screenshots import (
    ProductionScreenshotError,
    ProductionScreenshotService,
)
from kodi_lab.application.production_transient_approvals import (
    ProductionApprovalError,
    ProductionTransientApprovalService,
    ProductionTransientResult,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    DeploymentPreflightResult,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureError,
)
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.development.launch import ManifestLaunchEntry
from kodi_lab.development.sources import (
    CopiedSmokeUnsupportedReason,
    DevelopmentSourceValidationError,
)
from kodi_lab.domain import (
    TargetType,
)
from kodi_lab.kodi.logs import (
    KodiLogRequest,
    KodiLogResult,
    KodiLogScanBridgeRequest,
    KodiLogScanRequest,
    KodiLogScanResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE, PNG_SIGNATURE
from kodi_lab.kodi.system_info import SystemInfo
from kodi_lab.kodi.ui_state import UiObservation
from kodi_lab.kodi.ui_state_native import MANAGED_BRIDGE_RECOVERY_REASONS
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION
from kodi_lab.storage.models import (
    TargetRow,
)
from kodi_lab.workspace import AgentWorkspaceStorageError

MAX_MCP_RESPONSE_BYTES = 64 * 1024
MAX_MCP_DEVELOPMENT_RESPONSE_BYTES = 48 * 1024
MAX_MCP_DEVELOPMENT_TEXT_BYTES = 44 * 1024
MAX_MCP_DEVELOPMENT_EDIT_TEXT_BYTES = 48 * 1024
MAX_MCP_DEVELOPMENT_EDIT_ARGUMENT_BYTES = 60 * 1024

_SOURCE_CAPTURE_REASONS = frozenset(
    {
        "workspace_generation_conflict",
        "workspace_private_edits_present",
        "registered_source_unavailable",
        "workspace_not_clean_and_ready",
        "capture_reconciliation_required",
    }
)


def _source_capture_reason(error: Exception) -> str | None:
    if isinstance(error, WorkspaceSourceCaptureError):
        if error.code == "source_addon_identity_changed":
            return "source_identity_changed"
        if error.code in {
            "workspace_capture_evidence_changed",
            "workspace_bytes_changed",
        }:
            return "capture_reconciliation_required"
        return error.code if error.code in _SOURCE_CAPTURE_REASONS else None
    if isinstance(error, AgentWorkspaceStorageError):
        return {
            "agent_workspace_capture_identity_changed": "source_identity_changed",
            "agent_workspace_capture_private_edits": "workspace_private_edits_present",
            "agent_workspace_capture_reconciliation_required": (
                "capture_reconciliation_required"
            ),
            "agent_workspace_capture_addon_invalid": "source_invalid",
        }.get(error.code)
    if isinstance(error, DevelopmentTreeError):
        if error.code in {
            "development_source_unavailable",
            "development_source_unreadable",
        }:
            return "registered_source_unavailable"
        if error.code in {
            "development_source_unsafe_name",
            "development_source_tree_bounds",
            "development_source_root_invalid",
            "development_source_unsafe_entry",
            "development_source_type_conflict",
        }:
            return "source_invalid"
        return None
    if isinstance(error, DevelopmentSourceValidationError):
        if error.code in {
            "root_not_configured",
            "root_unavailable",
            "source_unavailable",
        }:
            return "registered_source_unavailable"
        if error.code in {
            "root_invalid",
            "locator_invalid",
            "addon_xml_missing",
            "addon_xml_unsafe",
            "addon_xml_too_large",
            "addon_xml_malformed",
            "addon_metadata_invalid",
        }:
            return "source_invalid"
        return None
    return None


class AddonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    addon_id: str = Field(max_length=128)
    name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    addon_type: str = Field(max_length=128)
    enabled: StrictBool
    provenance: str | None = Field(default=None, max_length=64)
    provenance_status: str | None = Field(default=None, max_length=32)


class AddonListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    target_id: UUID
    kodi_version: str = Field(max_length=32)
    complete: StrictBool = True
    total_count: int = Field(ge=0, le=2_048)
    addons: tuple[AddonSummary, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=128)


class ScreenshotCaptureMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    target_id: UUID
    capture_id: UUID
    captured_at: datetime
    width: int = Field(ge=1, le=4096)
    height: int = Field(ge=1, le=2160)
    mime_type: str = Field(max_length=16)
    byte_size: int = Field(ge=1, le=MAX_PNG_BYTES)


class ScreenshotRetrievalMetadata(ScreenshotCaptureMetadata):
    request_id: UUID
    age_seconds: int = Field(ge=0, le=120)
    runtime_state_revision: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class ScreenshotToolResult:
    metadata: ScreenshotCaptureMetadata
    png: bytes


class TargetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    display_name: str = Field(max_length=256)
    kodi_version: str = Field(max_length=32)
    target_class: str = "internal_lab"
    runtime_profile: str
    provision_state: str
    runtime_state: str
    process_state: str
    jsonrpc_state: str
    usability_state: str
    readiness_state: str
    capabilities: tuple[str, ...] = ("target_status",)


class TargetListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    targets: tuple[TargetSummary, ...]
    next_cursor: str | None = None


class TargetStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    target: TargetSummary


class DevelopmentSourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    logical_name: str = Field(max_length=120)
    source_type: str = Field(max_length=32)
    addon_id: str = Field(max_length=128)
    addon_name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    extension_types: tuple[str, ...] = Field(max_length=64)
    registration_observed_at: datetime
    availability: str = "registration_observed"


class DevelopmentSourceListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    sources: tuple[DevelopmentSourceSummary, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=36)


class WorkspaceSourceScanToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    source_tree_digest: str = Field(max_length=71)
    scanned_at: datetime
    active_capture_version: str = Field(max_length=128)
    active_capture_digest: str | None = Field(default=None, max_length=71)
    workspace_generation: int = Field(ge=0)
    workspace_tree_digest: str | None = Field(default=None, max_length=71)
    source_matches_capture: bool
    private_workspace_edits: bool


class WorkspaceCaptureToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    expected_generation: int = Field(
        ge=0,
        description=(
            "Current workspace generation observed by the caller, used as an "
            "optimistic-concurrency precondition. Use expected_generation=1 to "
            "capture changed source into generation 2. Do not supply the desired "
            "next generation."
        ),
    )


class WorkspaceCaptureToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    operation_id: UUID | None
    generation: int = Field(ge=0)
    tree_digest: str = Field(max_length=71)
    changed: bool


class WorkspaceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    source_id: UUID
    lifecycle_state: str
    bytes_state: str
    generation: int = Field(ge=0)
    selected_session_generation: int | None = Field(default=None, gt=0)
    creation_state: str
    created_at: datetime
    updated_at: datetime | None
    latest_promotion_state: str | None = None
    latest_verification_state: str | None = None
    retention_classification: str
    retention_deadline_at: datetime | None = None
    automatic_cleanup_blocked: StrictBool
    latest_validation_id: UUID | None = None
    latest_validation_state: str | None = None
    latest_validation_generation: int | None = Field(default=None, ge=0)
    latest_deployment_id: UUID | None = None
    latest_deployment_state: str | None = None
    latest_deployment_mode: str | None = Field(default=None, max_length=16)
    latest_deployment_phase: str | None = Field(default=None, max_length=32)
    latest_deployment_publication_id: UUID | None = None
    latest_deployment_installation_kind: str | None = Field(default=None, max_length=16)
    latest_deployment_next_action: str | None = Field(default=None, max_length=160)
    latest_deployment_generation: int | None = Field(default=None, ge=0)
    latest_deployment_target_id: UUID | None = None
    latest_execution_id: UUID | None = None
    latest_execution_state: str | None = None
    latest_execution_generation: int | None = Field(default=None, ge=0)
    latest_execution_target_id: UUID | None = None
    latest_launch_id: UUID | None = None
    latest_launch_request_id: UUID | None = None
    latest_launch_state: str | None = None
    latest_launch_generation: int | None = None
    latest_launch_target_id: UUID | None = None
    latest_launch_extension_ordinal: int | None = None
    latest_promotion_plan_id: UUID | None = None
    latest_promotion_review_state: str | None = None
    latest_promotion_operation_id: UUID | None = None
    latest_promotion_operation_state: str | None = None
    latest_promotion_verification_id: UUID | None = None
    latest_promotion_verification_state: str | None = None
    latest_promotion_final_classification: str | None = None


class WorkspaceStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspaces: tuple[WorkspaceSummary, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=36)


class WorkspaceTreeEntrySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(max_length=512)
    entry_type: str = Field(pattern="^(file|directory)$")
    byte_size: int | None = Field(default=None, ge=0)


class WorkspaceTreeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    directory: str = Field(max_length=512)
    observed_generation: int = Field(ge=0)
    entries: tuple[WorkspaceTreeEntrySummary, ...] = Field(max_length=200)
    next_cursor: str | None = Field(default=None, max_length=512)


class WorkspaceReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    path: str = Field(max_length=512)
    text: str
    observed_generation: int = Field(ge=0)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    start_line: int = Field(ge=1)
    returned_line_count: int = Field(ge=0, le=2000)
    next_line: int | None = Field(default=None, ge=1)
    total_file_bytes: int = Field(ge=0, le=2 * 1024 * 1024)
    truncated: StrictBool


class WorkspaceEditInput(BaseModel):
    """Generation-bound complete-file edits; aggregate UTF-8 text at most 48 KiB."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"enum": ["create", "replace"]}}
                    },
                    "then": {
                        "required": ["content"],
                        "properties": {"content": {"type": "string"}},
                        "not": {
                            "anyOf": [
                                {"required": ["expected_text"]},
                                {"required": ["replacement_text"]},
                            ]
                        },
                    },
                },
                {
                    "if": {"properties": {"operation": {"const": "exact_patch"}}},
                    "then": {
                        "required": ["expected_text", "replacement_text"],
                        "properties": {
                            "expected_text": {"type": "string"},
                            "replacement_text": {"type": "string"},
                        },
                        "not": {"required": ["content"]},
                    },
                },
            ]
        },
    )

    workspace_id: UUID = Field(description="UUID of the owned ready workspace.")
    expected_generation: int = Field(
        ge=0,
        description="Currently observed workspace generation before this mutation.",
    )
    operation: Literal["create", "replace", "exact_patch"] = Field(
        description="Edit mode; each mode permits only its specified text fields."
    )
    relative_path: str = Field(
        min_length=1,
        max_length=512,
        description=(
            "Confined relative file path in the workspace. Create also "
            "creates missing parents in agent-owned projects atomically with the file."
        ),
    )
    content: str | None = Field(
        default=None,
        description=(
            "Required for create or replace only: complete initial or resulting "
            "UTF-8 file content. Omit for exact_patch."
        ),
    )
    expected_text: str | None = Field(
        default=None,
        description=(
            "Required for exact_patch only: complete exact current file content, "
            "including line endings. Omit otherwise."
        ),
    )
    replacement_text: str | None = Field(
        default=None,
        description=(
            "Required for exact_patch only: complete desired resulting file "
            "content. Omit otherwise."
        ),
    )

    @model_validator(mode="after")
    def validate_operation_payload(self) -> WorkspaceEditInput:
        supplied = self.model_fields_set
        text_values: tuple[str, ...]
        if self.operation in {"create", "replace"}:
            if (
                "content" not in supplied
                or self.content is None
                or "expected_text" in supplied
                or "replacement_text" in supplied
            ):
                raise ValueError("create and replace require only content")
            text_values = (self.content,)
        else:
            if (
                "content" in supplied
                or "expected_text" not in supplied
                or "replacement_text" not in supplied
                or self.expected_text is None
                or self.replacement_text is None
            ):
                raise ValueError(
                    "exact_patch requires only expected_text and replacement_text"
                )
            text_values = (self.expected_text, self.replacement_text)
        try:
            aggregate = sum(
                len(value.encode("utf-8", "strict")) for value in text_values
            )
        except UnicodeEncodeError as error:
            raise ValueError("edit text must be valid UTF-8") from error
        if aggregate > MAX_MCP_DEVELOPMENT_EDIT_TEXT_BYTES:
            raise ValueError("aggregate edit text exceeds 48 KiB")
        encoded_arguments = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded_arguments) > MAX_MCP_DEVELOPMENT_EDIT_ARGUMENT_BYTES:
            raise ValueError("encoded edit arguments exceed the MCP bound")
        return self


class WorkspaceEditResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    relative_path: str = Field(max_length=512)
    operation: Literal["create", "replace", "exact_patch"]
    prior_generation: int = Field(ge=0)
    new_generation: int = Field(ge=1)
    result_classification: Literal["succeeded"] = "succeeded"
    file_byte_size: int = Field(ge=0, le=2 * 1024 * 1024)


class WorkspaceValidateInput(BaseModel):
    """Closed structural validation has no caller-selected execution input."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    expected_generation: int = Field(
        ge=0,
        description=(
            "Current, existing workspace generation to validate. "
            "Validation does not create a generation."
        ),
    )


class PackageInstallationStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    verify: bool = False


class WorkspaceTargetOperationInput(BaseModel):
    """The exact closed input shared by deployment and fixed execution."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    workspace_id: UUID
    expected_generation: int = Field(ge=0)
    target_id: UUID


class WorkspaceLaunchInput(WorkspaceTargetOperationInput):
    extension_ordinal: int = Field(ge=0, le=63, strict=True)
    expected_generation: int = Field(ge=0, strict=True)


class WorkspaceExecuteInput(WorkspaceTargetOperationInput):
    """Select only one of the server-owned execution intents."""

    expected_generation: int = Field(
        ge=0,
        description=(
            "Current, existing workspace generation of the exact completed "
            "deployment to execute. Execution does not create a generation."
        ),
    )
    execution_kind: WorkspaceExecutionKind = WorkspaceExecutionKind.SMOKE


class WorkspaceExecutionRecoverInput(BaseModel):
    """Select an existing ambiguous operation; all runtime details are derived."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    execution_operation_id: UUID


class WorkspaceDeployInput(WorkspaceTargetOperationInput):
    """Existing deployment tool with an explicit transport selection."""

    expected_generation: int = Field(
        ge=0,
        description=(
            "Current, existing workspace generation with matching successful "
            "validation to deploy. Deployment does not create a generation."
        ),
    )
    deployment_mode: Literal["auto", "direct", "repository"] = Field(
        default="auto",
        description=(
            "auto selects repository only for an external_development target with "
            "repository readiness; otherwise it selects direct, subject to target "
            "eligibility checks. direct is the managed deployment path; repository "
            "uses the external-development human approval and Kodi update path."
        ),
    )


class WorkspaceValidatedPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(max_length=512)
    change_kind: Literal["added", "modified"]


class WorkspaceValidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = KODI_LAB_MCP_SCHEMA_VERSION
    workspace_id: UUID
    source_id: UUID
    observed_generation: int = Field(ge=0)
    validated_at: datetime
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    extension_types: tuple[str, ...] = Field(max_length=64)
    launch_entries: tuple[ManifestLaunchEntry, ...] = Field(default=(), max_length=64)
    fixed_smoke_eligible: bool | None = Field(
        default=None,
        description=(
            "Manifest-only eligibility for the existing bounded video smoke. "
            "Deployment and target authority must still pass; this does not "
            "authorize direct script launch."
        ),
    )
    fixed_smoke_unsupported_reason: CopiedSmokeUnsupportedReason | None = None
    identity_classification: Literal["stable", "changed"]
    addon_id_classification: Literal["unchanged", "changed"]
    version_classification: Literal["unchanged", "changed"]
    extension_type_classification: Literal["unchanged", "changed"]
    changed_file_count: int = Field(ge=0, le=50_000)
    added_file_count: int = Field(ge=0, le=50_000)
    modified_file_count: int = Field(ge=0, le=50_000)
    deletion_classification: Literal["none"] = "none"
    binary_change_classification: Literal["none"] = "none"
    type_transition_classification: Literal["none"] = "none"
    promotable: StrictBool
    promotable_classification: Literal[
        "promotable", "addon_identity_changed", "agent_owned_project"
    ]
    changed_paths: tuple[WorkspaceValidatedPath, ...] = Field(max_length=64)
    truncated: StrictBool


class ManagedSelectionPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_id: UUID = Field(strict=False)
    workspace_id: UUID = Field(strict=False)


class ManagedSelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    generation: int
    target_id: UUID
    workspace_id: UUID
    source_id: UUID
    phase: str
    selected: bool


class ManagedEnsureReadyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: UUID = Field(strict=False)
    target_id: UUID = Field(strict=False)
    hold_minutes: int = Field(default=0, ge=0, le=240)


class ManagedReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: UUID = Field(strict=False)
    target_id: UUID = Field(strict=False)


class McpTargetToolService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        authorization: McpAuthorizationService,
        remote_actions: McpRemoteActionReceiptService | None = None,
        workspace_development: McpWorkspaceDevelopmentService | None = None,
        promotion_requests: McpPromotionRequestService | None = None,
        target_adapters: KodiTargetAdapterService | None = None,
        production_screenshots: ProductionScreenshotService | None = None,
        production_transient_approvals: (
            ProductionTransientApprovalService | None
        ) = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._remote_actions = remote_actions
        self._workspace_development = workspace_development
        self._promotion_requests = promotion_requests
        self._target_adapters = target_adapters
        self._production_screenshots = production_screenshots
        self._production_transient_approvals = production_transient_approvals

    def promotion_request(
        self, principal: McpPrincipal, operation: WorkspaceTargetOperationInput
    ) -> PromotionRequestResult:
        self._authorization.authorize_workspace_target(
            principal,
            "promotion_request",
            operation.workspace_id,
            operation.target_id,
        )
        if self._promotion_requests is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            result = self._promotion_requests.request(
                client_id=principal.client_id,
                request_id=operation.request_id,
                workspace_id=operation.workspace_id,
                expected_generation=operation.expected_generation,
                target_id=operation.target_id,
            )
        except McpPromotionRequestError as error:
            raise McpAuthorizationError(error.code) from error
        self._require_development_bounded(result)
        return result

    def workspace_deploy_preflight(
        self, principal: McpPrincipal, operation: DeploymentPreflightInput
    ) -> DeploymentPreflightResult:
        def authorize() -> object:
            return self._authorization.authorize_workspace_target_scope(
                principal,
                "workspace_deploy_preflight",
                operation.workspace_id,
                operation.target_id,
            )

        authorize()
        result = WorkspaceDeploymentPreflightService(self._session_factory).inspect(
            operation, client_id=principal.client_id
        )
        authorize()
        self._require_development_bounded(result)
        return result

    def workspace_deploy(
        self, principal: McpPrincipal, operation: WorkspaceDeployInput
    ) -> WorkspaceDeploymentResult:
        self._authorization.authorize_workspace_target(
            principal,
            "workspace_deploy",
            operation.workspace_id,
            operation.target_id,
        )
        if self._workspace_development is None:
            raise McpAuthorizationError("capability_unavailable")

        def recheck_authority(session: Session) -> None:
            try:
                self._authorization.recheck_workspace_launch(
                    session,
                    principal,
                    operation.workspace_id,
                    operation.target_id,
                    tool_name="workspace_deploy",
                )
            except McpAuthorizationError as error:
                raise McpWorkspaceDevelopmentError(
                    error.code, reason=error.reason
                ) from error

        try:
            result = self._workspace_development.deploy(
                client_id=principal.client_id,
                request_id=operation.request_id,
                workspace_id=operation.workspace_id,
                expected_generation=operation.expected_generation,
                target_id=operation.target_id,
                deployment_mode=operation.deployment_mode,
                recheck_authority=recheck_authority,
            )
        except McpWorkspaceDevelopmentError as error:
            raise McpAuthorizationError(error.code, reason=error.reason) from error
        self._require_development_bounded(result)
        return result

    def workspace_execute(
        self, principal: McpPrincipal, operation: WorkspaceExecuteInput
    ) -> WorkspaceExecutionResult:
        self._authorization.authorize_workspace_target(
            principal,
            "workspace_execute",
            operation.workspace_id,
            operation.target_id,
        )
        if self._workspace_development is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            result = self._workspace_development.execute(
                client_id=principal.client_id,
                request_id=operation.request_id,
                workspace_id=operation.workspace_id,
                expected_generation=operation.expected_generation,
                target_id=operation.target_id,
                execution_kind=operation.execution_kind,
            )
        except McpWorkspaceDevelopmentError as error:
            raise McpAuthorizationError(
                error.code,
                reason=error.reason if error.code == "execution_unsupported" else None,
                diagnostic=(
                    error.diagnostic if error.code == "execution_failed" else None
                ),
                addon_exception=(
                    error.addon_exception if error.code == "execution_failed" else None
                ),
            ) from error
        self._require_development_bounded(result)
        return result

    def remote_action(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
        observe_ui: bool = False,
    ) -> McpRemoteActionResult | ProductionTransientResult:
        if self._remote_actions is None:
            raise McpAuthorizationError("capability_unavailable")
        if observe_ui:
            raise McpAuthorizationError("capability_unavailable")

        if self._authorization.is_production_target(target_id):
            action_class = classify_production_action(action)
            if action_class is ProductionActionClass.PROHIBITED:
                raise McpAuthorizationError("production_action_prohibited")
            if action_class is ProductionActionClass.TRANSIENT_CONTROL:
                self._authorization.authorize_production_transient_access(
                    principal, target_id
                )
                if self._production_transient_approvals is None:
                    raise McpAuthorizationError("capability_unavailable")
                try:
                    result = self._production_transient_approvals.invoke(
                        client_id=principal.client_id,
                        credential_id=principal.credential_id,
                        target_id=target_id,
                        request_id=request_id,
                        action=action,
                    )
                except ProductionApprovalError as error:
                    raise McpAuthorizationError(error.code) from error
                self._require_bounded(result)
                return result

            def authorize_new() -> ProductionNavigationAuthority:
                return self._authorization.authorize_production_navigation(
                    principal, target_id, action
                )

            try:
                result = self._remote_actions.invoke_production(
                    client_id=principal.client_id,
                    credential_id=principal.credential_id,
                    target_id=target_id,
                    request_id=request_id,
                    action=action,
                    authorize_new=authorize_new,
                )
            except McpRemoteActionReceiptError as error:
                raise McpAuthorizationError(error.code) from error
            self._require_bounded(result)
            return result

        self._authorization.authorize_target(principal, "remote_action", target_id)

        def reauthorize() -> None:
            self._authorization.authorize_target(principal, "remote_action", target_id)

        try:
            result = self._remote_actions.invoke(
                client_id=principal.client_id,
                credential_id=principal.credential_id,
                target_id=target_id,
                request_id=request_id,
                action=action,
                before_dispatch=reauthorize,
            )
        except McpRemoteActionReceiptError as error:
            raise McpAuthorizationError(error.code) from error
        self._require_bounded(result)
        return result

    def scan_log(
        self, principal: McpPrincipal, *, target_id: UUID, request: KodiLogScanRequest
    ) -> KodiLogScanResult:
        bound = KodiLogScanBridgeRequest.model_validate(
            {
                **request.model_dump(),
                "client_binding": hashlib.sha256(
                    f"{principal.client_id}:{principal.credential_id}".encode()
                ).hexdigest(),
            }
        )
        return self.read_log(principal, target_id=target_id, request=bound)

    @overload
    def read_log(
        self, principal: McpPrincipal, *, target_id: UUID, request: KodiLogRequest
    ) -> KodiLogResult: ...

    @overload
    def read_log(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request: KodiLogScanBridgeRequest,
    ) -> KodiLogScanResult: ...

    def read_log(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request: KodiLogRequest | KodiLogScanBridgeRequest,
    ) -> KodiLogResult | KodiLogScanResult:
        tool_name = (
            "log_scan" if isinstance(request, KodiLogScanBridgeRequest) else "log_read"
        )

        def read() -> KodiLogResult | KodiLogScanResult:
            if self._target_adapters is None:
                raise McpAuthorizationError("capability_unavailable")
            try:
                result = self._target_adapters.read_log(target_id, request)
            except ManagedInstanceNotFoundError as error:
                raise McpAuthorizationError(
                    "target_not_found_or_not_authorized"
                ) from error
            except KodiTargetAdapterError as error:
                if (
                    error.code == "identity_mismatch"
                    and error.reason in MANAGED_BRIDGE_RECOVERY_REASONS
                ):
                    # Detailed failure facts require current target authority,
                    # including when generic identity errors skip the log fence.
                    self._authorization.authorize_managed_observation_error_detail(
                        principal, tool_name, target_id
                    )
                raise McpAuthorizationError(error.code, reason=error.reason) from error
            self._require_bounded(result)
            return result

        return self._authorization.invoke_target(principal, tool_name, target_id, read)

    def system_info(self, principal: McpPrincipal, *, target_id: UUID) -> SystemInfo:
        def inspect() -> SystemInfo:
            if self._target_adapters is None:
                raise McpAuthorizationError("capability_unavailable")
            try:
                result = self._target_adapters.system_info(target_id)
            except ManagedInstanceNotFoundError as error:
                raise McpAuthorizationError(
                    "target_not_found_or_not_authorized"
                ) from error
            except KodiTargetAdapterError as error:
                raise McpAuthorizationError(error.code) from error
            self._require_bounded(result)
            return result

        return self._authorization.invoke_target(
            principal, "system_info", target_id, inspect
        )

    def inspect_ui_state(
        self, principal: McpPrincipal, *, target_id: UUID
    ) -> UiObservation:
        def inspect() -> UiObservation:
            if self._target_adapters is None:
                raise McpAuthorizationError("capability_unavailable")
            try:
                result = self._target_adapters.inspect_ui_state(target_id)
            except ManagedInstanceNotFoundError as error:
                raise McpAuthorizationError(
                    "target_not_found_or_not_authorized"
                ) from error
            except KodiTargetAdapterError as error:
                if (
                    error.code == "identity_mismatch"
                    and error.reason in MANAGED_BRIDGE_RECOVERY_REASONS
                ):
                    # Detailed failure facts require current target authority,
                    # including when generic identity errors skip the log fence.
                    self._authorization.authorize_managed_observation_error_detail(
                        principal, "inspect_ui_state", target_id
                    )
                raise McpAuthorizationError(error.code, reason=error.reason) from error
            self._require_bounded(result)
            return result

        return self._authorization.invoke_target(
            principal, "inspect_ui_state", target_id, inspect
        )

    def addon_list(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AddonListResult:
        return self._authorization.invoke_target(
            principal,
            "addon_list",
            target_id,
            lambda: self._addon_list_authorized(target_id, cursor=cursor, limit=limit),
        )

    def _addon_list_authorized(
        self, target_id: UUID, *, cursor: str | None, limit: int
    ) -> AddonListResult:
        if (
            limit < 1
            or limit > 100
            or cursor == ""
            or (cursor is not None and len(cursor) > 128)
        ):
            raise McpAuthorizationError("permission_denied")
        if self._target_adapters is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            inventory = self._target_adapters.addon_inventory(target_id)
        except KodiTargetAdapterError as error:
            raise McpAuthorizationError(error.code) from error
        if inventory.query_status is not AddonInventoryQueryStatus.AVAILABLE:
            raise McpAuthorizationError(
                self._inventory_error(inventory.unavailable_reason)
            )
        ordered = sorted(inventory.items, key=lambda item: item.addon_id)
        eligible = [
            item for item in ordered if cursor is None or item.addon_id > cursor
        ]
        selected = eligible[:limit]
        addons = tuple(
            AddonSummary(
                addon_id=item.addon_id,
                name=item.name,
                version=item.version,
                addon_type=item.kodi_type,
                enabled=item.enabled,
                provenance=(
                    None
                    if item.provenance.classification
                    is KodiAddonProvenanceClassification.UNKNOWN
                    else item.provenance.classification.value
                ),
                provenance_status=(
                    None
                    if item.provenance.classification
                    is KodiAddonProvenanceClassification.UNKNOWN
                    else item.provenance.evidence_status.value
                ),
            )
            for item in selected
        )
        result = AddonListResult(
            target_id=target_id,
            kodi_version=inventory.kodi_release,
            total_count=len(ordered),
            addons=addons,
            next_cursor=(
                selected[-1].addon_id if len(eligible) > limit and selected else None
            ),
        )
        self._require_bounded(result)
        return result

    def screenshot_capture(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request_id: UUID | None = None,
    ) -> ScreenshotToolResult:
        production_screenshots = self._production_screenshots
        return self._authorization.invoke_target(
            principal,
            "screenshot_capture",
            target_id,
            lambda: self._screenshot_capture_authorized(
                principal, target_id, request_id=request_id
            ),
            post_authorization_failure=(
                None
                if request_id is None or production_screenshots is None
                else lambda: production_screenshots.discard_pixels(
                    principal.client_id, target_id, request_id
                )
            ),
        )

    def _screenshot_capture_authorized(
        self,
        principal: McpPrincipal,
        target_id: UUID,
        *,
        request_id: UUID | None,
    ) -> ScreenshotToolResult:
        capture_id: UUID
        captured_at: datetime | None
        width: int | None
        height: int | None
        mime_type: str | None
        png: bytes
        byte_size: int | None
        with self._session_factory() as session:
            target = session.get(TargetRow, str(target_id))
            production = bool(
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            )
            if production:
                try:
                    visual_state = evaluate_production_visual_state(session, target_id)
                except ProductionInspectionError as error:
                    raise McpAuthorizationError(error.code) from error
            else:
                visual_state = None
        if production:
            if request_id is None:
                raise McpAuthorizationError("request_id_required")
            if self._production_screenshots is None or visual_state is None:
                raise McpAuthorizationError("screenshot_capture_unavailable")
            try:
                production_result = self._production_screenshots.capture(
                    client_id=principal.client_id,
                    target_id=target_id,
                    request_id=request_id,
                    identity_generation=visual_state.identity_generation,
                    bridge_trust_revision=visual_state.bridge_trust_revision,
                )
            except ProductionScreenshotError as error:
                raise McpAuthorizationError(
                    self._screenshot_error(error.code)
                ) from error
            capture_id = production_result.capture_id
            captured_at = production_result.captured_at
            width = production_result.width
            height = production_result.height
            mime_type = "image/png"
            png = production_result.png
            byte_size = production_result.byte_size
        elif self._target_adapters is not None:
            try:
                adapted = self._target_adapters.capture_screenshot(
                    target_id, request_id=request_id
                )
            except KodiTargetAdapterError as error:
                raise McpAuthorizationError(
                    self._screenshot_error(error.code)
                ) from error
            capture_id = adapted.capture_id
            captured_at = adapted.captured_at
            width = adapted.width
            height = adapted.height
            mime_type = adapted.mime_type
            png = adapted.png
            byte_size = adapted.byte_size
        else:
            raise McpAuthorizationError("screenshot_capture_unavailable")
        if (
            captured_at is None
            or width is None
            or height is None
            or mime_type != PNG_MIME_TYPE
            or byte_size != len(png)
            or not png.startswith(PNG_SIGNATURE)
        ):
            raise McpAuthorizationError("screenshot_capture_unavailable")
        if len(png) > MAX_PNG_BYTES:
            raise McpAuthorizationError("response_too_large")
        metadata = ScreenshotCaptureMetadata(
            target_id=target_id,
            capture_id=capture_id,
            captured_at=captured_at,
            width=width,
            height=height,
            mime_type=PNG_MIME_TYPE,
            byte_size=len(png),
        )
        self._require_bounded(metadata)
        return ScreenshotToolResult(metadata=metadata, png=png)

    def target_list(
        self,
        principal: McpPrincipal,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> TargetListResult:
        return self._authorization.invoke_unscoped(
            principal,
            "target_list",
            lambda: self._target_list_authorized(principal, cursor=cursor, limit=limit),
        )

    def _target_list_authorized(
        self,
        principal: McpPrincipal,
        *,
        cursor: str | None,
        limit: int,
    ) -> TargetListResult:
        if limit < 1 or limit > 100:
            raise McpAuthorizationError("permission_denied")
        cursor_id: UUID | None = None
        if cursor is not None:
            try:
                cursor_id = UUID(cursor)
            except ValueError as error:
                raise McpAuthorizationError("permission_denied") from error
        granted = self._authorization.granted_target_ids(principal)
        eligible: list[UUID] = []
        for target_id in granted:
            if cursor_id is not None and str(target_id) <= str(cursor_id):
                continue
            try:
                self._authorization.authorize_target_eligibility(
                    principal, "target_list", target_id
                )
            except McpAuthorizationError:
                continue
            eligible.append(target_id)
            if len(eligible) > limit:
                break
        selected = eligible[:limit]
        summaries = self._summaries(selected)
        result = TargetListResult(
            targets=summaries,
            next_cursor=(
                str(selected[-1]) if len(eligible) > limit and selected else None
            ),
        )
        self._require_bounded(result)
        return result

    def target_status(
        self, principal: McpPrincipal, *, target_id: UUID
    ) -> TargetStatusResult:
        return self._authorization.invoke_target(
            principal,
            "target_status",
            target_id,
            lambda: self._target_status_authorized(target_id),
        )

    def _target_status_authorized(self, target_id: UUID) -> TargetStatusResult:
        result = TargetStatusResult(target=self._summary(target_id))
        self._require_bounded(result)
        return result

    def _summary(self, target_id: UUID) -> TargetSummary:
        if self._target_adapters is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            item = self._target_adapters.summary(target_id, fresh=True)
        except KodiTargetAdapterError as error:
            raise McpAuthorizationError(error.code) from error
        return TargetSummary(
            **{field: getattr(item, field) for field in TargetSummary.model_fields}
        )

    def _summaries(self, target_ids: list[UUID]) -> tuple[TargetSummary, ...]:
        if not target_ids:
            return ()
        if self._target_adapters is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            items = self._target_adapters.summaries(target_ids)
        except KodiTargetAdapterError as error:
            raise McpAuthorizationError(error.code) from error
        return tuple(
            TargetSummary(
                **{field: getattr(item, field) for field in TargetSummary.model_fields}
            )
            for item in items
        )

    @staticmethod
    def _require_bounded(result: BaseModel) -> None:
        encoded = json.dumps(
            result.model_dump(mode="json"), separators=(",", ":")
        ).encode()
        if len(encoded) > MAX_MCP_RESPONSE_BYTES:
            raise McpAuthorizationError("response_too_large")

    @staticmethod
    def _require_development_bounded(result: BaseModel) -> None:
        encoded = json.dumps(
            result.model_dump(mode="json"), separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_MCP_DEVELOPMENT_RESPONSE_BYTES:
            raise McpAuthorizationError("response_too_large")

    @staticmethod
    def _inventory_error(
        reason: AddonInventoryUnavailableReason | None,
    ) -> str:
        if reason in {
            AddonInventoryUnavailableReason.INSTANCE_NOT_PROVISIONED,
            AddonInventoryUnavailableReason.KODI_NOT_RUNNING,
        }:
            return "target_not_running"
        if reason in {
            AddonInventoryUnavailableReason.KODI_NOT_READY,
            AddonInventoryUnavailableReason.JSONRPC_UNAVAILABLE,
        }:
            return "target_not_ready"
        if reason is AddonInventoryUnavailableReason.INVENTORY_LIMIT_EXCEEDED:
            return "response_too_large"
        return "inventory_unavailable"

    @staticmethod
    def _screenshot_error(code: str) -> str:
        return {
            "screenshots_disabled": "screenshots_disabled",
            "screenshot_preference_changed": "screenshot_preference_changed",
            "screenshot_unsupported": "capability_unavailable",
            "bridge_not_paired": "capability_unavailable",
            "bridge_reverification_required": "bridge_reverification_required",
            "bridge_build_not_reviewed": "bridge_build_not_reviewed",
            "transport_acknowledgement_required": (
                "transport_acknowledgement_required"
            ),
            "bridge_capability_unavailable": "capability_unavailable",
            "bridge_unreachable": "screenshot_capture_unavailable",
            "bridge_authentication_failed": "screenshot_capture_unavailable",
            "bridge_identity_mismatch": "screenshot_capture_unavailable",
            "bridge_protocol_unsupported": "screenshot_capture_unavailable",
            "bridge_replay_rejected": "screenshot_capture_unavailable",
            "request_id_required": "request_id_required",
            "request_id_conflict": "request_id_conflict",
            "screenshot_replay_unavailable": "screenshot_replay_unavailable",
            "rate_limited": "rate_limited",
            "screenshot_invalid": "screenshot_invalid_result",
            "screenshot_payload_missing": "screenshot_payload_missing",
            "screenshot_payload_type_invalid": "screenshot_payload_type_invalid",
            "screenshot_response_schema_invalid": "screenshot_response_schema_invalid",
            "screenshot_decoder_rejected": "screenshot_decoder_rejected",
            "screenshot_format_invalid": "screenshot_format_invalid",
            "screenshot_dimensions_invalid": "screenshot_dimensions_invalid",
            "screenshot_receipt_persistence_failed": (
                "screenshot_receipt_persistence_failed"
            ),
            "screenshot_too_large": "response_too_large",
            "visual_instance_not_running": "target_not_running",
            "visual_kodi_not_ready": "target_not_ready",
        }.get(
            code,
            code
            if code
            in {
                "screenshot_capture_in_progress",
                "visual_rate_limited",
                "screenshot_capture_unavailable",
                "screenshot_invalid_result",
            }
            else "screenshot_capture_unavailable",
        )

"""Thin MCP tool adapters over existing Kodi Lab application services."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    AddonInventoryUnavailableReason,
    KodiAddonProvenanceClassification,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceNotFoundError,
    AgentWorkspaceRead,
    AgentWorkspaceService,
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
    WorkspaceExecutionResult,
)
from kodi_lab.application.mcp_workspace_mutations import (
    McpWorkspaceCreationService,
    McpWorkspaceMutationError,
    WorkspaceCreateResult,
)
from kodi_lab.application.mcp_workspace_retention import McpWorkspaceRetentionService
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
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.screenshots import (
    ManagedScreenshotService,
    ScreenshotServiceError,
)
from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureError,
    WorkspaceSourceCaptureService,
)
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.development.sources import DevelopmentSourceValidationError
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceCreationState,
    AgentWorkspaceState,
    TargetType,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE, PNG_SIGNATURE
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION
from kodi_lab.storage.models import ManagedInstanceRow, TargetRow
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
    expected_generation: int = Field(ge=0)


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
    """One flat strict discriminator for the three Phase 9 text mutations."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    expected_generation: int = Field(ge=0)
    operation: Literal["create", "replace", "exact_patch"]
    relative_path: str = Field(min_length=1, max_length=512)
    content: str | None = None
    expected_text: str | None = None
    replacement_text: str | None = None

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
    expected_generation: int = Field(ge=0)


class WorkspaceTargetOperationInput(BaseModel):
    """The exact closed input shared by deployment and fixed execution."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    workspace_id: UUID
    expected_generation: int = Field(ge=0)
    target_id: UUID


class WorkspaceDeployInput(WorkspaceTargetOperationInput):
    """Existing deployment tool with an explicit transport selection."""

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
    promotable_classification: Literal["promotable", "addon_identity_changed"]
    changed_paths: tuple[WorkspaceValidatedPath, ...] = Field(max_length=64)
    truncated: StrictBool


class McpTargetToolService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        authorization: McpAuthorizationService,
        status: ManagedRuntimeStatusService,
        addon_inventory: ManagedInstanceAddonInventoryService,
        screenshots: ManagedScreenshotService,
        remote_actions: McpRemoteActionReceiptService | None = None,
        workspaces: AgentWorkspaceService | None = None,
        workspace_creation: McpWorkspaceCreationService | None = None,
        workspace_retention: McpWorkspaceRetentionService | None = None,
        workspace_development: McpWorkspaceDevelopmentService | None = None,
        promotion_requests: McpPromotionRequestService | None = None,
        target_adapters: KodiTargetAdapterService | None = None,
        production_screenshots: ProductionScreenshotService | None = None,
        production_transient_approvals: (
            ProductionTransientApprovalService | None
        ) = None,
        workspace_source_captures: WorkspaceSourceCaptureService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._status = status
        self._addon_inventory = addon_inventory
        self._screenshots = screenshots
        self._remote_actions = remote_actions
        self._workspaces = workspaces
        self._workspace_creation = workspace_creation
        self._workspace_retention = workspace_retention
        self._workspace_development = workspace_development
        self._promotion_requests = promotion_requests
        self._target_adapters = target_adapters
        self._production_screenshots = production_screenshots
        self._production_transient_approvals = production_transient_approvals
        self._workspace_source_captures = workspace_source_captures
        self._active_edit_lock = threading.Lock()
        self._active_edits: set[UUID] = set()
        self._validation_lock = threading.Lock()
        self._validation_slots = threading.BoundedSemaphore(2)
        self._active_validation_clients: set[UUID] = set()
        self._active_validation_workspaces: set[UUID] = set()

    def workspace_create(
        self,
        principal: McpPrincipal,
        *,
        request_id: UUID,
        source_id: UUID,
    ) -> WorkspaceCreateResult:
        self._authorization.authorize_development_source(
            principal, "workspace_create", source_id
        )
        if self._workspace_creation is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            result = self._workspace_creation.create(
                client_id=principal.client_id,
                request_id=request_id,
                source_id=source_id,
            )
        except McpWorkspaceMutationError as error:
            raise McpAuthorizationError(error.code) from error
        self._require_development_bounded(result)
        return result

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

    def workspace_edit(
        self, principal: McpPrincipal, edit: WorkspaceEditInput
    ) -> WorkspaceEditResult:
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_edit", edit.workspace_id
        )
        service = self._workspace_service()
        with self._active_edit_lock:
            if edit.workspace_id in self._active_edits:
                raise McpAuthorizationError("workspace_generation_conflict")
            self._active_edits.add(edit.workspace_id)
        try:
            try:
                if edit.operation == "create":
                    assert edit.content is not None
                    mutation = service.create_text_file(
                        edit.workspace_id,
                        expected_generation=edit.expected_generation,
                        relative_path=edit.relative_path,
                        content=edit.content,
                    )
                    content = edit.content
                elif edit.operation == "replace":
                    assert edit.content is not None
                    mutation = service.replace_text_file(
                        edit.workspace_id,
                        expected_generation=edit.expected_generation,
                        relative_path=edit.relative_path,
                        content=edit.content,
                    )
                    content = edit.content
                else:
                    assert edit.expected_text is not None
                    assert edit.replacement_text is not None
                    mutation = service.apply_exact_patch(
                        edit.workspace_id,
                        expected_generation=edit.expected_generation,
                        relative_path=edit.relative_path,
                        expected_text=edit.expected_text,
                        replacement_text=edit.replacement_text,
                    )
                    content = edit.replacement_text
            except (
                AgentWorkspaceConflictError,
                AgentWorkspaceNotFoundError,
                AgentWorkspaceStorageError,
            ) as error:
                raise McpAuthorizationError(
                    self._workspace_edit_error(error)
                ) from error
        finally:
            with self._active_edit_lock:
                self._active_edits.remove(edit.workspace_id)
        result = WorkspaceEditResult(
            workspace_id=mutation.workspace_id,
            source_id=workspace.development_source_id,
            relative_path=mutation.path,
            operation=edit.operation,
            prior_generation=edit.expected_generation,
            new_generation=mutation.generation,
            file_byte_size=len(content.encode("utf-8")),
        )
        self._require_development_bounded(result)
        return result

    def workspace_source_scan(
        self, principal: McpPrincipal, workspace_id: UUID
    ) -> WorkspaceSourceScanToolResult:
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_source_scan", workspace_id
        )
        if self._workspace_source_captures is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            scan = self._workspace_source_captures.scan_source(workspace_id)
        except (
            WorkspaceSourceCaptureError,
            DevelopmentTreeError,
            AgentWorkspaceStorageError,
            DevelopmentSourceValidationError,
        ) as error:
            raise McpAuthorizationError(
                "development_source_scan_unsafe", reason=_source_capture_reason(error)
            ) from error
        except Exception as error:
            raise McpAuthorizationError("development_source_scan_unsafe") from error
        result = WorkspaceSourceScanToolResult(
            workspace_id=workspace_id,
            source_id=workspace.development_source_id,
            addon_id=scan.addon_id,
            addon_version=scan.addon_version,
            source_tree_digest=scan.tree_digest,
            scanned_at=scan.scanned_at,
            active_capture_version=(
                workspace.active_observed_version or workspace.source_observed_version
            ),
            active_capture_digest=workspace.baseline_manifest_digest,
            workspace_generation=workspace.generation,
            workspace_tree_digest=workspace.current_tree_digest,
            source_matches_capture=scan.tree_digest
            == workspace.baseline_manifest_digest,
            private_workspace_edits=(
                workspace.current_tree_digest != workspace.baseline_manifest_digest
            ),
        )
        self._require_development_bounded(result)
        return result

    def workspace_capture(
        self, principal: McpPrincipal, operation: WorkspaceCaptureToolInput
    ) -> WorkspaceCaptureToolResult:
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_capture", operation.workspace_id
        )
        if self._workspace_source_captures is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            captured = self._workspace_source_captures.capture(
                operation.workspace_id,
                expected_generation=operation.expected_generation,
            )
        except (
            WorkspaceSourceCaptureError,
            DevelopmentTreeError,
            AgentWorkspaceStorageError,
            DevelopmentSourceValidationError,
        ) as error:
            raise McpAuthorizationError(
                "workspace_capture_rejected", reason=_source_capture_reason(error)
            ) from error
        except Exception as error:
            raise McpAuthorizationError("workspace_capture_rejected") from error
        result = WorkspaceCaptureToolResult(
            workspace_id=captured.workspace_id,
            source_id=workspace.development_source_id,
            operation_id=captured.operation_id,
            generation=captured.generation,
            tree_digest=captured.tree_digest,
            changed=captured.changed,
        )
        self._require_development_bounded(result)
        return result

    def workspace_validate(
        self, principal: McpPrincipal, validation: WorkspaceValidateInput
    ) -> WorkspaceValidateResult:
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_validate", validation.workspace_id
        )
        service = self._workspace_service()
        with self._validation_lock:
            if (
                principal.client_id in self._active_validation_clients
                or validation.workspace_id in self._active_validation_workspaces
                or not self._validation_slots.acquire(blocking=False)
            ):
                raise McpAuthorizationError("validation_busy")
            self._active_validation_clients.add(principal.client_id)
            self._active_validation_workspaces.add(validation.workspace_id)
        try:
            try:
                observed = service.validate_structure(
                    validation.workspace_id,
                    expected_generation=validation.expected_generation,
                )
            except (
                AgentWorkspaceConflictError,
                AgentWorkspaceNotFoundError,
                AgentWorkspaceStorageError,
            ) as error:
                raise McpAuthorizationError(
                    self._workspace_validation_error(error)
                ) from error
        finally:
            with self._validation_lock:
                self._active_validation_clients.remove(principal.client_id)
                self._active_validation_workspaces.remove(validation.workspace_id)
                self._validation_slots.release()
        paths = tuple(
            WorkspaceValidatedPath(path=item.path, change_kind=item.change_kind)
            for item in observed.changes[:64]
        )
        added = sum(item.change_kind == "added" for item in observed.changes)
        modified = len(observed.changes) - added
        result = WorkspaceValidateResult(
            workspace_id=observed.workspace_id,
            source_id=workspace.development_source_id,
            observed_generation=observed.observed_generation,
            validated_at=observed.validated_at,
            addon_id=observed.addon_id,
            addon_version=observed.addon_version,
            extension_types=observed.extension_types,
            identity_classification=(
                "changed" if observed.addon_id_classification == "changed" else "stable"
            ),
            addon_id_classification=observed.addon_id_classification,
            version_classification=observed.version_classification,
            extension_type_classification=observed.extension_type_classification,
            changed_file_count=len(observed.changes),
            added_file_count=added,
            modified_file_count=modified,
            promotable=observed.promotable,
            promotable_classification=observed.promotable_classification,
            changed_paths=paths,
            truncated=len(paths) < len(observed.changes),
        )
        if self._workspace_development is not None:
            self._workspace_development.record_validation(
                workspace_id=result.workspace_id,
                source_id=result.source_id,
                generation=result.observed_generation,
                addon_id=result.addon_id,
                addon_version=result.addon_version,
                extension_types=result.extension_types,
                validated_at=result.validated_at,
            )
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
        try:
            result = self._workspace_development.deploy(
                client_id=principal.client_id,
                request_id=operation.request_id,
                workspace_id=operation.workspace_id,
                expected_generation=operation.expected_generation,
                target_id=operation.target_id,
                deployment_mode=operation.deployment_mode,
            )
        except McpWorkspaceDevelopmentError as error:
            raise McpAuthorizationError(error.code) from error
        self._require_development_bounded(result)
        return result

    def workspace_execute(
        self, principal: McpPrincipal, operation: WorkspaceTargetOperationInput
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
            )
        except McpWorkspaceDevelopmentError as error:
            raise McpAuthorizationError(
                error.code,
                reason=error.reason if error.code == "execution_unsupported" else None,
                diagnostic=(
                    error.diagnostic if error.code == "execution_failed" else None
                ),
            ) from error
        self._require_development_bounded(result)
        return result

    def development_source_list(
        self,
        principal: McpPrincipal,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> DevelopmentSourceListResult:
        if limit < 1 or limit > 100:
            raise McpAuthorizationError("permission_denied")
        cursor_id = self._uuid_cursor(cursor)
        sources, has_more = self._authorization.granted_development_sources(
            principal,
            "development_source_list",
            cursor=cursor_id,
            limit=limit,
        )
        result = DevelopmentSourceListResult(
            sources=tuple(
                DevelopmentSourceSummary(
                    source_id=source.id,
                    logical_name=source.logical_name,
                    source_type=source.source_kind.value,
                    addon_id=source.addon_id,
                    addon_name=source.observed_name,
                    version=source.observed_version,
                    extension_types=source.extension_types,
                    registration_observed_at=source.validated_at,
                )
                for source in sources
            ),
            next_cursor=str(sources[-1].id) if has_more and sources else None,
        )
        self._require_development_bounded(result)
        return result

    def workspace_status(
        self,
        principal: McpPrincipal,
        *,
        workspace_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> WorkspaceStatusResult:
        service = self._workspace_service()
        if (
            limit < 1
            or limit > 100
            or (workspace_id is not None and cursor is not None)
        ):
            raise McpAuthorizationError("permission_denied")
        has_more = False
        authorized: tuple[AgentWorkspace, ...]
        if workspace_id is not None:
            authorized = (
                self._authorization.authorize_workspace(
                    principal, "workspace_status", workspace_id
                ),
            )
        else:
            authorized, has_more = self._authorization.owned_workspaces(
                principal,
                "workspace_status",
                cursor=self._uuid_cursor(cursor),
                limit=limit,
            )
        summaries = tuple(
            self._workspace_summary(service.get(workspace.id), workspace.updated_at)
            for workspace in authorized
        )
        result = WorkspaceStatusResult(
            workspaces=summaries,
            next_cursor=(str(authorized[-1].id) if has_more and authorized else None),
        )
        self._require_development_bounded(result)
        return result

    def workspace_tree(
        self,
        principal: McpPrincipal,
        *,
        workspace_id: UUID,
        relative_path: str = "",
        cursor: str | None = None,
        limit: int = 100,
    ) -> WorkspaceTreeResult:
        service = self._workspace_service()
        if limit < 1 or limit > 200:
            raise McpAuthorizationError("permission_denied")
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_tree", workspace_id
        )
        try:
            items = service.inspect_directory(workspace_id, relative_path)
        except (
            AgentWorkspaceConflictError,
            AgentWorkspaceNotFoundError,
            AgentWorkspaceStorageError,
        ) as error:
            raise McpAuthorizationError(self._workspace_error(error)) from error
        cursor_name = self._decode_tree_cursor(cursor)
        eligible = [
            item
            for item in items
            if cursor_name is None
            or item.name.encode("utf-8") > cursor_name.encode("utf-8")
        ]
        selected = eligible[:limit]
        prefix = f"{relative_path}/" if relative_path else ""
        result = WorkspaceTreeResult(
            workspace_id=workspace.id,
            source_id=workspace.development_source_id,
            directory=relative_path,
            observed_generation=workspace.generation,
            entries=tuple(
                WorkspaceTreeEntrySummary(
                    path=f"{prefix}{item.name}",
                    entry_type=item.entry_type,
                    byte_size=item.size,
                )
                for item in selected
            ),
            next_cursor=(
                self._encode_tree_cursor(selected[-1].name)
                if len(eligible) > limit and selected
                else None
            ),
        )
        self._require_development_bounded(result)
        return result

    def workspace_read(
        self,
        principal: McpPrincipal,
        *,
        workspace_id: UUID,
        relative_path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> WorkspaceReadResult:
        service = self._workspace_service()
        if start_line < 1 or max_lines < 1 or max_lines > 2000:
            raise McpAuthorizationError("permission_denied")
        workspace = self._authorization.authorize_workspace(
            principal, "workspace_read", workspace_id
        )
        try:
            read = service.read_file(workspace_id, relative_path)
        except (
            AgentWorkspaceConflictError,
            AgentWorkspaceNotFoundError,
            AgentWorkspaceStorageError,
        ) as error:
            raise McpAuthorizationError(self._workspace_error(error)) from error
        payload = read.content.encode("utf-8")
        lines = read.content.splitlines(keepends=True)
        offset = start_line - 1
        candidates = lines[offset : offset + max_lines]
        selected: list[str] = []
        selected_bytes = 0
        for line in candidates:
            line_bytes = len(line.encode("utf-8"))
            if selected_bytes + line_bytes > MAX_MCP_DEVELOPMENT_TEXT_BYTES:
                break
            selected.append(line)
            selected_bytes += line_bytes
        if candidates and not selected:
            raise McpAuthorizationError("response_too_large")
        next_line = (
            start_line + len(selected) if offset + len(selected) < len(lines) else None
        )
        result = WorkspaceReadResult(
            workspace_id=workspace.id,
            source_id=workspace.development_source_id,
            path=relative_path,
            text="".join(selected),
            observed_generation=workspace.generation,
            sha256=hashlib.sha256(payload).hexdigest(),
            start_line=start_line,
            returned_line_count=len(selected),
            next_line=next_line,
            total_file_bytes=len(payload),
            truncated=start_line > 1 or next_line is not None,
        )
        self._require_development_bounded(result)
        return result

    def remote_action(
        self,
        principal: McpPrincipal,
        *,
        target_id: UUID,
        request_id: UUID,
        action: BrowserRemoteAction,
    ) -> McpRemoteActionResult | ProductionTransientResult:
        if self._remote_actions is None:
            raise McpAuthorizationError("capability_unavailable")

        if self._authorization.is_production_target(target_id):
            action_class = classify_production_action(action)
            if action_class is ProductionActionClass.PROHIBITED:
                raise McpAuthorizationError("production_action_prohibited")
            if action_class is ProductionActionClass.TRANSIENT_CONTROL:
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
        try:
            inventory = (
                self._addon_inventory.get(target_id)
                if self._target_adapters is None
                else self._target_adapters.addon_inventory(target_id)
            )
        except ManagedInstanceNotFoundError as error:
            raise McpAuthorizationError("target_not_found_or_not_authorized") from error
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
            try:
                result = self._screenshots.capture_image(target_id)
            except ManagedInstanceNotFoundError as error:
                raise McpAuthorizationError(
                    "target_not_found_or_not_authorized"
                ) from error
            except ScreenshotServiceError as error:
                raise McpAuthorizationError(
                    self._screenshot_error(error.code)
                ) from error
            status = result.capture.screenshot
            capture_id = result.capture.capture_id
            captured_at = status.captured_at
            width = status.width
            height = status.height
            mime_type = status.mime_type
            png = result.png
            byte_size = status.byte_size
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
        if self._target_adapters is not None:
            try:
                item = self._target_adapters.summary(target_id, fresh=True)
            except KodiTargetAdapterError as error:
                raise McpAuthorizationError(error.code) from error
            return TargetSummary(
                **{field: getattr(item, field) for field in TargetSummary.model_fields}
            )
        try:
            status = self._status.get(target_id)
        except ManagedInstanceNotFoundError as error:
            raise McpAuthorizationError("target_not_found_or_not_authorized") from error
        with self._session_factory() as session:
            instance = session.get(ManagedInstanceRow, str(target_id))
            if instance is None:
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            profile = f"managed_{instance.instance_type.value}"
        return self._summary_from_status(status, profile)

    def _summaries(self, target_ids: list[UUID]) -> tuple[TargetSummary, ...]:
        if not target_ids:
            return ()
        if self._target_adapters is not None:
            try:
                items = self._target_adapters.summaries(target_ids)
            except KodiTargetAdapterError as error:
                raise McpAuthorizationError(error.code) from error
            return tuple(
                TargetSummary(
                    **{
                        field: getattr(item, field)
                        for field in TargetSummary.model_fields
                    }
                )
                for item in items
            )
        statuses = {
            item.managed_instance_id: item for item in self._status.list_statuses()
        }
        with self._session_factory() as session:
            instances = session.scalars(
                select(ManagedInstanceRow).where(
                    ManagedInstanceRow.target_id.in_(
                        [str(target_id) for target_id in target_ids]
                    )
                )
            ).all()
            profiles = {
                UUID(instance.target_id): f"managed_{instance.instance_type.value}"
                for instance in instances
            }
        try:
            return tuple(
                self._summary_from_status(statuses[target_id], profiles[target_id])
                for target_id in target_ids
            )
        except KeyError as error:
            raise McpAuthorizationError("target_not_found_or_not_authorized") from error

    def _workspace_service(self) -> AgentWorkspaceService:
        if self._workspaces is None:
            raise McpAuthorizationError("capability_unavailable")
        return self._workspaces

    def _workspace_summary(
        self, workspace: AgentWorkspaceRead, updated_at: datetime
    ) -> WorkspaceSummary:
        creation = self._workspace_service().creation_status_for_workspace(workspace.id)
        creation_state = "reconciliation_required"
        if (
            creation is not None
            and creation.workspace_id == workspace.id
            and creation.development_source_id == workspace.development_source_id
        ):
            if creation.state is AgentWorkspaceCreationState.SUCCEEDED:
                if workspace.captured_at is not None and workspace.state not in {
                    AgentWorkspaceState.CREATING,
                    AgentWorkspaceState.CAPTURING,
                }:
                    creation_state = "ready"
            elif creation.state is AgentWorkspaceCreationState.FAILED:
                if (
                    workspace.state is AgentWorkspaceState.ABANDONED
                    and workspace.captured_at is None
                ):
                    creation_state = "abandoned"
            elif (
                creation.state
                in {
                    AgentWorkspaceCreationState.QUEUED,
                    AgentWorkspaceCreationState.RUNNING,
                }
                and workspace.state
                in {AgentWorkspaceState.CREATING, AgentWorkspaceState.CAPTURING}
                and workspace.captured_at is None
            ):
                creation_state = "pending"
        projection = (
            None
            if self._workspace_retention is None
            else self._workspace_retention.projection(workspace.id)
        )
        development = (
            None
            if self._workspace_development is None
            else self._workspace_development.projection(workspace.id)
        )
        promotion = (
            None
            if self._promotion_requests is None
            else self._promotion_requests.projection(workspace.id)
        )
        return WorkspaceSummary(
            workspace_id=workspace.id,
            source_id=workspace.development_source_id,
            lifecycle_state=workspace.state.value,
            bytes_state=workspace.bytes_state.value,
            generation=workspace.generation,
            creation_state=creation_state,
            created_at=workspace.created_at,
            updated_at=updated_at,
            latest_promotion_state=(
                None
                if workspace.promotion_outcome is None
                else workspace.promotion_outcome.value
            ),
            latest_verification_state=(
                None
                if workspace.verification_outcome is None
                else workspace.verification_outcome.value
            ),
            retention_classification=(
                "active" if projection is None else projection.classification
            ),
            retention_deadline_at=(
                None if projection is None else projection.deadline_at
            ),
            automatic_cleanup_blocked=(
                False if projection is None else projection.automatic_cleanup_blocked
            ),
            latest_validation_id=(
                None if development is None else development.validation_id
            ),
            latest_validation_state=(
                None if development is None else development.validation_state
            ),
            latest_validation_generation=(
                None if development is None else development.validation_generation
            ),
            latest_deployment_id=(
                None if development is None else development.deployment_id
            ),
            latest_deployment_state=(
                None if development is None else development.deployment_state
            ),
            latest_deployment_mode=(
                None if development is None else development.deployment_mode
            ),
            latest_deployment_phase=(
                None if development is None else development.deployment_phase
            ),
            latest_deployment_publication_id=(
                None if development is None else development.deployment_publication_id
            ),
            latest_deployment_installation_kind=(
                None
                if development is None
                else development.deployment_installation_kind
            ),
            latest_deployment_next_action=(
                None if development is None else development.deployment_next_action
            ),
            latest_deployment_generation=(
                None if development is None else development.deployment_generation
            ),
            latest_deployment_target_id=(
                None if development is None else development.deployment_target_id
            ),
            latest_execution_id=(
                None if development is None else development.execution_id
            ),
            latest_execution_state=(
                None if development is None else development.execution_state
            ),
            latest_execution_generation=(
                None if development is None else development.execution_generation
            ),
            latest_execution_target_id=(
                None if development is None else development.execution_target_id
            ),
            latest_promotion_plan_id=(None if promotion is None else promotion.plan_id),
            latest_promotion_review_state=(
                None if promotion is None else promotion.review_state
            ),
            latest_promotion_operation_id=(
                None if promotion is None else promotion.promotion_operation_id
            ),
            latest_promotion_operation_state=(
                None if promotion is None else promotion.promotion_operation_state
            ),
            latest_promotion_verification_id=(
                None if promotion is None else promotion.verification_id
            ),
            latest_promotion_verification_state=(
                None if promotion is None else promotion.verification_state
            ),
            latest_promotion_final_classification=(
                None if promotion is None else promotion.final_classification
            ),
        )

    @staticmethod
    def _uuid_cursor(cursor: str | None) -> UUID | None:
        if cursor is None:
            return None
        try:
            parsed = UUID(cursor)
        except (ValueError, AttributeError) as error:
            raise McpAuthorizationError("permission_denied") from error
        if str(parsed) != cursor:
            raise McpAuthorizationError("permission_denied")
        return parsed

    @staticmethod
    def _encode_tree_cursor(name: str) -> str:
        return (
            base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
        )

    @staticmethod
    def _decode_tree_cursor(cursor: str | None) -> str | None:
        if cursor is None:
            return None
        if not cursor or len(cursor) > 512:
            raise McpAuthorizationError("path_not_allowed")
        try:
            payload = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            name = payload.decode("utf-8", "strict")
        except (ValueError, UnicodeDecodeError) as error:
            raise McpAuthorizationError("path_not_allowed") from error
        if not name or "/" in name or "\\" in name or "\x00" in name:
            raise McpAuthorizationError("path_not_allowed")
        return name

    @staticmethod
    def _workspace_error(error: Exception) -> str:
        code = getattr(error, "code", "")
        if code in {"workspace_bytes_cleaned"}:
            return "workspace_bytes_cleaned"
        if code in {"agent_workspace_not_ready", "workspace_reconciliation_blocked"}:
            return "workspace_not_ready"
        if code in {
            "agent_workspace_path_invalid",
            "agent_workspace_path_unavailable",
        }:
            return "path_not_allowed"
        if code in {
            "agent_workspace_entry_unsafe",
            "agent_workspace_file_unsafe",
            "agent_workspace_file_not_text",
        }:
            return "unsupported_file_type"
        if code == "agent_workspace_file_too_large":
            return "file_too_large"
        if code == "agent_workspace_directory_too_large":
            return "response_too_large"
        if code == "agent_workspace_not_found":
            return "workspace_integrity_changed"
        return "workspace_integrity_changed"

    @staticmethod
    def _workspace_edit_error(error: Exception) -> str:
        code = getattr(error, "code", "")
        if code == "agent_workspace_generation_conflict":
            return "workspace_generation_conflict"
        if code in {
            "agent_workspace_not_ready",
            "workspace_reconciliation_blocked",
            "agent_workspace_promotion_active",
        }:
            return "workspace_not_ready"
        if code in {
            "agent_workspace_path_invalid",
            "agent_workspace_path_unavailable",
            "agent_workspace_parent_unsafe",
            "agent_workspace_create_conflict",
            "agent_workspace_replace_missing",
        }:
            return "path_not_allowed"
        if code == "agent_workspace_patch_preimage_mismatch":
            return "workspace_generation_conflict"
        if code in {
            "agent_workspace_entry_unsafe",
            "agent_workspace_file_unsafe",
            "agent_workspace_file_not_text",
            "agent_workspace_addon_xml_invalid",
        }:
            return "unsupported_file_type"
        if code in {
            "agent_workspace_file_too_large",
            "agent_workspace_content_too_large",
        }:
            return "file_too_large"
        return "workspace_integrity_changed"

    @staticmethod
    def _workspace_validation_error(error: Exception) -> str:
        code = getattr(error, "code", "")
        if code == "agent_workspace_generation_conflict":
            return "workspace_generation_conflict"
        if code in {
            "agent_workspace_not_ready",
            "workspace_reconciliation_blocked",
            "agent_workspace_promotion_active",
        }:
            return "workspace_not_ready"
        if code == "agent_workspace_busy":
            return "validation_busy"
        if code == "agent_workspace_baseline_entry_missing":
            return "unsupported_deletion"
        if code in {
            "agent_workspace_type_transition",
            "agent_workspace_mode_transition",
        }:
            return "unsupported_type_transition"
        if code in {
            "agent_workspace_changed_binary",
            "agent_workspace_tree_unsafe",
            "development_source_unsafe_entry",
        }:
            return "unsupported_file_type"
        if code == "agent_workspace_addon_xml_invalid":
            return "validation_failed"
        return "workspace_integrity_changed"

    @staticmethod
    def _summary_from_status(
        status: ManagedRuntimeStatus, profile: str
    ) -> TargetSummary:
        return TargetSummary(
            target_id=status.managed_instance_id,
            display_name=status.name,
            kodi_version=status.kodi_version,
            runtime_profile=profile,
            provision_state=status.provision_state.value,
            runtime_state=status.container_state.value,
            process_state=status.process_state.value,
            jsonrpc_state=status.jsonrpc_state.value,
            usability_state=status.usability_state.value,
            readiness_state=status.readiness_state.value,
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

"""Thin MCP tool adapters over existing Kodi Lab application services."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool
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
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.screenshots import (
    ManagedScreenshotService,
    ScreenshotServiceError,
)
from kodi_lab.domain import AgentWorkspace
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE, PNG_SIGNATURE
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION
from kodi_lab.storage.models import ManagedInstanceRow
from kodi_lab.workspace import AgentWorkspaceStorageError

MAX_MCP_RESPONSE_BYTES = 64 * 1024
MAX_MCP_DEVELOPMENT_RESPONSE_BYTES = 48 * 1024
MAX_MCP_DEVELOPMENT_TEXT_BYTES = 44 * 1024


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
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._status = status
        self._addon_inventory = addon_inventory
        self._screenshots = screenshots
        self._remote_actions = remote_actions
        self._workspaces = workspaces

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
    ) -> McpRemoteActionResult:
        self._authorization.authorize_target(principal, "remote_action", target_id)
        if self._remote_actions is None:
            raise McpAuthorizationError("capability_unavailable")
        try:
            result = self._remote_actions.invoke(
                client_id=principal.client_id,
                credential_id=principal.credential_id,
                target_id=target_id,
                request_id=request_id,
                action=action,
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
            inventory = self._addon_inventory.get(target_id)
        except ManagedInstanceNotFoundError as error:
            raise McpAuthorizationError("target_not_found_or_not_authorized") from error
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
        self, principal: McpPrincipal, *, target_id: UUID
    ) -> ScreenshotToolResult:
        return self._authorization.invoke_target(
            principal,
            "screenshot_capture",
            target_id,
            lambda: self._screenshot_capture_authorized(target_id),
        )

    def _screenshot_capture_authorized(self, target_id: UUID) -> ScreenshotToolResult:
        try:
            result = self._screenshots.capture_image(target_id)
        except ManagedInstanceNotFoundError as error:
            raise McpAuthorizationError("target_not_found_or_not_authorized") from error
        except ScreenshotServiceError as error:
            raise McpAuthorizationError(self._screenshot_error(error.code)) from error
        status = result.capture.screenshot
        if (
            status.capture_id != result.capture.capture_id
            or status.captured_at is None
            or status.width is None
            or status.height is None
            or status.mime_type != PNG_MIME_TYPE
            or status.byte_size != len(result.png)
            or not result.png.startswith(PNG_SIGNATURE)
        ):
            raise McpAuthorizationError("screenshot_capture_unavailable")
        if len(result.png) > MAX_PNG_BYTES:
            raise McpAuthorizationError("response_too_large")
        metadata = ScreenshotCaptureMetadata(
            target_id=target_id,
            capture_id=result.capture.capture_id,
            captured_at=status.captured_at,
            width=status.width,
            height=status.height,
            mime_type=PNG_MIME_TYPE,
            byte_size=len(result.png),
        )
        self._require_bounded(metadata)
        return ScreenshotToolResult(metadata=metadata, png=result.png)

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

    @staticmethod
    def _workspace_summary(
        workspace: AgentWorkspaceRead, updated_at: datetime
    ) -> WorkspaceSummary:
        return WorkspaceSummary(
            workspace_id=workspace.id,
            source_id=workspace.development_source_id,
            lifecycle_state=workspace.state.value,
            bytes_state=workspace.bytes_state.value,
            generation=workspace.generation,
            creation_state=(
                "ready" if workspace.captured_at is not None else "pending"
            ),
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

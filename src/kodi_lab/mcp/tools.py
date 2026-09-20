"""Thin MCP tool adapters over existing Kodi Lab application services."""

from __future__ import annotations

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
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.mcp_administration import McpPrincipal
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.screenshots import (
    ManagedScreenshotService,
    ScreenshotServiceError,
)
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE, PNG_SIGNATURE
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION
from kodi_lab.storage.models import ManagedInstanceRow

MAX_MCP_RESPONSE_BYTES = 64 * 1024


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


class McpTargetToolService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        authorization: McpAuthorizationService,
        status: ManagedRuntimeStatusService,
        addon_inventory: ManagedInstanceAddonInventoryService,
        screenshots: ManagedScreenshotService,
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._status = status
        self._addon_inventory = addon_inventory
        self._screenshots = screenshots

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

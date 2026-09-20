"""Thin MCP tool adapters over existing Kodi Lab application services."""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.mcp_administration import McpPrincipal
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION
from kodi_lab.storage.models import ManagedInstanceRow

MAX_MCP_RESPONSE_BYTES = 64 * 1024


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
    ) -> None:
        self._session_factory = session_factory
        self._authorization = authorization
        self._status = status

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

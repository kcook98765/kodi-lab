"""One common durable authority intersection for every MCP tool call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import McpPrincipal
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.application.screenshots import ManagedScreenshotService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.kodi.remote import REMOTE_RUNTIME_REVISION_IDS
from kodi_lab.mcp.registry import McpToolDefinition, McpToolRegistry, TargetClass
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpClientRow,
    McpClientTargetGrantRow,
    McpPresetPermissionRow,
    McpTargetPresetAssignmentRow,
)

_ResultT = TypeVar("_ResultT")


class McpAuthorizationError(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TargetTechnicalAuthority:
    target_class: TargetClass
    capabilities: frozenset[str]
    current_states: frozenset[str]


class TargetAuthorityProvider(Protocol):
    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority: ...


class InternalManagedTargetAuthorityProvider:
    """Current Phase 11 resolver; later target adapters supply other classes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        reconciliation: ManagedInstanceReconciliationService | None = None,
        screenshots: ManagedScreenshotService | None = None,
        status: ManagedRuntimeStatusService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._reconciliation = reconciliation
        self._screenshots = screenshots
        self._status = status

    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
        capabilities = {"managed_runtime_status", "addon_inventory"}
        states: set[str] = set()
        if self._session_factory is not None and self._screenshots is not None:
            with self._session_factory() as session:
                instance = session.get(ManagedInstanceRow, str(target_id))
            if instance is not None and self._screenshots.supports_runtime_revision(
                UUID(instance.runtime_revision_id)
            ):
                capabilities.add("managed_screenshot")
            if (
                instance is not None
                and UUID(instance.runtime_revision_id) in REMOTE_RUNTIME_REVISION_IDS
            ):
                capabilities.add("semantic_remote")
        if self._status is not None:
            runtime = self._status.get(target_id)
            if runtime.container_state is ContainerRuntimeState.RUNNING:
                states.add("running")
            if (
                runtime.process_state is KodiProcessState.RUNNING
                and runtime.usability_state is SemanticUsabilityState.USABLE
            ):
                states.add("ready")
        if (
            self._reconciliation is not None
            and not self._reconciliation.status(target_id).blocking
        ):
            states.add("reconciled")
        return TargetTechnicalAuthority(
            target_class=TargetClass.INTERNAL_LAB,
            capabilities=frozenset(capabilities),
            current_states=frozenset(states),
        )


class McpAuthorizationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: McpToolRegistry,
        *,
        targets: TargetAuthorityProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._targets = targets or InternalManagedTargetAuthorityProvider()

    def discoverable_tools(self, principal: McpPrincipal) -> tuple[str, ...]:
        permissions = self._client_permissions(principal.client_id)
        return tuple(item.name for item in self._registry.discoverable(permissions))

    def authorize_unscoped(
        self, principal: McpPrincipal, tool_name: str
    ) -> McpToolDefinition:
        definition = self._definition(tool_name)
        if definition.target_scope_required:
            raise McpAuthorizationError("permission_denied")
        permissions = self._client_permissions(principal.client_id)
        if not definition.permissions.issubset(permissions):
            raise McpAuthorizationError("permission_denied")
        return definition

    def authorize_target(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> McpToolDefinition:
        definition = self._definition(tool_name)
        if not definition.target_scope_required:
            raise McpAuthorizationError("permission_denied")
        return self._authorize_target_definition(principal, definition, target_id)

    def authorize_target_eligibility(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> McpToolDefinition:
        """Apply target policy when an unscoped listing projects candidates."""

        definition = self._definition(tool_name)
        if definition.target_scope_required:
            raise McpAuthorizationError("permission_denied")
        return self._authorize_target_definition(principal, definition, target_id)

    def _authorize_target_definition(
        self,
        principal: McpPrincipal,
        definition: McpToolDefinition,
        target_id: UUID,
    ) -> McpToolDefinition:
        with self._session_factory() as session:
            client = session.get(McpClientRow, str(principal.client_id))
            if client is None or not client.enabled:
                raise McpAuthorizationError("permission_denied")
            grant = session.get(
                McpClientTargetGrantRow,
                {"client_id": str(principal.client_id), "target_id": str(target_id)},
            )
            instance = session.get(ManagedInstanceRow, str(target_id))
            assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
            if grant is None or instance is None or assignment is None:
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            client_permissions = self._permissions(session, client.preset_id)
            target_permissions = self._permissions(session, assignment.preset_id)
            if not definition.permissions.issubset(
                client_permissions.intersection(target_permissions)
            ):
                raise McpAuthorizationError("permission_denied")
        technical = self._targets.resolve(target_id)
        if technical.target_class not in definition.allowed_target_classes:
            raise McpAuthorizationError("permission_denied")
        if (
            technical.target_class is TargetClass.EXTERNAL_PRODUCTION
            and not definition.production_eligible
        ):
            raise McpAuthorizationError("permission_denied")
        if not definition.required_capabilities.issubset(technical.capabilities):
            raise McpAuthorizationError("capability_unavailable")
        if not definition.required_states.issubset(technical.current_states):
            missing = definition.required_states.difference(technical.current_states)
            if "running" in missing:
                raise McpAuthorizationError("target_not_running")
            if "ready" in missing:
                raise McpAuthorizationError("target_not_ready")
            if "reconciled" in missing:
                raise McpAuthorizationError("target_reconciliation_required")
            raise McpAuthorizationError("permission_denied")
        return definition

    def invoke_unscoped(
        self,
        principal: McpPrincipal,
        tool_name: str,
        adapter: Callable[[], _ResultT],
    ) -> _ResultT:
        """Authorize from current durable state before entering an adapter."""

        self.authorize_unscoped(principal, tool_name)
        return adapter()

    def invoke_target(
        self,
        principal: McpPrincipal,
        tool_name: str,
        target_id: UUID,
        adapter: Callable[[], _ResultT],
    ) -> _ResultT:
        """Apply the complete target intersection before adapter execution."""

        self.authorize_target(principal, tool_name, target_id)
        return adapter()

    def granted_target_ids(self, principal: McpPrincipal) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            values = session.scalars(
                select(McpClientTargetGrantRow.target_id)
                .where(McpClientTargetGrantRow.client_id == str(principal.client_id))
                .order_by(McpClientTargetGrantRow.target_id)
                .limit(1001)
            ).all()
        if len(values) > 1000:
            raise McpAuthorizationError("response_too_large")
        return tuple(UUID(item) for item in values)

    def _definition(self, tool_name: str) -> McpToolDefinition:
        definition = self._registry.get(tool_name)
        if definition is None:
            raise McpAuthorizationError("permission_denied")
        return definition

    def _client_permissions(self, client_id: UUID) -> frozenset[str]:
        with self._session_factory() as session:
            client = session.get(McpClientRow, str(client_id))
            if client is None or not client.enabled:
                return frozenset()
            return self._permissions(session, client.preset_id)

    @staticmethod
    def _permissions(session: Session, preset_id: str) -> frozenset[str]:
        return frozenset(
            session.scalars(
                select(McpPresetPermissionRow.permission).where(
                    McpPresetPermissionRow.preset_id == preset_id
                )
            ).all()
        )

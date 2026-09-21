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
from kodi_lab.domain import (
    AgentWorkspace,
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    DevelopmentSource,
    DevelopmentSourceKind,
    InstanceType,
    TargetType,
)
from kodi_lab.kodi.remote import REMOTE_RUNTIME_REVISION_IDS
from kodi_lab.mcp.registry import (
    McpToolDefinition,
    McpToolRegistry,
    TargetClass,
    ToolScope,
)
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpClientTargetGrantRow,
    McpPresetPermissionRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
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
        capabilities = {"managed_runtime_status", "target_inspect", "addon_inventory"}
        states: set[str] = set()
        instance = None
        if self._session_factory is not None:
            with self._session_factory() as session:
                instance = session.get(ManagedInstanceRow, str(target_id))
            if (
                instance is not None
                and instance.instance_type is InstanceType.DISPOSABLE
            ):
                capabilities.update({"workspace_deployment", "workspace_execution"})
        if instance is not None and self._screenshots is not None:
            if instance is not None and self._screenshots.supports_runtime_revision(
                UUID(instance.runtime_revision_id)
            ):
                capabilities.add("managed_screenshot")
                capabilities.add("screenshot")
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
        if definition.resolved_scope is not ToolScope.GLOBAL:
            raise McpAuthorizationError("permission_denied")
        permissions = self._client_permissions(principal.client_id)
        if not definition.permissions.issubset(permissions):
            raise McpAuthorizationError("permission_denied")
        return definition

    def authorize_target(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> McpToolDefinition:
        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.TARGET:
            raise McpAuthorizationError("permission_denied")
        return self._authorize_target_definition(principal, definition, target_id)

    def authorize_target_eligibility(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> McpToolDefinition:
        """Apply target policy when an unscoped listing projects candidates."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.GLOBAL:
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
            target = session.get(TargetRow, str(target_id))
            assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
            if grant is None or target is None or assignment is None:
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            self._enforce_target_class_veto(target)
            client_permissions = self._permissions(session, client.preset_id)
            target_permissions = self._permissions(session, assignment.preset_id)
            if not definition.permissions.issubset(
                client_permissions.intersection(target_permissions)
            ):
                raise McpAuthorizationError("permission_denied")
        technical = self._technical(target_id)
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

    def granted_development_sources(
        self,
        principal: McpPrincipal,
        tool_name: str,
        *,
        cursor: UUID | None,
        limit: int,
    ) -> tuple[tuple[DevelopmentSource, ...], bool]:
        """Resolve one bounded page through current client permission and grants."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.DEVELOPMENT_SOURCE:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            self._require_client_permission(session, principal, definition)
            statement = (
                select(McpClientDevelopmentSourceGrantRow.development_source_id)
                .where(
                    McpClientDevelopmentSourceGrantRow.client_id
                    == str(principal.client_id)
                )
                .order_by(McpClientDevelopmentSourceGrantRow.development_source_id)
                .limit(limit + 1)
            )
            if cursor is not None:
                statement = statement.where(
                    McpClientDevelopmentSourceGrantRow.development_source_id
                    > str(cursor)
                )
            ids = session.scalars(statement).all()
            repository = DevelopmentSourceRepository(session)
            sources = tuple(repository.get(UUID(value)) for value in ids[:limit])
        if any(source is None for source in sources):
            raise McpAuthorizationError("source_not_found_or_not_authorized")
        return tuple(source for source in sources if source is not None), len(
            ids
        ) > limit

    def authorize_development_source(
        self,
        principal: McpPrincipal,
        tool_name: str,
        source_id: UUID,
    ) -> DevelopmentSource:
        """Resolve current client permission and one exact source grant."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.DEVELOPMENT_SOURCE:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            self._require_client_permission(session, principal, definition)
            grant = session.get(
                McpClientDevelopmentSourceGrantRow,
                {
                    "client_id": str(principal.client_id),
                    "development_source_id": str(source_id),
                },
            )
            source = DevelopmentSourceRepository(session).get(source_id)
        if grant is None or source is None:
            raise McpAuthorizationError("source_not_found_or_not_authorized")
        if source.source_kind is not DevelopmentSourceKind.LOCAL_DIRECTORY:
            raise McpAuthorizationError("source_not_found_or_not_authorized")
        return source

    def owned_workspaces(
        self,
        principal: McpPrincipal,
        tool_name: str,
        *,
        cursor: UUID | None,
        limit: int,
    ) -> tuple[tuple[AgentWorkspace, ...], bool]:
        """List only exact bindings whose source grant remains current."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.WORKSPACE:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            self._require_client_permission(session, principal, definition)
            statement = (
                select(McpAgentWorkspaceBindingRow.workspace_id)
                .join(
                    McpClientDevelopmentSourceGrantRow,
                    (
                        McpClientDevelopmentSourceGrantRow.client_id
                        == McpAgentWorkspaceBindingRow.client_id
                    )
                    & (
                        McpClientDevelopmentSourceGrantRow.development_source_id
                        == McpAgentWorkspaceBindingRow.development_source_id
                    ),
                )
                .where(
                    McpAgentWorkspaceBindingRow.client_id == str(principal.client_id)
                )
                .order_by(McpAgentWorkspaceBindingRow.workspace_id)
                .limit(limit + 1)
            )
            if cursor is not None:
                statement = statement.where(
                    McpAgentWorkspaceBindingRow.workspace_id > str(cursor)
                )
            ids = session.scalars(statement).all()
            repository = AgentWorkspaceRepository(session)
            workspaces = tuple(repository.get(UUID(value)) for value in ids[:limit])
        if any(workspace is None for workspace in workspaces):
            raise McpAuthorizationError("workspace_not_found_or_not_authorized")
        return (
            tuple(workspace for workspace in workspaces if workspace is not None),
            len(ids) > limit,
        )

    def authorize_workspace(
        self,
        principal: McpPrincipal,
        tool_name: str,
        workspace_id: UUID,
    ) -> AgentWorkspace:
        """Resolve the permission/grant/binding/source/state intersection once."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.WORKSPACE:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            self._require_client_permission(session, principal, definition)
            binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
            if binding is None or binding.client_id != str(principal.client_id):
                raise McpAuthorizationError("workspace_not_found_or_not_authorized")
            grant = session.get(
                McpClientDevelopmentSourceGrantRow,
                {
                    "client_id": str(principal.client_id),
                    "development_source_id": binding.development_source_id,
                },
            )
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            if (
                grant is None
                or workspace is None
                or str(workspace.development_source_id) != binding.development_source_id
            ):
                raise McpAuthorizationError("workspace_not_found_or_not_authorized")
        if "present" in definition.required_states and (
            workspace.bytes_state is AgentWorkspaceBytesState.CLEANED
        ):
            raise McpAuthorizationError("workspace_bytes_cleaned")
        if "ready" in definition.required_states and (
            workspace.state is not AgentWorkspaceState.READY
        ):
            raise McpAuthorizationError("workspace_not_ready")
        return workspace

    def authorize_workspace_target(
        self,
        principal: McpPrincipal,
        tool_name: str,
        workspace_id: UUID,
        target_id: UUID,
    ) -> AgentWorkspace:
        """Intersect exact workspace ownership/source authority with target policy."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.WORKSPACE_TARGET:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            client = self._require_client_permission(session, principal, definition)
            binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
            source_grant = (
                None
                if binding is None
                else session.get(
                    McpClientDevelopmentSourceGrantRow,
                    {
                        "client_id": str(principal.client_id),
                        "development_source_id": binding.development_source_id,
                    },
                )
            )
            workspace = AgentWorkspaceRepository(session).get(workspace_id)
            target_grant = session.get(
                McpClientTargetGrantRow,
                {"client_id": str(principal.client_id), "target_id": str(target_id)},
            )
            target = session.get(TargetRow, str(target_id))
            assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
            target_permissions = (
                frozenset()
                if assignment is None
                else self._permissions(session, assignment.preset_id)
            )
            if (
                binding is None
                or binding.client_id != str(principal.client_id)
                or source_grant is None
                or workspace is None
                or str(workspace.development_source_id) != binding.development_source_id
            ):
                raise McpAuthorizationError("workspace_not_found_or_not_authorized")
            if target_grant is None or target is None or assignment is None:
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            self._enforce_target_class_veto(target, tool_name=tool_name)
            client_permissions = self._permissions(session, client.preset_id)
            if not definition.permissions.issubset(
                client_permissions.intersection(target_permissions)
            ):
                raise McpAuthorizationError(
                    "promotion_not_allowed"
                    if tool_name == "promotion_request"
                    else "deployment_not_allowed"
                )
        technical = self._technical(target_id)
        if technical.target_class not in definition.allowed_target_classes:
            raise McpAuthorizationError(
                "promotion_not_allowed"
                if tool_name == "promotion_request"
                else "deployment_not_allowed"
            )
        if not definition.required_capabilities.issubset(technical.capabilities):
            raise McpAuthorizationError(
                "promotion_not_allowed"
                if tool_name == "promotion_request"
                else "deployment_not_allowed"
            )
        if not definition.required_states.issubset(technical.current_states):
            missing = definition.required_states.difference(technical.current_states)
            if "running" in missing:
                raise McpAuthorizationError("target_not_running")
            if "ready" in missing:
                raise McpAuthorizationError("target_not_ready")
            raise McpAuthorizationError("target_reconciliation_required")
        if workspace.bytes_state is AgentWorkspaceBytesState.CLEANED:
            raise McpAuthorizationError("workspace_bytes_cleaned")
        if workspace.state is not AgentWorkspaceState.READY:
            raise McpAuthorizationError("workspace_not_ready")
        return workspace

    @staticmethod
    def _enforce_target_class_veto(
        target: TargetRow, *, tool_name: str | None = None
    ) -> None:
        """One fail-closed Slice-1 veto shared by target and workspace paths."""

        if target.target_type not in {
            TargetType.EXTERNAL_PRODUCTION,
            TargetType.EXTERNAL_PRODUCTION.value,
        }:
            return
        if tool_name == "promotion_request":
            raise McpAuthorizationError("promotion_not_allowed")
        if tool_name in {"workspace_deploy", "workspace_execute"}:
            raise McpAuthorizationError("deployment_not_allowed")
        raise McpAuthorizationError("permission_denied")

    def _require_client_permission(
        self,
        session: Session,
        principal: McpPrincipal,
        definition: McpToolDefinition,
    ) -> McpClientRow:
        client = session.get(McpClientRow, str(principal.client_id))
        if client is None or not client.enabled:
            raise McpAuthorizationError("permission_denied")
        permissions = self._permissions(session, client.preset_id)
        if not definition.permissions.issubset(permissions):
            raise McpAuthorizationError("permission_denied")
        if (
            TargetClass.INTERNAL_LAB not in definition.allowed_target_classes
            or definition.production_eligible
        ):
            raise McpAuthorizationError("permission_denied")
        return client

    def _technical(self, target_id: UUID) -> TargetTechnicalAuthority:
        try:
            return self._targets.resolve(target_id)
        except McpAuthorizationError:
            raise
        except Exception as error:
            code = getattr(error, "code", "target_not_found_or_not_authorized")
            if code not in {
                "authentication_failed",
                "capability_unavailable",
                "identity_mismatch",
                "needs_reverification",
                "permission_denied",
                "target_not_found_or_not_authorized",
                "target_unreachable",
            }:
                code = "target_not_found_or_not_authorized"
            raise McpAuthorizationError(code) from error

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

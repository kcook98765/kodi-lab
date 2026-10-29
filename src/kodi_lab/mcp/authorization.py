"""One common durable authority intersection for every MCP tool call."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypeVar, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    PRODUCTION_REMOTE_KODI_PRESET_ID,
    McpPrincipal,
)
from kodi_lab.application.production_inspection import (
    PRODUCTION_INSPECTION_PRESET_ID,
    PRODUCTION_INSPECTION_PRESET_NAME,
    PRODUCTION_VISUAL_PRESET_ID,
    PRODUCTION_VISUAL_PRESET_NAME,
    ProductionInspectionError,
    ProductionReadRateLimiter,
    evaluate_production_bridge_anchor_state,
    evaluate_production_inspection_state,
    evaluate_production_visual_state,
)
from kodi_lab.application.production_navigation import (
    ProductionActionClass,
    ProductionNavigationAuthority,
    classify_production_action,
    current_native_control_acknowledgement,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.application.screenshot_preferences import (
    ScreenshotPreferenceError,
    require_screenshot_preference,
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
from kodi_lab.domain.smoke_diagnostics import AddonExceptionDiagnostic
from kodi_lab.kodi.addons import KodiJsonRpcDiagnostic
from kodi_lab.kodi.remote import REMOTE_RUNTIME_REVISION_IDS, BrowserRemoteAction
from kodi_lab.mcp.registry import (
    McpToolDefinition,
    McpToolRegistry,
    TargetClass,
    ToolEffect,
    ToolRateClass,
    ToolScope,
)
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.screenshot_diagnostics import (
    bind_screenshot_bridge_revision,
    emit_screenshot_stage,
)
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
    McpPresetPermissionRow,
    McpPresetRow,
    McpProductionTargetScopeRow,
    McpTargetPresetAssignmentRow,
    ProductionMutationControlRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)

_ResultT = TypeVar("_ResultT")


class McpAuthorizationError(PermissionError):
    def __init__(
        self,
        code: str,
        *,
        reason: str | None = None,
        diagnostic: KodiJsonRpcDiagnostic | None = None,
        addon_exception: AddonExceptionDiagnostic | None = None,
        capture_conflict_observed_generation: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.diagnostic = diagnostic
        self.addon_exception = addon_exception
        self.capture_conflict_observed_generation = capture_conflict_observed_generation


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

    def capabilities(self, target_id: UUID) -> frozenset[str]:
        capabilities = {"managed_runtime_status", "target_inspect", "addon_inventory"}
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
            revision_id = UUID(instance.runtime_revision_id)
            if revision_id in REMOTE_RUNTIME_REVISION_IDS:
                capabilities.add("semantic_remote")
            elif self._session_factory is not None:
                with self._session_factory() as session:
                    if supports_reviewed_recipe_capability(
                        session, revision_id, "navigation"
                    ):
                        capabilities.add("semantic_remote")
        return frozenset(capabilities)

    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
        return self._resolve(target_id, observe_runtime=True)

    def resolve_ui_state(self, target_id: UUID) -> TargetTechnicalAuthority:
        # UI observation requires reconciled authority here. Its managed
        # collector checks fresh health/readiness/identity inside the target gate.
        return self._resolve(target_id, observe_runtime=False)

    def _resolve(
        self, target_id: UUID, *, observe_runtime: bool
    ) -> TargetTechnicalAuthority:
        capabilities = self.capabilities(target_id)
        states: set[str] = set()
        if observe_runtime and self._status is not None:
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
            capabilities=capabilities,
            current_states=frozenset(states),
        )


class McpAuthorizationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: McpToolRegistry,
        *,
        targets: TargetAuthorityProvider | None = None,
        production_rates: ProductionReadRateLimiter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._targets = targets or InternalManagedTargetAuthorityProvider()
        self._production_rates = production_rates or ProductionReadRateLimiter()
        self._clock = clock

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
        if tool_name == "target_list" and principal.preset_id in {
            PRODUCTION_INSPECTION_PRESET_ID,
            PRODUCTION_REMOTE_KODI_PRESET_ID,
            PRODUCTION_VISUAL_PRESET_ID,
        }:
            with self._session_factory() as session:
                client = session.get(McpClientRow, str(principal.client_id))
                if (
                    client is None
                    or not client.enabled
                    or client.preset_id
                    not in {
                        str(PRODUCTION_INSPECTION_PRESET_ID),
                        str(PRODUCTION_REMOTE_KODI_PRESET_ID),
                        str(PRODUCTION_VISUAL_PRESET_ID),
                    }
                ):
                    raise McpAuthorizationError("permission_denied")
                self._require_current_credential(session, principal, client)
        return definition

    def authorize_target(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> McpToolDefinition:
        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.TARGET:
            raise McpAuthorizationError("permission_denied")
        return self._authorize_target_definition(principal, definition, target_id)

    def authorize_managed_observation_error_detail(
        self, principal: McpPrincipal, tool_name: str, target_id: UUID
    ) -> None:
        """Recheck durable scope before publishing managed Bridge failure facts.

        This read grants no successful observation or mutation authority and
        performs no additional native probe after the failed observation.
        """
        definition = self._definition(tool_name)
        if tool_name not in {"inspect_ui_state", "log_read"}:
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            client = session.get(McpClientRow, str(principal.client_id))
            if client is None or not client.enabled:
                raise McpAuthorizationError("permission_denied")
            if not definition.permissions.issubset(
                self._permissions(session, client.preset_id)
            ):
                raise McpAuthorizationError("permission_denied")
            self._require_current_credential(session, principal, client)
            if client.preset_id in {
                str(PRODUCTION_INSPECTION_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
                str(PRODUCTION_VISUAL_PRESET_ID),
            }:
                raise McpAuthorizationError("permission_denied")
            target = session.get(TargetRow, str(target_id))
            assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or session.get(ManagedInstanceRow, str(target_id)) is None
                or assignment is None
                or session.get(
                    McpClientTargetGrantRow,
                    (str(principal.client_id), str(target_id)),
                )
                is None
            ):
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            if not definition.permissions.issubset(
                self._permissions(session, assignment.preset_id)
            ):
                raise McpAuthorizationError("permission_denied")

    def recheck_workspace_launch(
        self,
        session: Session,
        principal: McpPrincipal,
        workspace_id: UUID,
        target_id: UUID,
        *,
        tool_name: str = "workspace_launch",
    ) -> None:
        definition = self._definition(tool_name)
        client = self._require_client_permission(session, principal, definition)
        self._require_current_credential(session, principal, client)
        binding = session.get(McpAgentWorkspaceBindingRow, str(workspace_id))
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        if (
            binding is None
            or binding.client_id != str(principal.client_id)
            or workspace is None
            or str(workspace.development_source_id) != binding.development_source_id
            or session.get(
                McpClientDevelopmentSourceGrantRow,
                (str(principal.client_id), binding.development_source_id),
            )
            is None
        ):
            raise McpAuthorizationError("workspace_not_found_or_not_authorized")
        target = session.get(TargetRow, str(target_id))
        assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or session.get(ManagedInstanceRow, str(target_id)) is None
            or assignment is None
            or session.get(
                McpClientTargetGrantRow, (str(principal.client_id), str(target_id))
            )
            is None
        ):
            raise McpAuthorizationError("target_not_found_or_not_authorized")
        if not definition.permissions.issubset(
            self._permissions(session, assignment.preset_id)
        ):
            raise McpAuthorizationError("permission_denied")

    def recheck_managed_lifecycle(
        self,
        session: Session,
        principal: McpPrincipal,
        tool_name: str,
        target_id: UUID,
    ) -> None:
        """Recheck current credentials and exact policy in the mutation transaction.

        Technical running/readiness checks remain in the managed lifecycle. This
        recheck performs no network calls or runtime projection writes.
        """
        definition = self._definition(tool_name)
        if (
            tool_name not in {"managed_ensure_ready", "managed_release"}
            or definition.permissions != frozenset({"managed.lifecycle"})
            or definition.allowed_target_classes
            != frozenset({TargetClass.INTERNAL_LAB})
        ):
            raise McpAuthorizationError("permission_denied")
        client = self._require_client_permission(session, principal, definition)
        self._require_current_credential(session, principal, client)
        target = session.get(TargetRow, str(target_id))
        instance = session.get(ManagedInstanceRow, str(target_id))
        assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
        grant = session.get(
            McpClientTargetGrantRow, (str(principal.client_id), str(target_id))
        )
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or instance is None
            or assignment is None
            or grant is None
        ):
            raise McpAuthorizationError("target_not_found_or_not_authorized")
        if not definition.permissions.issubset(
            self._permissions(session, assignment.preset_id)
        ):
            raise McpAuthorizationError("permission_denied")

    def authorize_production_navigation(
        self,
        principal: McpPrincipal,
        target_id: UUID,
        action: BrowserRemoteAction,
    ) -> ProductionNavigationAuthority:
        """Authorize one new production dispatch through the action-aware policy."""

        if classify_production_action(action) is not ProductionActionClass.NAVIGATION:
            raise McpAuthorizationError("production_action_prohibited")
        definition = self._definition("remote_action")
        if (
            not definition.production_eligible
            or definition.effect.value != "mutating"
            or TargetClass.EXTERNAL_PRODUCTION not in definition.allowed_target_classes
            or definition.permissions != frozenset({"target.control"})
            or definition.required_capabilities != frozenset({"semantic_remote"})
        ):
            raise McpAuthorizationError("permission_denied")
        with self._session_factory() as session:
            client = session.get(McpClientRow, str(principal.client_id))
            grant = session.get(
                McpClientTargetGrantRow,
                {"client_id": str(principal.client_id), "target_id": str(target_id)},
            )
            target = session.get(TargetRow, str(target_id))
            assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
            if (
                client is None
                or not client.enabled
                or client.id != str(principal.client_id)
                or grant is None
                or target is None
                or target.target_type is not TargetType.EXTERNAL_PRODUCTION
                or assignment is None
            ):
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            remote_preset = session.get(
                McpPresetRow, str(PRODUCTION_REMOTE_KODI_PRESET_ID)
            )
            expected_permissions = frozenset(
                {"target.inspect", "target.visual", "target.control"}
            )
            if (
                principal.preset_id != PRODUCTION_REMOTE_KODI_PRESET_ID
                or client.preset_id != str(PRODUCTION_REMOTE_KODI_PRESET_ID)
                or assignment.preset_id != str(PRODUCTION_REMOTE_KODI_PRESET_ID)
                or remote_preset is None
                or not remote_preset.builtin
                or not remote_preset.assignable
                or remote_preset.display_name != "Production Remote Kodi"
                or self._permissions(session, remote_preset.id) != expected_permissions
                or not definition.permissions.issubset(expected_permissions)
            ):
                raise McpAuthorizationError("permission_denied")
            self._require_current_credential(session, principal, client)
            try:
                native = evaluate_production_inspection_state(
                    session,
                    target_id,
                    required_capabilities=frozenset(
                        {"target_inspect", "semantic_remote"}
                    ),
                )
                bridge = evaluate_production_bridge_anchor_state(session, target_id)
            except ProductionInspectionError as error:
                raise McpAuthorizationError(error.code) from error
            inspect = session.scalar(
                select(McpProductionTargetScopeRow).where(
                    McpProductionTargetScopeRow.client_id == client.id,
                    McpProductionTargetScopeRow.target_id == target.id,
                    McpProductionTargetScopeRow.identity_generation
                    == native.identity_generation,
                    McpProductionTargetScopeRow.scope == "inspect",
                    McpProductionTargetScopeRow.state == "active",
                )
            )
            navigate = session.scalar(
                select(McpProductionTargetScopeRow).where(
                    McpProductionTargetScopeRow.client_id == client.id,
                    McpProductionTargetScopeRow.target_id == target.id,
                    McpProductionTargetScopeRow.identity_generation
                    == native.identity_generation,
                    McpProductionTargetScopeRow.bridge_trust_revision
                    == bridge.bridge_trust_revision,
                    McpProductionTargetScopeRow.scope == "navigate",
                    McpProductionTargetScopeRow.state == "active",
                )
            )
            if inspect is None or navigate is None:
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            policy = session.get(ExternalProductionPolicyRow, target.id)
            mutation = session.get(ProductionMutationControlRow, 1)
            if policy is None or mutation is None:
                raise McpAuthorizationError("production_policy_not_confirmed")
            if mutation.production_mutations_blocked:
                raise McpAuthorizationError("production_mutations_blocked")
            if policy.mutations_suspended:
                raise McpAuthorizationError("production_target_mutations_suspended")
            try:
                acknowledgement = current_native_control_acknowledgement(
                    session, target_id
                )
            except ProductionInspectionError as error:
                raise McpAuthorizationError(error.code) from error
            external = session.get(ExternalKodiTargetRow, target.id)
            if external is None:
                raise McpAuthorizationError("needs_reverification")
            if (
                external.protocol_kind.value == "jsonrpc_http"
                and acknowledgement is None
            ):
                raise McpAuthorizationError("transport_acknowledgement_required")
            return ProductionNavigationAuthority(
                native_identity_generation=native.identity_generation,
                bridge_trust_revision=bridge.bridge_trust_revision,
                target_policy_revision=policy.policy_revision,
                mutation_control_epoch=mutation.policy_epoch,
                inspect_scope_id=UUID(inspect.id),
                navigate_scope_id=UUID(navigate.id),
                client_preset_id=PRODUCTION_REMOTE_KODI_PRESET_ID,
                target_preset_id=UUID(assignment.preset_id),
                generic_grant_created_at=grant.created_at,
                generic_grant_id=UUID(grant.grant_id),
                native_http_acknowledgement_id=(
                    None if acknowledgement is None else UUID(acknowledgement.id)
                ),
            )

    def recheck_native_zip_installation(
        self,
        session: Session,
        principal: McpPrincipal,
        target_id: UUID,
        *,
        require_running: bool = True,
        runtime_revision_id: str | None = None,
    ) -> None:
        """Pure database admission for serialized publication/completion."""
        client = session.get(McpClientRow, str(principal.client_id))
        target = session.get(TargetRow, str(target_id))
        instance = session.get(ManagedInstanceRow, str(target_id))
        grant = session.get(
            McpClientTargetGrantRow,
            {"client_id": str(principal.client_id), "target_id": str(target_id)},
        )
        assignment = session.get(McpTargetPresetAssignmentRow, str(target_id))
        if client is None or not client.enabled:
            raise McpAuthorizationError("permission_denied")
        self._require_current_credential(session, principal, client)
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or grant is None
            or assignment is None
        ):
            raise McpAuthorizationError("target_not_found_or_not_authorized")
        if "addon.install" not in self._permissions(
            session, client.preset_id
        ).intersection(self._permissions(session, assignment.preset_id)):
            raise McpAuthorizationError("permission_denied")
        if require_running and instance.runtime_state.value != "running":
            raise McpAuthorizationError("target_not_running")
        if (
            runtime_revision_id is not None
            and instance.runtime_revision_id != runtime_revision_id
        ):
            raise McpAuthorizationError("target_not_found_or_not_authorized")

    def authorize_native_zip_installation(
        self, principal: McpPrincipal, target_id: UUID, *, require_running: bool = True
    ) -> None:
        """HTTP native-delivery API authority; not an advertised MCP tool.

        This opt-in permission is deliberately absent from every built-in preset.
        The common target grant/preset/capability checks still apply.
        """
        with self._session_factory() as session:
            self.recheck_native_zip_installation(
                session, principal, target_id, require_running=require_running
            )
        self._authorize_target_definition(
            principal,
            McpToolDefinition(
                name="native_zip_installation",
                schema_revision=1,
                permissions=frozenset({"addon.install"}),
                effect=ToolEffect.MUTATING,
                allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
                required_capabilities=frozenset({"addon_inventory"}),
                required_states=frozenset({"running", "ready", "reconciled"})
                if require_running
                else frozenset(),
                rate_class=ToolRateClass.DEVELOPMENT_DEPLOY,
                adapter="native_zip.operator_install",
                production_eligible=False,
                target_scope_required=True,
                scope=ToolScope.TARGET,
            ),
            target_id,
        )

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
            if target.target_type is TargetType.EXTERNAL_PRODUCTION:
                self._evaluate_production_inspection(
                    session,
                    principal=principal,
                    definition=definition,
                    client=client,
                    target=target,
                    assignment=assignment,
                )
                if definition.name == "screenshot_capture":
                    self._screenshot_preference_revision(target_id)
                return definition
            if client.preset_id in {
                str(PRODUCTION_INSPECTION_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
                str(PRODUCTION_VISUAL_PRESET_ID),
            }:
                raise McpAuthorizationError("permission_denied")
            client_permissions = self._permissions(session, client.preset_id)
            target_permissions = self._permissions(session, assignment.preset_id)
            if not definition.permissions.issubset(
                client_permissions.intersection(target_permissions)
            ):
                raise McpAuthorizationError("permission_denied")
        technical = self._technical(target_id, tool_name=definition.name)
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
        if definition.name == "screenshot_capture":
            self._screenshot_preference_revision(target_id)
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

    def authorize_workspace_target_scope(
        self,
        principal: McpPrincipal,
        tool_name: str,
        workspace_id: UUID,
        target_id: UUID,
        *,
        read_session: Session | None = None,
    ) -> AgentWorkspace:
        """Intersect exact workspace ownership/source authority with target policy."""

        definition = self._definition(tool_name)
        if definition.resolved_scope is not ToolScope.WORKSPACE_TARGET:
            raise McpAuthorizationError("permission_denied")
        with (
            self._session_factory()
            if read_session is None
            else nullcontext(read_session)
        ) as session:
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
            self._enforce_workspace_production_veto(target, tool_name=tool_name)
            client_permissions = self._permissions(session, client.preset_id)
            if not definition.permissions.issubset(
                client_permissions.intersection(target_permissions)
            ):
                raise McpAuthorizationError(
                    "promotion_not_allowed"
                    if tool_name == "promotion_request"
                    else "deployment_not_allowed"
                )
        return workspace

    def recheck_retained_selection(
        self,
        session: Session,
        principal: McpPrincipal,
        tool_name: str,
        workspace_id: UUID,
        target_id: UUID,
    ) -> None:
        definition = self._definition(tool_name)
        if tool_name not in {
            "managed_selection_preview",
            "managed_select",
        } or definition.permissions != frozenset(
            {"managed.lifecycle", "workspace.inspect"}
        ):
            raise McpAuthorizationError("permission_denied")
        try:
            self.authorize_workspace_target_scope(
                principal, tool_name, workspace_id, target_id, read_session=session
            )
        except McpAuthorizationError as error:
            if error.code == "deployment_not_allowed":
                raise McpAuthorizationError("permission_denied") from error
            raise
        client = self._require_client_permission(session, principal, definition)
        self._require_current_credential(session, principal, client)
        target = session.get(TargetRow, str(target_id))
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or session.get(ManagedInstanceRow, str(target_id)) is None
        ):
            raise McpAuthorizationError("target_not_found_or_not_authorized")

    def authorize_workspace_target(
        self,
        principal: McpPrincipal,
        tool_name: str,
        workspace_id: UUID,
        target_id: UUID,
    ) -> AgentWorkspace:
        """Apply durable scope, then existing technical and mutation admission."""

        workspace = self.authorize_workspace_target_scope(
            principal, tool_name, workspace_id, target_id
        )
        definition = self._definition(tool_name)
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
    def _enforce_workspace_production_veto(
        target: TargetRow, *, tool_name: str | None = None
    ) -> None:
        """Workspace-target paths remain absolutely production-ineligible."""

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

    def _evaluate_production_inspection(
        self,
        session: Session,
        *,
        principal: McpPrincipal,
        definition: McpToolDefinition,
        client: McpClientRow,
        target: TargetRow,
        assignment: McpTargetPresetAssignmentRow,
    ) -> None:
        """One complete durable evaluator for every production read phase."""

        if (
            not definition.production_eligible
            or TargetClass.EXTERNAL_PRODUCTION not in definition.allowed_target_classes
            or definition.name
            not in {
                "target_list",
                "target_status",
                "addon_list",
                "inspect_ui_state",
                "log_read",
                "screenshot_capture",
            }
        ):
            raise McpAuthorizationError("permission_denied")
        visual = definition.name == "screenshot_capture"
        allowed_client_presets = (
            {
                str(PRODUCTION_VISUAL_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
            }
            if visual
            else {
                str(PRODUCTION_INSPECTION_PRESET_ID),
                str(PRODUCTION_VISUAL_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
            }
        )
        client_preset = (
            UUID(client.preset_id)
            if client.preset_id in allowed_client_presets
            else None
        )
        allowed_target_presets = (
            {
                str(PRODUCTION_VISUAL_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
            }
            if visual
            else {
                str(PRODUCTION_INSPECTION_PRESET_ID),
                str(PRODUCTION_VISUAL_PRESET_ID),
                str(PRODUCTION_REMOTE_KODI_PRESET_ID),
            }
        )
        if (
            client.id != str(principal.client_id)
            or not client.enabled
            or client_preset is None
            or client.preset_id != str(client_preset)
            or principal.preset_id != client_preset
            or assignment.preset_id not in allowed_target_presets
        ):
            raise McpAuthorizationError("permission_denied")
        preset = session.get(McpPresetRow, str(client_preset))
        expected_name, expected_permissions = self._production_preset_contract(
            client_preset
        )
        if (
            preset is None
            or not preset.builtin
            or not preset.assignable
            or preset.display_name != expected_name
            or self._permissions(session, preset.id) != expected_permissions
            or not definition.permissions.issubset(expected_permissions)
        ):
            if visual:
                emit_screenshot_stage(
                    "catalog_permission", "failed", "permission_denied"
                )
            raise McpAuthorizationError("permission_denied")
        if visual:
            emit_screenshot_stage(
                "catalog_permission", "passed", "screenshot_tool_permitted"
            )
            emit_screenshot_stage("target_grant", "passed", "target_grant_current")
        target_preset = session.get(McpPresetRow, assignment.preset_id)
        target_expected_name, target_expected_permissions = (
            self._production_preset_contract(UUID(assignment.preset_id))
        )
        if (
            target_preset is None
            or not target_preset.builtin
            or not target_preset.assignable
            or target_preset.display_name != target_expected_name
            or self._permissions(session, target_preset.id)
            != target_expected_permissions
        ):
            raise McpAuthorizationError("permission_denied")
        self._require_current_credential(session, principal, client)
        try:
            state = evaluate_production_inspection_state(
                session,
                UUID(target.id),
                required_capabilities=(
                    frozenset({"target_inspect"})
                    if visual
                    else definition.required_capabilities
                ),
            )
        except ProductionInspectionError as error:
            raise McpAuthorizationError(error.code) from error
        scope = session.scalar(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.client_id == client.id,
                McpProductionTargetScopeRow.target_id == target.id,
                McpProductionTargetScopeRow.identity_generation
                == state.identity_generation,
                McpProductionTargetScopeRow.scope == "inspect",
                McpProductionTargetScopeRow.state == "active",
            )
        )
        if scope is None:
            raise McpAuthorizationError("target_not_found_or_not_authorized")
        if visual:
            try:
                visual_state = evaluate_production_visual_state(
                    session, UUID(target.id)
                )
            except ProductionInspectionError as error:
                failed_stage = {
                    "capability_unavailable": "capability_intersection",
                    "transport_acknowledgement_required": "http_acknowledgement",
                }.get(error.code, "bridge_anchor")
                emit_screenshot_stage(failed_stage, "failed", error.code)
                raise McpAuthorizationError(error.code) from error
            bind_screenshot_bridge_revision(visual_state.bridge_trust_revision)
            emit_screenshot_stage("bridge_anchor", "passed", "bridge_revision_current")
            emit_screenshot_stage(
                "capability_intersection", "passed", "screenshot_capability_current"
            )
            emit_screenshot_stage(
                "http_acknowledgement", "passed", "http_acknowledgement_current"
            )
            visual_scope = session.scalar(
                select(McpProductionTargetScopeRow).where(
                    McpProductionTargetScopeRow.client_id == client.id,
                    McpProductionTargetScopeRow.target_id == target.id,
                    McpProductionTargetScopeRow.identity_generation
                    == visual_state.identity_generation,
                    McpProductionTargetScopeRow.bridge_trust_revision
                    == visual_state.bridge_trust_revision,
                    McpProductionTargetScopeRow.scope == "visual",
                    McpProductionTargetScopeRow.state == "active",
                )
            )
            if visual_scope is None:
                emit_screenshot_stage(
                    "visual_authority", "failed", "visual_scope_not_current"
                )
                raise McpAuthorizationError("target_not_found_or_not_authorized")
            emit_screenshot_stage("visual_authority", "passed", "visual_scope_current")

    def _require_current_credential(
        self, session: Session, principal: McpPrincipal, client: McpClientRow
    ) -> None:
        credential = session.get(McpCredentialRow, str(principal.credential_id))
        try:
            expires_at = (
                None
                if credential is None or credential.expires_at is None
                else datetime.fromisoformat(
                    credential.expires_at.replace("Z", "+00:00")
                )
            )
        except ValueError as error:
            raise McpAuthorizationError("permission_denied") from error
        if (
            credential is None
            or credential.client_id != client.id
            or credential.revoked_at is not None
            or (expires_at is not None and expires_at <= self._clock())
        ):
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

    def _technical(
        self, target_id: UUID, *, tool_name: str | None = None
    ) -> TargetTechnicalAuthority:
        try:
            resolver = getattr(self._targets, "resolve_ui_state", None)
            if tool_name == "inspect_ui_state" and callable(resolver):
                return cast(Callable[[UUID], TargetTechnicalAuthority], resolver)(
                    target_id
                )
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
        if tool_name == "target_list" and principal.preset_id in {
            PRODUCTION_INSPECTION_PRESET_ID,
            PRODUCTION_REMOTE_KODI_PRESET_ID,
            PRODUCTION_VISUAL_PRESET_ID,
        }:
            try:
                self._production_rates.require_target_list(principal.client_id)
            except ProductionInspectionError as error:
                raise McpAuthorizationError(error.code) from error
        return adapter()

    def invoke_target(
        self,
        principal: McpPrincipal,
        tool_name: str,
        target_id: UUID,
        adapter: Callable[[], _ResultT],
        *,
        post_authorization_failure: Callable[[], None] | None = None,
    ) -> _ResultT:
        """Apply the complete target intersection before adapter execution."""

        self.authorize_target(principal, tool_name, target_id)
        preference_revision = (
            self._screenshot_preference_revision(target_id)
            if tool_name == "screenshot_capture"
            else None
        )
        production = self._is_production_target(target_id)
        if production:
            try:
                self._production_rates.require_target_read(
                    tool_name, principal.client_id, target_id
                )
            except ProductionInspectionError as error:
                raise McpAuthorizationError(error.code) from error
        try:
            result = adapter()
        except McpAuthorizationError as error:
            if (
                production or tool_name == "log_read"
            ) and error.code != "identity_mismatch":
                # Even bounded failure classifications are not disclosed after
                # durable production authority changes during network work.
                self.authorize_target(principal, tool_name, target_id)
            raise
        except Exception:
            if production or tool_name == "log_read":
                self.authorize_target(principal, tool_name, target_id)
            raise
        if production or preference_revision is not None or tool_name == "log_read":
            # A result is not publishable unless the entire durable authority
            # intersection still holds after the fresh native read.
            try:
                self.authorize_target(principal, tool_name, target_id)
                if preference_revision is not None:
                    self._screenshot_preference_revision(
                        target_id, expected_revision=preference_revision
                    )
            except Exception:
                if post_authorization_failure is not None:
                    post_authorization_failure()
                raise
        return result

    def _screenshot_preference_revision(
        self, target_id: UUID, *, expected_revision: int | None = None
    ) -> int | None:
        with self._session_factory() as session:
            target = session.get(TargetRow, str(target_id))
            if target is None or target.target_type is TargetType.MANAGED:
                return None
            try:
                return require_screenshot_preference(
                    session, target_id, expected_revision=expected_revision
                )
            except ScreenshotPreferenceError as error:
                raise McpAuthorizationError(error.code) from error

    def _is_production_target(self, target_id: UUID) -> bool:
        with self._session_factory() as session:
            target = session.get(TargetRow, str(target_id))
            return bool(
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            )

    def is_production_target(self, target_id: UUID) -> bool:
        """Expose target class only for transport-neutral tool orchestration."""

        return self._is_production_target(target_id)

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
    def _production_preset_contract(preset_id: UUID) -> tuple[str, frozenset[str]]:
        if preset_id == PRODUCTION_INSPECTION_PRESET_ID:
            return PRODUCTION_INSPECTION_PRESET_NAME, frozenset({"target.inspect"})
        if preset_id == PRODUCTION_VISUAL_PRESET_ID:
            return PRODUCTION_VISUAL_PRESET_NAME, frozenset(
                {"target.inspect", "target.visual"}
            )
        if preset_id == PRODUCTION_REMOTE_KODI_PRESET_ID:
            return "Production Remote Kodi", frozenset(
                {"target.inspect", "target.visual", "target.control"}
            )
        raise McpAuthorizationError("permission_denied")

    @staticmethod
    def _permissions(session: Session, preset_id: str) -> frozenset[str]:
        return frozenset(
            session.scalars(
                select(McpPresetPermissionRow.permission).where(
                    McpPresetPermissionRow.preset_id == preset_id
                )
            ).all()
        )

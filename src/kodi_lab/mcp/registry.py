"""Static server-owned tool metadata and common authorization declarations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

KODI_LAB_MCP_SCHEMA_VERSION = 1


class ToolEffect(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"


class ToolRateClass(StrEnum):
    READ_LIGHT = "read_light"
    READ_BOUNDED = "read_bounded"
    VISUAL_CAPTURE = "visual_capture"
    REMOTE_CONTROL = "remote_control"
    DEVELOPMENT_READ = "development_read"
    DEVELOPMENT_CREATE = "development_create"
    DEVELOPMENT_EDIT = "development_edit"
    DEVELOPMENT_VALIDATE = "development_validate"
    DEVELOPMENT_DEPLOY = "development_deploy"
    DEVELOPMENT_EXECUTE = "development_execute"
    DEVELOPMENT_PROMOTION_REQUEST = "development_promotion_request"
    MANAGED_LIFECYCLE = "managed_lifecycle"


class ToolScope(StrEnum):
    GLOBAL = "global"
    TARGET = "target"
    DEVELOPMENT_SOURCE = "development_source"
    WORKSPACE = "workspace"
    WORKSPACE_TARGET = "workspace_target"


class TargetClass(StrEnum):
    INTERNAL_LAB = "internal_lab"
    EXTERNAL_DEVELOPMENT = "external_development"
    EXTERNAL_PRODUCTION = "external_production"


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    name: str
    schema_revision: int
    permissions: frozenset[str]
    effect: ToolEffect
    allowed_target_classes: frozenset[TargetClass]
    required_capabilities: frozenset[str]
    required_states: frozenset[str]
    rate_class: ToolRateClass
    adapter: str
    production_eligible: bool
    target_scope_required: bool
    scope: ToolScope = ToolScope.GLOBAL

    @property
    def resolved_scope(self) -> ToolScope:
        # Retain construction compatibility for the Phase 11 boolean while all
        # new definitions declare their richer scope explicitly.
        if self.target_scope_required:
            return ToolScope.TARGET
        return self.scope


class McpRegistryError(ValueError):
    pass


class McpToolRegistry:
    """Transport-neutral closed production inventory with extensible metadata."""

    def __init__(self, *, known_permissions: frozenset[str]) -> None:
        self._known_permissions = known_permissions
        self._tools: dict[str, McpToolDefinition] = {}

    def register(self, definition: McpToolDefinition) -> None:
        if (
            not definition.name
            or len(definition.name) > 64
            or definition.name != definition.name.lower()
            or definition.schema_revision < 1
            or not definition.permissions
            or not definition.permissions.issubset(self._known_permissions)
            or not definition.allowed_target_classes
            or not definition.adapter
            or definition.name in self._tools
            or (
                definition.scope is ToolScope.TARGET
                and not definition.target_scope_required
            )
            or (
                definition.target_scope_required
                and definition.scope not in {ToolScope.GLOBAL, ToolScope.TARGET}
            )
        ):
            raise McpRegistryError("invalid_or_unknown_tool_metadata")
        self._tools[definition.name] = definition

    def get(self, name: str) -> McpToolDefinition | None:
        return self._tools.get(name)

    def require(self, name: str) -> McpToolDefinition:
        definition = self.get(name)
        if definition is None:
            raise McpRegistryError("unknown_tool")
        return definition

    def discoverable(
        self, permissions: frozenset[str]
    ) -> tuple[McpToolDefinition, ...]:
        return tuple(
            definition
            for name, definition in sorted(self._tools.items())
            if definition.permissions.issubset(permissions)
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


def build_mcp_tool_registry() -> McpToolRegistry:
    registry = McpToolRegistry(
        known_permissions=frozenset(
            {
                "target.inspect",
                "target.visual",
                "target.control",
                "workspace.inspect",
                "workspace.edit",
                "workspace.deploy",
                "development_source.inspect",
                "workspace.capture",
                "development.execute",
                "development.request_promotion",
                "managed.lifecycle",
                "development.settings.write",
            }
        )
    )
    registry.register(
        McpToolDefinition(
            name="addon_settings_set",
            schema_revision=1,
            permissions=frozenset({"development.settings.write", "workspace.inspect"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"workspace_execution"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_EXECUTE,
            adapter="agent_workspace.selected_addon_settings",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="addon_settings_result",
            schema_revision=1,
            permissions=frozenset({"development.settings.write", "workspace.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"workspace_execution"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_READ,
            adapter="agent_workspace.selected_settings_receipt",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    for name, effect in (
        ("managed_selection_preview", ToolEffect.READ_ONLY),
        ("managed_select", ToolEffect.MUTATING),
    ):
        registry.register(
            McpToolDefinition(
                name=name,
                schema_revision=1,
                permissions=frozenset({"managed.lifecycle", "workspace.inspect"}),
                effect=effect,
                allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
                required_capabilities=frozenset(),
                required_states=frozenset(),
                rate_class=ToolRateClass.MANAGED_LIFECYCLE,
                adapter="managed_runtime." + name,
                production_eligible=False,
                target_scope_required=False,
                scope=ToolScope.WORKSPACE_TARGET,
            )
        )
    for name in ("managed_ensure_ready", "managed_release"):
        registry.register(
            McpToolDefinition(
                name=name,
                schema_revision=1,
                permissions=frozenset({"managed.lifecycle"}),
                effect=ToolEffect.MUTATING,
                allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
                required_capabilities=frozenset(),
                required_states=frozenset({"reconciled"}),
                rate_class=ToolRateClass.MANAGED_LIFECYCLE,
                adapter="managed_runtime." + name,
                production_eligible=False,
                target_scope_required=True,
                scope=ToolScope.TARGET,
            )
        )
    for name, effect, rate in (
        (
            "workspace_import_preview",
            ToolEffect.READ_ONLY,
            ToolRateClass.DEVELOPMENT_READ,
        ),
        ("workspace_import", ToolEffect.MUTATING, ToolRateClass.DEVELOPMENT_EDIT),
    ):
        registry.register(
            McpToolDefinition(
                name=name,
                schema_revision=1,
                permissions=frozenset({"workspace.edit", "development_source.inspect"}),
                effect=effect,
                allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
                required_capabilities=frozenset({"registered_development_source"}),
                required_states=frozenset(),
                rate_class=rate,
                adapter="agent_workspace." + name,
                production_eligible=False,
                target_scope_required=False,
                scope=ToolScope.WORKSPACE,
            )
        )
    registry.register(
        McpToolDefinition(
            name="screenshot_retrieve",
            schema_revision=1,
            permissions=frozenset({"target.visual"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"screenshot"}),
            required_states=frozenset({"reconciled"}),
            rate_class=ToolRateClass.READ_BOUNDED,
            adapter="kodi_target.screenshot.retrieve_exact_managed_capture",
            production_eligible=False,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="log_read",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(TargetClass),
            required_capabilities=frozenset({"target_inspect"}),
            required_states=frozenset({"reconciled"}),
            rate_class=ToolRateClass.READ_BOUNDED,
            adapter="kodi_target.logs.read",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="inspect_ui_state",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(TargetClass),
            required_capabilities=frozenset({"target_inspect"}),
            required_states=frozenset({"reconciled"}),
            rate_class=ToolRateClass.READ_BOUNDED,
            adapter="kodi_target.ui_state.inspect",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="promotion_request",
            schema_revision=1,
            permissions=frozenset({"development.request_promotion"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset(
                {TargetClass.INTERNAL_LAB, TargetClass.EXTERNAL_DEVELOPMENT}
            ),
            required_capabilities=frozenset(
                {"workspace_deployment", "workspace_execution"}
            ),
            required_states=frozenset({"running", "ready", "reconciled"}),
            rate_class=ToolRateClass.DEVELOPMENT_PROMOTION_REQUEST,
            adapter="agent_workspace.prepare_promotion_for_web_review",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="remote_action",
            schema_revision=1,
            permissions=frozenset({"target.control"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset(
                {
                    TargetClass.INTERNAL_LAB,
                    TargetClass.EXTERNAL_DEVELOPMENT,
                    TargetClass.EXTERNAL_PRODUCTION,
                }
            ),
            required_capabilities=frozenset({"semantic_remote"}),
            required_states=frozenset({"running", "ready", "reconciled"}),
            rate_class=ToolRateClass.REMOTE_CONTROL,
            adapter="kodi_target.remote.invoke_replay_safe",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="addon_list",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(
                {
                    TargetClass.INTERNAL_LAB,
                    TargetClass.EXTERNAL_DEVELOPMENT,
                    TargetClass.EXTERNAL_PRODUCTION,
                }
            ),
            required_capabilities=frozenset({"target_inspect", "addon_inventory"}),
            required_states=frozenset({"reconciled"}),
            rate_class=ToolRateClass.READ_BOUNDED,
            adapter="kodi_target.addon_inventory.list",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="screenshot_capture",
            schema_revision=1,
            permissions=frozenset({"target.visual"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(
                {
                    TargetClass.INTERNAL_LAB,
                    TargetClass.EXTERNAL_DEVELOPMENT,
                    TargetClass.EXTERNAL_PRODUCTION,
                }
            ),
            required_capabilities=frozenset({"screenshot"}),
            required_states=frozenset({"reconciled"}),
            rate_class=ToolRateClass.VISUAL_CAPTURE,
            adapter="kodi_target.screenshot.capture",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="target_list",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(
                {
                    TargetClass.INTERNAL_LAB,
                    TargetClass.EXTERNAL_DEVELOPMENT,
                    TargetClass.EXTERNAL_PRODUCTION,
                }
            ),
            required_capabilities=frozenset({"target_inspect"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.READ_LIGHT,
            adapter="kodi_target.status.list",
            production_eligible=True,
            target_scope_required=False,
            scope=ToolScope.GLOBAL,
        )
    )
    registry.register(
        McpToolDefinition(
            name="target_status",
            schema_revision=1,
            permissions=frozenset({"target.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(
                {
                    TargetClass.INTERNAL_LAB,
                    TargetClass.EXTERNAL_DEVELOPMENT,
                    TargetClass.EXTERNAL_PRODUCTION,
                }
            ),
            required_capabilities=frozenset({"target_inspect"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.READ_LIGHT,
            adapter="kodi_target.status.get",
            production_eligible=True,
            target_scope_required=True,
            scope=ToolScope.TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="development_source_list",
            schema_revision=1,
            permissions=frozenset({"workspace.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"registered_development_source"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_READ,
            adapter="development_source.list_granted",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.DEVELOPMENT_SOURCE,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_create",
            schema_revision=1,
            permissions=frozenset({"workspace.edit"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"registered_development_source"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_CREATE,
            adapter="agent_workspace.create_replay_safe",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.DEVELOPMENT_SOURCE,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_edit",
            schema_revision=1,
            permissions=frozenset({"workspace.edit"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"agent_workspace"}),
            required_states=frozenset({"ready", "present"}),
            rate_class=ToolRateClass.DEVELOPMENT_EDIT,
            adapter="agent_workspace.edit_generation_bound",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_source_scan",
            schema_revision=1,
            permissions=frozenset({"development_source.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"registered_development_source"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_READ,
            adapter="agent_workspace.scan_registered_source",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_capture",
            schema_revision=1,
            permissions=frozenset({"workspace.capture"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"registered_development_source"}),
            required_states=frozenset({"ready", "present"}),
            rate_class=ToolRateClass.DEVELOPMENT_EDIT,
            adapter="agent_workspace.capture_registered_source",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_deploy_preflight",
            schema_revision=1,
            permissions=frozenset({"workspace.deploy"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset(
                {TargetClass.INTERNAL_LAB, TargetClass.EXTERNAL_DEVELOPMENT}
            ),
            required_capabilities=frozenset(),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_READ,
            adapter="agent_workspace.deployment_preflight",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_deploy",
            schema_revision=1,
            permissions=frozenset({"workspace.deploy"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset(
                {TargetClass.INTERNAL_LAB, TargetClass.EXTERNAL_DEVELOPMENT}
            ),
            required_capabilities=frozenset({"workspace_deployment"}),
            required_states=frozenset({"running", "ready", "reconciled"}),
            rate_class=ToolRateClass.DEVELOPMENT_DEPLOY,
            adapter="agent_workspace.deploy_exact_generation",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_execute",
            schema_revision=1,
            permissions=frozenset({"development.execute"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset(
                {TargetClass.INTERNAL_LAB, TargetClass.EXTERNAL_DEVELOPMENT}
            ),
            required_capabilities=frozenset({"workspace_execution"}),
            required_states=frozenset({"running", "ready", "reconciled"}),
            rate_class=ToolRateClass.DEVELOPMENT_EXECUTE,
            adapter="agent_workspace.execute_fixed_workflow",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_launch",
            schema_revision=1,
            permissions=frozenset({"target.control", "development.execute"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"workspace_execution"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_EXECUTE,
            adapter="agent_workspace.launch_manifest_entry",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_execution_recover",
            schema_revision=1,
            permissions=frozenset({"target.control", "development.execute"}),
            effect=ToolEffect.MUTATING,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"workspace_execution"}),
            required_states=frozenset(),
            rate_class=ToolRateClass.DEVELOPMENT_EXECUTE,
            adapter="agent_workspace.recover_ambiguous_smoke",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE_TARGET,
        )
    )
    registry.register(
        McpToolDefinition(
            name="workspace_validate",
            schema_revision=1,
            permissions=frozenset({"workspace.inspect"}),
            effect=ToolEffect.READ_ONLY,
            allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
            required_capabilities=frozenset({"agent_workspace"}),
            required_states=frozenset({"ready", "present"}),
            rate_class=ToolRateClass.DEVELOPMENT_VALIDATE,
            adapter="agent_workspace.validate_structure",
            production_eligible=False,
            target_scope_required=False,
            scope=ToolScope.WORKSPACE,
        )
    )
    for name, adapter, states in (
        (
            "workspace_read",
            "agent_workspace.read_text",
            frozenset({"ready", "present"}),
        ),
        ("workspace_status", "agent_workspace.status", frozenset()),
        (
            "workspace_tree",
            "agent_workspace.inspect_directory",
            frozenset({"ready", "present"}),
        ),
    ):
        registry.register(
            McpToolDefinition(
                name=name,
                schema_revision=1,
                permissions=frozenset({"workspace.inspect"}),
                effect=ToolEffect.READ_ONLY,
                allowed_target_classes=frozenset({TargetClass.INTERNAL_LAB}),
                required_capabilities=frozenset({"agent_workspace"}),
                required_states=states,
                rate_class=ToolRateClass.DEVELOPMENT_READ,
                adapter=adapter,
                production_eligible=False,
                target_scope_required=False,
                scope=ToolScope.WORKSPACE,
            )
        )
    return registry

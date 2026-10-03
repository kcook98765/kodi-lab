from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
)
from kodi_lab.mcp.authorization import McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import WorkspaceExecutionRecoverInput
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base


def test_custom_subset_is_immutable_and_excludes_unrelated_tools(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'subset.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    administration = McpAdministrationService(sessions)
    selected = frozenset(
        {
            "workspace.inspect",
            "workspace.deploy",
            "development.execute",
        }
    )
    preset = administration.create_custom_preset(
        display_name="Physical development acceptance",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=selected,
    )
    assert frozenset(preset.permissions) == selected
    client = administration.create_client(
        display_name="Acceptance client", preset_id=preset.id
    )
    authorization = McpAuthorizationService(sessions, build_mcp_tool_registry())
    from uuid import uuid4

    from kodi_lab.application.mcp_administration import McpPrincipal

    principal = McpPrincipal(
        client_id=client.id,
        credential_id=uuid4(),
        preset_id=preset.id,
        permissions=selected,
    )
    discoverable = set(authorization.discoverable_tools(principal))
    assert {
        "workspace_validate",
        "workspace_deploy",
        "workspace_execute",
    } <= discoverable
    assert "workspace_execution_recover" not in discoverable
    assert (
        not {
            "promotion_request",
            "remote_action",
            "screenshot_capture",
            "workspace_create",
            "workspace_edit",
            "target_list",
            "addon_list",
        }
        & discoverable
    )
    assert (
        frozenset(administration.get_client(client.id).preset.permissions) == selected
    )
    administration.set_client_enabled(client.id, False)
    assert not administration.get_client(client.id).enabled
    engine.dispose()


def test_workspace_recovery_discovery_requires_both_existing_permissions() -> None:
    registry = build_mcp_tool_registry()
    recover = "workspace_execution_recover"
    for permissions in (
        frozenset(),
        frozenset({"target.control"}),
        frozenset({"development.execute"}),
    ):
        assert recover not in {tool.name for tool in registry.discoverable(permissions)}
    definition = registry.get(recover)
    assert definition is not None
    assert definition.permissions == frozenset(
        {"target.control", "development.execute"}
    )
    assert {tool.name for tool in registry.discoverable(definition.permissions)} >= {
        recover
    }
    assert definition.allowed_target_classes == frozenset({"internal_lab"})
    assert not definition.production_eligible
    schema = WorkspaceExecutionRecoverInput.model_json_schema()
    assert set(schema["properties"]) == {"request_id", "execution_operation_id"}
    assert set(schema["required"]) == {"request_id", "execution_operation_id"}
    assert schema["additionalProperties"] is False
    with pytest.raises(ValidationError):
        WorkspaceExecutionRecoverInput.model_validate(
            {
                "request_id": "11000000-0000-4000-8000-000000000001",
                "execution_operation_id": "11000000-0000-4000-8000-000000000002",
                "container_id": "a" * 64,
            }
        )


def test_custom_subset_must_be_nonempty_and_below_template(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'reject.db'}")
    Base.metadata.create_all(engine)
    administration = McpAdministrationService(create_session_factory(engine))
    for selected in (frozenset(), frozenset({"unknown.permission"})):
        with pytest.raises(
            McpAdministrationError, match="mcp_preset_permissions_invalid"
        ):
            administration.create_custom_preset(
                display_name="Rejected preset",
                template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
                permissions=selected,
            )
    engine.dispose()

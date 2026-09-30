from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
)
from kodi_lab.mcp.authorization import McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
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

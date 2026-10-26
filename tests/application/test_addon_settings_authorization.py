"""The new settings permission is opt-in and intersects exact durable grants."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.kodi.addon_settings import AddonSettingOutcome, AddonSettingsSetInput
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.storage.models import (
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
)
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_addon_settings import fixture, request


def test_defaults_do_not_inherit_settings_write_and_schema_cannot_select_targets(
    tmp_path: Path,
) -> None:
    env, _bridge, _service = fixture(tmp_path)
    admin = McpAdministrationService(env.sessions)
    for preset in admin.list_presets():
        if preset.builtin:
            assert "development.settings.write" not in preset.permissions
    for template in (
        ADDON_DEVELOPMENT_PRESET_ID,
        FULL_LAB_CONTROL_PRESET_ID,
        READ_ONLY_INSPECTION_PRESET_ID,
    ):
        copy = admin.create_custom_preset(
            display_name=str(uuid4()), template_preset_id=template
        )
        assert "development.settings.write" not in copy.permissions
    definition = build_mcp_tool_registry().get("addon_settings_set")
    assert definition is not None and not definition.production_eligible
    assert definition.allowed_target_classes == frozenset({TargetClass.INTERNAL_LAB})
    schema = AddonSettingsSetInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "request_id",
        "expected_session_generation",
        "expected_workspace_generation",
        "settings",
    }
    for key in ("addon_id", "target_id", "container_id", "path", "python", "operation"):
        with pytest.raises(ValidationError):
            AddonSettingsSetInput.model_validate(
                {**request().model_dump(mode="json"), key: "arbitrary"}
            )
    data = request().model_dump(mode="json")
    with pytest.raises(ValidationError, match="duplicate"):
        AddonSettingsSetInput.model_validate({**data, "settings": data["settings"] * 2})


@pytest.mark.parametrize(
    "revocation",
    [
        "none",
        "source",
        "target",
        "credential",
        "preset",
        "other_client",
        "during_source",
        "during_target",
        "during_credential",
    ],
)
def test_exact_permission_source_binding_target_and_credential_intersection(
    tmp_path: Path, revocation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, bridge, service = fixture(tmp_path)
    admin = McpAdministrationService(env.sessions)
    preset = admin.create_custom_preset(
        display_name="Settings opt-in",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"workspace.inspect", "development.settings.write"}),
    )
    admin.assign_client_preset(env.client_id, preset.id)
    admin.assign_target_preset(fixtures.TARGET_ID, preset.id)
    admin.grant_target(env.client_id, fixtures.TARGET_ID)
    admin.grant_development_source(env.client_id, fixtures.SOURCE_ID)
    issued = admin.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(issued.token)
    assert principal is not None
    auth = McpAuthorizationService(env.sessions, build_mcp_tool_registry())
    assert "addon_settings_set" in auth.discoverable_tools(principal)
    if revocation == "source":
        with env.sessions.begin() as session:
            session.execute(
                delete(McpClientDevelopmentSourceGrantRow).where(
                    McpClientDevelopmentSourceGrantRow.client_id == str(env.client_id)
                )
            )
    elif revocation == "target":
        with env.sessions.begin() as session:
            session.execute(
                delete(McpClientTargetGrantRow).where(
                    McpClientTargetGrantRow.client_id == str(env.client_id)
                )
            )
    elif revocation == "credential":
        admin.revoke_credential(env.client_id, issued.credential.id)
    elif revocation == "preset":
        admin.assign_client_preset(env.client_id, READ_ONLY_INSPECTION_PRESET_ID)
    elif revocation == "other_client":
        client = admin.create_client(
            display_name="Other settings client", preset_id=preset.id
        )
        token = admin.issue_credential(client.id)
        principal = McpAuthenticationService(env.sessions).authenticate(token.token)
        assert principal is not None
        admin.grant_target(client.id, fixtures.TARGET_ID)
        admin.grant_development_source(client.id, fixtures.SOURCE_ID)

    if revocation.startswith("during_"):
        original = bridge.settings

        def revoked_after_write(
            observed: object, version: str, payload: dict[str, object]
        ) -> tuple[AddonSettingOutcome, ...]:
            outcomes = original(observed, version, payload)
            if revocation == "during_source":
                admin.remove_development_source_grant(env.client_id, fixtures.SOURCE_ID)
            elif revocation == "during_target":
                admin.remove_target_grant(env.client_id, fixtures.TARGET_ID)
            else:
                admin.revoke_credential(env.client_id, issued.credential.id)
            return outcomes

        monkeypatch.setattr(bridge, "settings", revoked_after_write)

    def authorize(session: Session, workspace: UUID, target: UUID) -> None:
        auth.recheck_workspace_launch(
            session, principal, workspace, target, tool_name="addon_settings_set"
        )

    if revocation == "none":
        result = service.set(
            client_id=env.client_id, request=request(), authorize=authorize
        )
        assert result.state == "completed" and bridge.count == 1
    else:
        with pytest.raises(McpAuthorizationError):
            service.set(
                client_id=principal.client_id, request=request(), authorize=authorize
            )
        assert bridge.count == (1 if revocation.startswith("during_") else 0)
        if revocation.startswith("during_"):
            from sqlalchemy import select

            from kodi_lab.storage.models import McpAddonSettingsOperationRow

            with env.sessions() as session:
                row = session.scalar(select(McpAddonSettingsOperationRow))
                assert row is not None and row.state == "ambiguous"
                assert row.results_json == '[{"id":"debug","status":"verified"}]'


def test_agent_discovers_selected_generation_through_existing_owned_workspace_status(
    tmp_path: Path,
) -> None:
    from typing import Any, cast

    from kodi_lab.mcp.tools import McpTargetToolService

    env, _bridge, _service = fixture(tmp_path)
    admin = McpAdministrationService(env.sessions)
    admin.grant_development_source(env.client_id, fixtures.SOURCE_ID)
    admin.grant_target(env.client_id, fixtures.TARGET_ID)
    principal = McpAuthenticationService(env.sessions).authenticate(
        admin.issue_credential(env.client_id).token
    )
    assert principal is not None
    authorization = McpAuthorizationService(env.sessions, build_mcp_tool_registry())
    tools = McpTargetToolService(
        env.sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=env.workspaces,
    )
    summary = tools.workspace_status(
        principal, workspace_id=env.workspace_id
    ).workspaces[0]
    assert summary.generation == 0 and summary.selected_session_generation == 1
    admin.remove_target_grant(env.client_id, fixtures.TARGET_ID)
    summary = tools.workspace_status(
        principal, workspace_id=env.workspace_id
    ).workspaces[0]
    assert summary.generation == 0 and summary.selected_session_generation is None

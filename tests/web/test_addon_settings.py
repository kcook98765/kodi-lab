"""MCP settings wire schemas and audits never disclose submitted values."""

import logging
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
)
from kodi_lab.kodi.addon_settings import (
    AddonSettingOutcome,
    AddonSettingsSetInput,
    AddonSettingsSetResult,
)
from tests.web.conftest import INSTANCE_ID
from tests.web.test_mcp import _administration, _post, _strict_tool_result


def test_settings_wire_derives_identity_and_redacts_all_values(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    database: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Earlier Alembic fileConfig calls can disable pre-existing loggers. Restore
    # this actual audit logger for the bounded redaction assertion, then let
    # monkeypatch restore its incoming state.
    audit_logger = logging.getLogger("kodi_lab.mcp")
    monkeypatch.setattr(audit_logger, "disabled", False)
    monkeypatch.setattr(audit_logger, "propagate", True)
    caplog.set_level(logging.INFO, logger="kodi_lab.mcp")
    context = cast(FastAPI, client.app).state.kodi_lab
    root = context.settings.development_root / "script.settings-wire"
    root.mkdir()
    (root / "addon.xml").write_text(
        '<addon id="script.settings-wire" name="Settings" version="1.0.0">'
        '<extension point="xbmc.python.script" library="main.py"/></addon>'
    )
    (root / "main.py").write_text("pass\n")
    source = context.development_source_administration.register_source(
        logical_name="Settings wire", relative_locator=root.name
    )
    creation = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    admin = _administration(client)
    old = admin.create_client(
        display_name="Default client", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    old_token = admin.issue_credential(old.id).token
    assert "addon_settings_set" not in {
        v["name"]
        for v in _post(client, old_token, "tools/list").json()["result"]["tools"]
    }
    preset = admin.create_custom_preset(
        display_name="Settings opt-in",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"workspace.inspect", "development.settings.write"}),
    )
    agent = admin.create_client(display_name="Settings client", preset_id=preset.id)
    admin.grant_development_source(agent.id, source.id)
    admin.bind_workspace(agent.id, creation.workspace_id)
    admin.assign_target_preset(INSTANCE_ID, preset.id)
    admin.grant_target(agent.id, INSTANCE_ID)
    token = admin.issue_credential(agent.id).token
    tools = {
        v["name"]: v
        for v in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    schema = tools["addon_settings_set"]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert not {"target_id", "addon_id", "container_id", "path", "python"} & set(
        schema["properties"]
    )
    calls = []
    receipts: dict[object, AddonSettingsSetResult] = {}

    def accept(**kwargs: Any) -> AddonSettingsSetResult:
        req = cast(AddonSettingsSetInput, kwargs["request"])
        with database() as session:
            kwargs["authorize"](session, creation.workspace_id, INSTANCE_ID)
        calls.append(req.request_id)
        receipt = AddonSettingsSetResult(
            operation_id=uuid4(),
            request_id=req.request_id,
            workspace_id=creation.workspace_id,
            target_id=INSTANCE_ID,
            session_generation=1,
            generation=0,
            state="completed",
            results=(AddonSettingOutcome(id="title", status="verified"),),
        )
        receipts[req.request_id] = receipt
        return receipt

    def original_receipt(**kwargs: Any) -> AddonSettingsSetResult:
        with database() as session:
            kwargs["authorize"](session, creation.workspace_id, INSTANCE_ID)
        return receipts[kwargs["request_id"]].model_copy(update={"replayed": True})

    monkeypatch.setattr(context.addon_settings, "result", original_receipt)
    monkeypatch.setattr(context.addon_settings, "set", accept)
    arguments = {
        "request_id": str(uuid4()),
        "expected_session_generation": 1,
        "expected_workspace_generation": 0,
        "settings": [{"id": "title", "value": "private-canary-setting-value"}],
    }

    def call(args: dict[str, Any], bearer: str = token) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _post(
                client,
                bearer,
                "tools/call",
                params={"name": "addon_settings_set", "arguments": args},
            ).json()["result"],
        )

    result = call(arguments)
    _strict_tool_result(result, tools["addon_settings_set"]["outputSchema"])
    assert not result["isError"] and result["structuredContent"]["results"] == [
        {"id": "title", "status": "verified"}
    ]
    assert "private-canary-setting-value" not in str(result) + caplog.text
    recovered = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "addon_settings_result",
            "arguments": {"request_id": arguments["request_id"]},
        },
    ).json()["result"]
    _strict_tool_result(recovered, tools["addon_settings_result"]["outputSchema"])
    assert recovered["structuredContent"]["replayed"]
    assert (
        recovered["structuredContent"]["operation_id"]
        == result["structuredContent"]["operation_id"]
    )
    assert tools["addon_settings_result"]["annotations"]["readOnlyHint"] is True

    assert call({**arguments, "addon_id": "service.other"})["isError"]
    assert call(
        {
            **arguments,
            "settings": [
                {"id": "title", "value": {"secret": "private-canary-setting-value"}}
            ],
        }
    )["isError"]
    assert call(arguments, old_token)["isError"]
    assert len(calls) == 1
    admin.remove_target_grant(agent.id, INSTANCE_ID)
    assert call(arguments)["isError"] and len(calls) == 1
    assert "private-canary-setting-value" not in caplog.text
    assert any("mcp_request" in record.getMessage() for record in caplog.records)

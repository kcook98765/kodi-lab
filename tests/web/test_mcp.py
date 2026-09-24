from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import ImageContent
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_promotion_requests import PromotionRequestResult
from kodi_lab.application.mcp_workspace_development import (
    WorkspaceDeploymentResult,
    WorkspaceExecutionResult,
)
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import PNG_SIGNATURE, validate_png
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.transport import (
    BoundedTokenBucketLimiter,
    ModernMcpAuthMiddleware,
    build_mcp_http_transport,
)
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ManagedScreenshotRow,
    McpCredentialRow,
    McpDevelopmentRequestRow,
    McpPresetPermissionRow,
    McpPresetRow,
    McpSecurityEventRow,
    TargetRow,
)

from .conftest import (
    EXTERNAL_TARGET_ID,
    INSTANCE_ID,
    ORIGIN,
    REVISION_ID,
    FakeBroker,
    FakeExternalKodi,
    login,
    seed_external_target,
)

PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "kodi-lab-test",
        "version": "1",
    },
}


def _administration(client: TestClient) -> McpAdministrationService:
    app = cast(FastAPI, client.app)
    return cast(McpAdministrationService, app.state.kodi_lab.mcp_administration)


def _authority(client: TestClient) -> tuple[UUID, str]:
    administration = _administration(client)
    principal = administration.create_client(
        display_name="Modern wire client",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(principal.id, INSTANCE_ID)
    issued = administration.issue_credential(principal.id)
    return principal.id, issued.token


def _post(
    client: TestClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    materialized = {} if params is None else dict(params)
    materialized["_meta"] = META
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if method == "tools/call" and isinstance(materialized.get("name"), str):
        headers["Mcp-Name"] = cast(str, materialized["name"])
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": materialized,
        },
        follow_redirects=False,
    )


def _legacy_post(
    client: TestClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": LEGACY_PROTOCOL,
        "Mcp-Method": method,
    }
    if method == "tools/call" and isinstance((params or {}).get("name"), str):
        headers["Mcp-Name"] = cast(str, (params or {})["name"])
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        },
        follow_redirects=False,
    )


def _make_target_ready(client: TestClient) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/provision", json={}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/start", json={}, headers=headers
        ).status_code
        == 200
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()[
            "screenshot_available_now"
        ]:
            return
        time.sleep(0.01)
    raise AssertionError("managed target did not become ready")


def test_modern_wire_is_stateless_and_exposes_only_read_only_tools(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)

    discover = _post(client, token, "server/discover", request_id=10)
    assert discover.status_code == 200
    assert discover.headers.get("mcp-session-id") is None
    assert discover.json()["result"]["supportedVersions"] == [PROTOCOL]
    assert set(discover.json()["result"]["capabilities"]) == {"tools"}

    listed = _post(client, token, "tools/list", request_id=11)
    assert listed.status_code == 200
    assert listed.headers.get("mcp-session-id") is None
    listed_result = listed.json()["result"]
    assert listed_result["ttlMs"] == 0
    assert listed_result["cacheScope"] == "private"
    tools = listed_result["tools"]
    assert [item["name"] for item in tools] == [
        "addon_list",
        "screenshot_capture",
        "target_list",
        "target_status",
    ]
    assert all(item["_meta"]["kodiLabMcpSchemaVersion"] == 1 for item in tools)
    schemas = {item["name"]: item["inputSchema"] for item in tools}
    assert set(schemas["addon_list"]["properties"]) == {
        "target_id",
        "cursor",
        "limit",
    }
    assert set(schemas["screenshot_capture"]["properties"]) == {
        "target_id",
        "request_id",
    }
    assert schemas["screenshot_capture"]["required"] == ["target_id"]

    target_list = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {}},
        request_id=12,
    )
    assert target_list.status_code == 200
    listed_content = target_list.json()["result"]["structuredContent"]
    assert listed_content["schema_version"] == 1
    assert [item["target_id"] for item in listed_content["targets"]] == [
        str(INSTANCE_ID)
    ]

    status = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "target_status",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
        request_id=13,
    )
    assert status.status_code == 200
    status_content = status.json()["result"]["structuredContent"]
    assert status_content["target"]["target_id"] == str(INSTANCE_ID)
    assert "container_id" not in str(status_content)
    assert "jsonrpc_port" not in str(status_content)

    for tool_name in ("addon_list", "screenshot_capture"):
        unavailable = _post(
            client,
            token,
            "tools/call",
            params={
                "name": tool_name,
                "arguments": {"target_id": str(INSTANCE_ID)},
            },
        ).json()["result"]
        assert unavailable["isError"] is True
        assert unavailable["structuredContent"] == {"error": "target_not_running"}


def test_pinned_official_sdk_uses_only_modern_stateless_requests(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    methods: list[str] = []

    async def capture(request: httpx2.Request) -> None:
        body = json.loads(request.content)
        methods.append(cast(str, body["method"]))

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"request": [capture]},
        ) as http:
            discovery_streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            async with Client(discovery_streams, mode="auto", cache=None) as probe:
                assert probe.session.protocol_version == PROTOCOL
                await probe.list_tools()
            streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            async with Client(streams, mode=PROTOCOL, cache=None) as official:
                assert official.session.protocol_version == PROTOCOL
                listed = await official.list_tools()
                assert [tool.name for tool in listed.tools] == [
                    "addon_list",
                    "screenshot_capture",
                    "target_list",
                    "target_status",
                ]
                target_list = await official.call_tool("target_list", {})
                assert target_list.is_error is False
                status = await official.call_tool(
                    "target_status", {"target_id": str(INSTANCE_ID)}
                )
                assert status.is_error is False

    asyncio.run(exercise())
    assert methods == [
        "server/discover",
        "tools/list",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    assert "initialize" not in methods
    assert "notifications/initialized" not in methods


def test_official_sdk_legacy_http_shares_catalog_replay_and_current_authority(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    _make_target_ready(client)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Cross-protocol client",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    issued = administration.issue_credential(mcp_client.id)
    observed: list[tuple[str, str | None, str | None]] = []

    async def capture(request: httpx2.Request) -> None:
        body = json.loads(request.content)
        observed.append(
            (
                cast(str, body["method"]),
                request.headers.get("mcp-protocol-version"),
                request.headers.get("mcp-session-id"),
            )
        )

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {issued.token}"},
            event_hooks={"request": [capture]},
        ) as http:
            modern_streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            legacy_streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            async with (
                Client(modern_streams, mode=PROTOCOL, cache=None) as modern,
                Client(legacy_streams, mode="legacy", cache=None) as legacy,
            ):
                assert modern.protocol_version == PROTOCOL
                assert legacy.protocol_version == LEGACY_PROTOCOL
                modern_tools = await modern.list_tools()
                legacy_tools = await legacy.list_tools()
                assert [item.model_dump() for item in modern_tools.tools] == [
                    item.model_dump() for item in legacy_tools.tools
                ]
                assert [item.name for item in modern_tools.tools] == [
                    "addon_list",
                    "development_source_list",
                    "promotion_request",
                    "remote_action",
                    "screenshot_capture",
                    "target_list",
                    "target_status",
                    "workspace_create",
                    "workspace_deploy",
                    "workspace_edit",
                    "workspace_execute",
                    "workspace_read",
                    "workspace_status",
                    "workspace_tree",
                    "workspace_validate",
                ]

                request_id = str(uuid4())
                arguments = {
                    "request_id": request_id,
                    "target_id": str(INSTANCE_ID),
                    "action": "volume_up",
                }
                modern_first = await modern.call_tool("remote_action", arguments)
                legacy_replay = await legacy.call_tool("remote_action", arguments)
                assert not modern_first.is_error and not legacy_replay.is_error
                assert (
                    modern_first.structured_content == legacy_replay.structured_content
                )
                assert broker.remote_calls == [BrowserRemoteAction.VOLUME_UP]

                reverse_id = str(uuid4())
                reverse = {
                    "request_id": reverse_id,
                    "target_id": str(INSTANCE_ID),
                    "action": "volume_down",
                }
                legacy_first = await legacy.call_tool("remote_action", reverse)
                modern_replay = await modern.call_tool("remote_action", reverse)
                assert not legacy_first.is_error and not modern_replay.is_error
                assert (
                    legacy_first.structured_content == modern_replay.structured_content
                )
                assert broker.remote_calls == [
                    BrowserRemoteAction.VOLUME_UP,
                    BrowserRemoteAction.VOLUME_DOWN,
                ]

                administration.remove_target_grant(mcp_client.id, INSTANCE_ID)
                modern_denied = await modern.call_tool(
                    "target_status", {"target_id": str(INSTANCE_ID)}
                )
                legacy_denied = await legacy.call_tool(
                    "target_status", {"target_id": str(INSTANCE_ID)}
                )
                assert modern_denied.is_error and legacy_denied.is_error
                assert (
                    modern_denied.structured_content == legacy_denied.structured_content
                )
                administration.grant_target(mcp_client.id, INSTANCE_ID)

                administration.assign_target_preset(
                    INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID
                )
                for protocol_client in (modern, legacy):
                    denied = await protocol_client.call_tool(
                        "remote_action",
                        {
                            "request_id": str(uuid4()),
                            "target_id": str(INSTANCE_ID),
                            "action": "volume_up",
                        },
                    )
                    assert denied.is_error
                    assert denied.structured_content == {"error": "permission_denied"}
                administration.assign_target_preset(
                    INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID
                )

                administration.assign_client_preset(
                    mcp_client.id, READ_ONLY_INSPECTION_PRESET_ID
                )
                for protocol_client in (modern, legacy):
                    names = [
                        item.name for item in (await protocol_client.list_tools()).tools
                    ]
                    assert "remote_action" not in names
                administration.assign_client_preset(
                    mcp_client.id, FULL_LAB_CONTROL_PRESET_ID
                )

                administration.set_client_enabled(mcp_client.id, False)
                for protocol_client in (modern, legacy):
                    with pytest.raises(MCPError):
                        await protocol_client.list_tools()
                administration.set_client_enabled(mcp_client.id, True)
                assert "remote_action" in {
                    item.name for item in (await legacy.list_tools()).tools
                }

                administration.revoke_credential(mcp_client.id, issued.credential.id)
                for protocol_client in (modern, legacy):
                    with pytest.raises(MCPError):
                        await protocol_client.list_tools()

    asyncio.run(exercise())
    assert observed[0] == ("initialize", None, None)
    assert any(
        method == "notifications/initialized" and version == LEGACY_PROTOCOL
        for method, version, _session in observed
    )
    assert any(
        method == "tools/list" and version == PROTOCOL
        for method, version, _session in observed
    )
    assert any(
        method == "tools/list" and version == LEGACY_PROTOCOL
        for method, version, _session in observed
    )
    assert all(session is None for _method, _version, session in observed)


def test_external_target_modern_legacy_status_inventory_and_replay_parity(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External HTTP parity",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    for tool in ("target_status", "addon_list"):
        params = {
            "name": tool,
            "arguments": {"target_id": str(EXTERNAL_TARGET_ID)},
        }
        modern = _post(client, token, "tools/call", params=params).json()["result"]
        legacy = _legacy_post(client, token, "tools/call", params=params).json()[
            "result"
        ]
        assert modern["isError"] is legacy["isError"] is False
        modern_content = dict(modern["structuredContent"])
        legacy_content = dict(legacy["structuredContent"])
        modern_content.pop("observed_at", None)
        legacy_content.pop("observed_at", None)
        assert modern_content == legacy_content

    arguments = {
        "request_id": str(uuid4()),
        "target_id": str(EXTERNAL_TARGET_ID),
        "action": "home",
    }
    modern = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    legacy = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    assert modern["isError"] is legacy["isError"] is False
    assert modern["structuredContent"] == legacy["structuredContent"]
    assert external_kodi.mutations == 1


def test_external_remote_rechecks_revocation_after_identity_before_dispatch(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External pre-dispatch recheck",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    def revoke() -> None:
        administration.remove_target_grant(mcp_client.id, EXTERNAL_TARGET_ID)

    external_kodi.on_identity = revoke
    arguments = {
        "request_id": str(uuid4()),
        "target_id": str(EXTERNAL_TARGET_ID),
        "action": "home",
    }

    first = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    replay = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    assert first["isError"] is replay["isError"] is True
    assert (
        first["structuredContent"]
        == replay["structuredContent"]
        == {"error": "target_not_found_or_not_authorized"}
    )
    assert external_kodi.mutations == 0


def test_observation_tools_are_bounded_native_and_retain_latest_on_failure(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                "z.disabled", "Zulu", "2.0", False, "xbmc.service"
            ),
            KodiAddonInventoryRecord(
                "a.enabled",
                "Alpha",
                "1.0",
                True,
                "xbmc.python.pluginsource",
            ),
        ),
    )
    _make_target_ready(client)
    _client_id, token = _authority(client)

    first_page = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "addon_list",
            "arguments": {"target_id": str(INSTANCE_ID), "limit": 1},
        },
    ).json()["result"]
    assert not first_page["isError"]
    first_inventory = first_page["structuredContent"]
    assert first_inventory["total_count"] == 2
    assert [item["addon_id"] for item in first_inventory["addons"]] == ["a.enabled"]
    assert first_inventory["next_cursor"] == "a.enabled"
    assert set(first_inventory["addons"][0]) == {
        "addon_id",
        "name",
        "version",
        "addon_type",
        "enabled",
        "provenance",
        "provenance_status",
    }
    second_inventory = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "addon_list",
            "arguments": {
                "target_id": str(INSTANCE_ID),
                "limit": 1,
                "cursor": first_inventory["next_cursor"],
            },
        },
    ).json()["result"]["structuredContent"]
    assert [item["addon_id"] for item in second_inventory["addons"]] == ["z.disabled"]
    assert second_inventory["next_cursor"] is None
    assert "path" not in json.dumps(second_inventory).lower()

    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=tuple(
            KodiAddonInventoryRecord(
                f"plugin.video.{index:03d}." + ("i" * 100),
                "n" * 256,
                "v" * 128,
                True,
                "t" * 128,
            )
            for index in range(100)
        ),
    )
    oversized_inventory = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "addon_list",
            "arguments": {"target_id": str(INSTANCE_ID), "limit": 100},
        },
    ).json()["result"]
    assert oversized_inventory["isError"] is True
    assert oversized_inventory["structuredContent"] == {"error": "response_too_large"}

    screenshot = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "screenshot_capture",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
    ).json()["result"]
    assert not screenshot["isError"]
    assert len(screenshot["content"]) == 1
    image = ImageContent.model_validate(screenshot["content"][0])
    assert image.mime_type == "image/png"
    png = base64.b64decode(image.data, validate=True)
    validated = validate_png(png)
    metadata = screenshot["structuredContent"]
    assert png.startswith(PNG_SIGNATURE)
    assert (validated.width, validated.height) == (
        metadata["width"],
        metadata["height"],
    )
    assert validated.byte_size == metadata["byte_size"]
    assert metadata["target_id"] == str(INSTANCE_ID)
    assert "path" not in json.dumps(screenshot).lower()
    with database() as session:
        latest = session.get(ManagedScreenshotRow, str(INSTANCE_ID))
        assert latest is not None
        retained_capture = latest.capture_id
        assert retained_capture == metadata["capture_id"]

    broker.screenshot_error = BrokerUnavailableError("private screenshot failure")
    failed = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "screenshot_capture",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
    ).json()["result"]
    assert failed["isError"] is True
    assert failed["structuredContent"] == {"error": "screenshot_capture_unavailable"}
    with database() as session:
        retained = session.get(ManagedScreenshotRow, str(INSTANCE_ID))
        assert retained is not None and retained.capture_id == retained_capture


def test_discovery_filters_visual_permission_without_bypassing_target_policy(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    administration = _administration(client)
    inspect_only_id = uuid4()
    with database.begin() as session:
        session.add(
            McpPresetRow(
                id=str(inspect_only_id),
                display_name="Test inspect only",
                builtin=False,
                assignable=True,
            )
        )
        session.add(
            McpPresetPermissionRow(
                preset_id=str(inspect_only_id), permission="target.inspect"
            )
        )
    mcp_client = administration.create_client(
        display_name="Inspect only", preset_id=inspect_only_id
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    token = administration.issue_credential(mcp_client.id).token

    inspect_names = [
        item["name"]
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    ]
    assert inspect_names == ["addon_list", "target_list", "target_status"]
    denied = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "screenshot_capture",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
    ).json()["result"]
    assert denied["isError"] is True
    assert denied["structuredContent"] == {"error": "permission_denied"}

    with database.begin() as session:
        session.add(
            McpPresetPermissionRow(
                preset_id=str(inspect_only_id), permission="target.visual"
            )
        )
    expanded_names = [
        item["name"]
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    ]
    assert expanded_names == [
        "addon_list",
        "screenshot_capture",
        "target_list",
        "target_status",
    ]


def test_remote_action_discovery_schema_replay_conflict_and_current_authority(
    client: TestClient, broker: FakeBroker
) -> None:
    _make_target_ready(client)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Control client", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    issued = administration.issue_credential(mcp_client.id)

    tools = _post(client, issued.token, "tools/list").json()["result"]["tools"]
    assert [item["name"] for item in tools] == [
        "addon_list",
        "development_source_list",
        "promotion_request",
        "remote_action",
        "screenshot_capture",
        "target_list",
        "target_status",
        "workspace_create",
        "workspace_deploy",
        "workspace_edit",
        "workspace_execute",
        "workspace_read",
        "workspace_status",
        "workspace_tree",
        "workspace_validate",
    ]
    remote = next(item for item in tools if item["name"] == "remote_action")
    workspace_create = next(
        item for item in tools if item["name"] == "workspace_create"
    )
    workspace_edit = next(item for item in tools if item["name"] == "workspace_edit")
    workspace_validate = next(
        item for item in tools if item["name"] == "workspace_validate"
    )
    assert set(remote["inputSchema"]["properties"]) == {
        "request_id",
        "target_id",
        "action",
    }
    assert set(remote["inputSchema"]["required"]) == {
        "request_id",
        "target_id",
        "action",
    }
    assert remote["outputSchema"]["type"] == "object"
    result_variants = {
        item["$ref"].rsplit("/", 1)[-1] for item in remote["outputSchema"]["anyOf"]
    }
    assert result_variants == {
        "McpRemoteActionResult",
        "ProductionApprovalRequiredResult",
        "ProductionApprovalTerminalResult",
    }
    assert remote["annotations"] == {
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "readOnlyHint": False,
    }
    assert set(workspace_create["inputSchema"]["properties"]) == {
        "request_id",
        "source_id",
    }
    assert set(workspace_create["inputSchema"]["required"]) == {
        "request_id",
        "source_id",
    }
    assert set(workspace_edit["inputSchema"]["properties"]) == {
        "workspace_id",
        "expected_generation",
        "operation",
        "relative_path",
        "content",
        "expected_text",
        "replacement_text",
    }
    assert workspace_edit["inputSchema"]["properties"]["operation"]["enum"] == [
        "create",
        "replace",
        "exact_patch",
    ]
    assert set(workspace_validate["inputSchema"]["properties"]) == {
        "workspace_id",
        "expected_generation",
    }
    assert set(workspace_validate["inputSchema"]["required"]) == {
        "workspace_id",
        "expected_generation",
    }
    assert workspace_validate["annotations"] == {
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "readOnlyHint": True,
    }

    request_id = str(uuid4())
    arguments = {
        "request_id": request_id,
        "target_id": str(INSTANCE_ID),
        "action": "left",
    }
    first = _post(
        client,
        issued.token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    replay = _post(
        client,
        issued.token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    assert first["isError"] is False and replay == first
    assert first["structuredContent"]["request_id"] == request_id
    assert first["structuredContent"]["action"] == "left"
    assert first["structuredContent"]["outcome"] == "succeeded"
    assert first["structuredContent"]["screenshot_refresh"]["result"] == ("succeeded")
    assert broker.remote_calls == [BrowserRemoteAction.LEFT]
    assert broker.screenshot_calls == 1

    conflict = _post(
        client,
        issued.token,
        "tools/call",
        params={
            "name": "remote_action",
            "arguments": arguments | {"action": "right"},
        },
    ).json()["result"]
    assert conflict["isError"] is True
    assert conflict["structuredContent"] == {"error": "request_id_conflict"}
    assert broker.remote_calls == [BrowserRemoteAction.LEFT]

    administration.set_client_enabled(mcp_client.id, False)
    denied = _post(
        client,
        issued.token,
        "tools/call",
        params={
            "name": "remote_action",
            "arguments": arguments | {"request_id": str(uuid4())},
        },
    )
    assert denied.status_code == 401


def test_mcp_raw_protocol_and_authentication_fail_closed(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    client_id, token = _authority(client)
    valid_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": META},
    }
    modern = {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/list",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    assert client.get("/mcp", follow_redirects=False).status_code == 405
    missing_version = client.post(
        "/mcp", headers={"Authorization": f"Bearer {token}"}, json=valid_body
    )
    assert missing_version.status_code == 400
    unsupported = client.post(
        "/mcp",
        headers={
            **modern,
            "MCP-Protocol-Version": "2025-06-18",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] != 0
    assert unsupported.json()["id"] == 1
    assert unsupported.json()["error"]["data"]["supported"] == [
        PROTOCOL,
    ]

    failures = [
        client.post("/mcp", headers=modern, json=valid_body),
        client.post(
            "/mcp", headers={**modern, "Authorization": "Basic no"}, json=valid_body
        ),
        client.post(
            "/mcp",
            headers={**modern, "Authorization": "Bearer invalid"},
            json=valid_body,
        ),
        client.post(
            f"/mcp?access_token={token}",
            headers={**modern, "Authorization": f"Bearer {token}"},
            json=valid_body,
        ),
    ]
    assert {(item.status_code, item.json()["error"]) for item in failures} == {
        (401, "authentication_failed")
    }

    malformed_json = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        content=b"{",
    )
    assert malformed_json.status_code == 400
    wrong_content_type = client.post(
        "/mcp",
        headers={
            **modern,
            "Content-Type": "text/plain",
            "Authorization": f"Bearer {token}",
        },
        content=b"{}",
    )
    assert wrong_content_type.status_code == 415
    mismatch = client.post(
        "/mcp",
        headers={
            **modern,
            "Mcp-Method": "tools/call",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert mismatch.status_code == 400
    session_header = client.post(
        "/mcp",
        headers={
            **modern,
            "Mcp-Session-Id": "not-a-session",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert session_header.status_code == 400
    invalid_method = _post(client, token, "debug/anything")
    assert invalid_method.status_code == 404
    initialize = _post(client, token, "initialize")
    assert initialize.status_code == 404
    subscription = _post(client, token, "subscriptions/listen")
    assert subscription.status_code == 404
    assert subscription.json()["error"]["code"] == -32601
    invalid_params = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_status", "arguments": {}},
    )
    assert invalid_params.status_code == 200
    assert invalid_params.json()["result"]["isError"] is True
    too_large = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        content=b" " * (64 * 1024 + 1),
    )
    assert too_large.status_code == 413
    invalid_origin = client.post(
        "/mcp",
        headers={
            **modern,
            "Authorization": f"Bearer {token}",
            "Origin": "https://attacker.example",
        },
        json=valid_body,
    )
    assert invalid_origin.status_code == 403

    administration = _administration(client)
    credential_id = administration.get_client(client_id).credentials[0].id
    administration.revoke_credential(client_id, credential_id)
    revoked = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        json=valid_body,
    )
    assert revoked.status_code == 401
    assert revoked.json() == {"error": "authentication_failed"}

    expired = administration.issue_credential(client_id)
    with database.begin() as session:
        row = session.get(McpCredentialRow, str(expired.credential.id))
        assert row is not None
        row.created_at = "2000-01-01T00:00:00.000000Z"
        row.expires_at = "2000-01-01T00:00:00.000001Z"
    expired_response = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {expired.token}"},
        json=valid_body,
    )
    assert expired_response.status_code == 401
    assert expired_response.json() == {"error": "authentication_failed"}

    disabled = administration.issue_credential(client_id)
    administration.set_client_enabled(client_id, False)
    disabled_response = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {disabled.token}"},
        json=valid_body,
    )
    assert disabled_response.status_code == 401
    assert disabled_response.json() == {"error": "authentication_failed"}


def test_legacy_http_malformed_input_and_downgrade_matrix(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    initialize_body = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "legacy-matrix", "version": "1"},
        },
    }
    initialized = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json=initialize_body,
    )
    assert initialized.status_code == 200
    assert initialized.headers.get("mcp-session-id") is None
    assert initialized.json()["result"]["protocolVersion"] == LEGACY_PROTOCOL

    listed = _legacy_post(client, token, "tools/list", request_id=11)
    assert listed.status_code == 200
    assert listed.headers.get("mcp-session-id") is None
    assert [item["name"] for item in listed.json()["result"]["tools"]] == [
        "addon_list",
        "screenshot_capture",
        "target_list",
        "target_status",
    ]

    legacy_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LEGACY_PROTOCOL,
    }
    malformed = client.post("/mcp", headers=legacy_headers, content=b"{")
    assert malformed.status_code == 400
    invalid_method = _legacy_post(client, token, "debug/anything")
    assert invalid_method.status_code == 404
    invalid_params = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "target_status", "arguments": {}},
    )
    assert invalid_params.status_code == 200
    assert invalid_params.json()["result"]["isError"] is True
    session_header = _legacy_post(
        client,
        token,
        "tools/list",
        extra_headers={"Mcp-Session-Id": "caller-state"},
    )
    assert session_header.status_code == 400
    mismatch = _legacy_post(
        client,
        token,
        "tools/list",
        extra_headers={"Mcp-Method": "tools/call"},
    )
    assert mismatch.status_code == 400

    # An explicit era never falls through to the other era's method set.
    legacy_discover = _legacy_post(client, token, "server/discover")
    assert legacy_discover.status_code == 404
    modern_initialize = _post(
        client,
        token,
        "initialize",
        params=cast(dict[str, Any], initialize_body["params"]),
    )
    assert modern_initialize.status_code == 404
    wrong_headerless_initialize = dict(initialize_body)
    wrong_headerless_initialize["params"] = {
        **cast(dict[str, Any], initialize_body["params"]),
        "protocolVersion": "2025-06-18",
    }
    wrong_version = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json=wrong_headerless_initialize,
    )
    assert wrong_version.status_code == 400
    assert wrong_version.json()["error"]["data"] == {
        "supported": [LEGACY_PROTOCOL],
        "requested": "2025-06-18",
    }

    unauthenticated = _legacy_post(client, "invalid", "tools/list")
    assert unauthenticated.status_code == 401
    oversized = client.post(
        "/mcp",
        headers=legacy_headers,
        content=b" " * (64 * 1024 + 1),
    )
    assert oversized.status_code == 413


def test_modern_and_legacy_share_one_authenticated_rate_bucket(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    app = cast(FastAPI, client.app)
    middleware = app.state.kodi_lab.mcp_transport.app
    assert isinstance(middleware, ModernMcpAuthMiddleware)
    middleware.authenticated = BoundedTokenBucketLimiter(
        rate=1.0,
        burst=2,
        maximum_buckets=4,
        idle_seconds=60,
        clock=lambda: 100.0,
    )
    assert _post(client, token, "tools/list", request_id=1).status_code == 200
    assert _legacy_post(client, token, "tools/list", request_id=2).status_code == 200
    limited = _legacy_post(client, token, "tools/list", request_id=3)
    assert limited.status_code == 429
    assert limited.json() == {"error": "rate_limited"}


def test_mcp_transport_host_allowlist_rejects_dns_rebinding_values(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    _client_id, token = _authority(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    call_entry = context.mcp_transport.server.get_request_handler("tools/call")
    list_entry = context.mcp_transport.server.get_request_handler("tools/list")
    assert call_entry is not None and list_entry is not None
    call_closure = inspect.getclosurevars(call_entry.handler).nonlocals
    list_closure = inspect.getclosurevars(list_entry.handler).nonlocals
    transport = build_mcp_http_transport(
        authentication=McpAuthenticationService(database),
        authorization=list_closure["authorization"],
        registry=build_mcp_tool_registry(),
        tools=call_closure["tools"],
        additional_allowed_hosts=("10.251.0.10", "10.251.0.10:*"),
    )
    sdk_app = cast(Any, transport.app)._app
    with TestClient(sdk_app, base_url="http://127.0.0.1") as direct:
        for index, host in enumerate(
            (
                "127.0.0.1",
                "127.0.0.1:8180",
                "localhost",
                "localhost:8180",
                "10.251.0.10",
                "10.251.0.10:8180",
            ),
            start=1,
        ):
            response = _post(
                direct,
                token,
                "server/discover",
                request_id=10_000 + index,
                extra_headers={"Host": host},
            )
            assert response.status_code == 200, host

        for index, host in enumerate(
            (
                "192.168.50.10",
                "192.168.50.135",
                "10.251.0.11",
                "8.8.8.8",
                "attacker.invalid",
                "10.251.0.10.evil",
                "[]",
                "*.example",
                "*",
            ),
            start=1,
        ):
            response = _post(
                direct,
                token,
                "server/discover",
                request_id=20_000 + index,
                extra_headers={"Host": host},
            )
            assert response.status_code == 421, host
            assert response.text == "Invalid Host header"


def test_web_mcp_administration_is_separate_show_once_and_csrf_protected(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    assert client.get("/mcp-clients", follow_redirects=False).status_code == 401
    assert client.get("/api/mcp/clients").status_code == 401
    csrf = login(client)
    presets = client.get("/api/mcp/presets").json()
    read_only = next(
        item for item in presets if item["display_name"] == "Read-only Inspection"
    )

    no_preset_csrf = client.post(
        "/api/mcp/presets",
        headers=ORIGIN,
        json={
            "display_name": "Custom Web preset",
            "template_preset_id": read_only["id"],
        },
    )
    assert no_preset_csrf.status_code == 403

    no_csrf = client.post(
        "/api/mcp/clients",
        headers=ORIGIN,
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert no_csrf.status_code == 403
    mutation_headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    custom_name = "Custom <script>alert(1)</script>"
    custom_response = client.post(
        "/api/mcp/presets",
        headers=mutation_headers,
        json={
            "display_name": f"  {custom_name}  ",
            "template_preset_id": read_only["id"],
        },
    )
    assert custom_response.status_code == 201
    custom = custom_response.json()
    assert custom["display_name"] == custom_name
    assert custom["builtin"] is False
    assert custom["permissions"] == ["target.inspect", "target.visual"]
    duplicate = client.post(
        "/api/mcp/presets",
        headers=mutation_headers,
        json={
            "display_name": custom_name,
            "template_preset_id": read_only["id"],
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "mcp_preset_name_conflict"
    custom_template = client.post(
        "/api/mcp/presets",
        headers=mutation_headers,
        json={
            "display_name": "Nested custom preset",
            "template_preset_id": custom["id"],
        },
    )
    assert custom_template.status_code == 409
    assert custom_template.json()["error"]["code"] == "mcp_preset_template_invalid"
    bad_origin = client.post(
        "/api/mcp/clients",
        headers={"Origin": "https://attacker.example", "X-Kodi-Lab-CSRF": csrf},
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert bad_origin.status_code == 403
    assert (
        client.get("/api/mcp/clients", headers={"Host": "attacker.example"}).status_code
        == 400
    )
    created = client.post(
        "/api/mcp/clients",
        headers=mutation_headers,
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert created.status_code == 201
    client_id = created.json()["id"]
    issue = client.post(
        f"/api/mcp/clients/{client_id}/credentials",
        headers=mutation_headers,
        json={"expiration_seconds": 3600},
    )
    assert issue.status_code == 201
    issued_payload = issue.json()
    token = issued_payload.pop("token")
    credential_id = issued_payload["credential"]["id"]
    assert len(base64.urlsafe_b64decode(token + "=")) == 32

    renamed = client.post(
        f"/api/mcp/clients/{client_id}/rename",
        headers=mutation_headers,
        json={"display_name": "Renamed Web client"},
    )
    assert renamed.json()["display_name"] == "Renamed Web client"
    assigned = client.post(
        f"/api/mcp/clients/{client_id}/preset",
        headers=mutation_headers,
        json={"preset_id": custom["id"]},
    )
    assert assigned.json()["preset"]["id"] == custom["id"]
    reassigned = client.post(
        f"/api/mcp/clients/{client_id}/preset",
        headers=mutation_headers,
        json={"preset_id": read_only["id"]},
    )
    assert reassigned.json()["preset"]["id"] == read_only["id"]
    policy = client.post(
        f"/api/mcp/target-policies/{INSTANCE_ID}",
        headers=mutation_headers,
        json={"preset_id": custom["id"]},
    )
    assert policy.json()["preset"]["id"] == custom["id"]
    policy = client.post(
        f"/api/mcp/target-policies/{INSTANCE_ID}",
        headers=mutation_headers,
        json={"preset_id": read_only["id"]},
    )
    assert policy.json()["preset"]["id"] == read_only["id"]
    granted = client.post(
        f"/api/mcp/clients/{client_id}/targets/{INSTANCE_ID}",
        headers=mutation_headers,
    )
    assert granted.json()["target_grants"] == [str(INSTANCE_ID)]
    removed = client.delete(
        f"/api/mcp/clients/{client_id}/targets/{INSTANCE_ID}",
        headers=mutation_headers,
    )
    assert removed.json()["target_grants"] == []
    rotated = client.post(
        f"/api/mcp/clients/{client_id}/credentials/{credential_id}/rotate",
        headers=mutation_headers,
        json={"expiration_seconds": 3600},
    )
    assert rotated.status_code == 200
    rotated_token = rotated.json()["token"]
    assert rotated_token != token
    rotated_id = rotated.json()["credential"]["id"]
    revoked = client.post(
        f"/api/mcp/clients/{client_id}/credentials/{rotated_id}/revoke",
        headers=mutation_headers,
    )
    assert revoked.json()["status"] == "revoked"
    disabled = client.post(
        f"/api/mcp/clients/{client_id}/enabled",
        headers=mutation_headers,
        json={"enabled": False},
    )
    assert disabled.json()["enabled"] is False

    detail_text = client.get(f"/api/mcp/clients/{client_id}").text
    page_text = client.get("/mcp-clients").text
    assert token not in detail_text
    assert token not in page_text
    assert rotated_token not in detail_text
    assert rotated_token not in page_text
    assert "token_digest" not in detail_text
    assert "token_digest" not in page_text
    assert "Non-expiring" in page_text
    assert "kodi-lab-mcp-stdio" in page_text
    assert "KODI_LAB_MCP_TOKEN" in page_text
    assert custom_name not in page_text
    assert "Custom &lt;script&gt;alert(1)&lt;/script&gt;" in page_text
    assert token not in page_text
    with database() as session:
        persisted = session.scalar(
            select(McpCredentialRow).where(McpCredentialRow.client_id == client_id)
        )
        assert persisted is not None
        assert persisted.token_digest not in detail_text
        event_kinds = set(session.scalars(select(McpSecurityEventRow.event_kind)))
    assert {
        "client_created",
        "preset_created",
        "client_renamed",
        "client_preset_assigned",
        "client_preset_unassigned",
        "credential_issued",
        "credential_rotated",
        "credential_revoked",
        "client_disabled",
        "target_preset_assigned",
        "target_preset_unassigned",
        "target_granted",
        "target_grant_removed",
    }.issubset(event_kinds)

    # A valid Web cookie is not an MCP bearer credential.
    cookie_only = _post(client, "not-a-token", "tools/list")
    assert cookie_only.status_code == 401
    client.cookies.clear()
    assert (
        client.get(
            "/api/mcp/clients", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )


def test_web_admin_source_grant_and_existing_workspace_binding(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = cast(FastAPI, client.app)
    context = app.state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.mcp.inspect"
    source_root.mkdir()
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.mcp.inspect" name="MCP Inspect" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("print('safe')\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="MCP inspection source",
        relative_locator="plugin.video.mcp.inspect",
    )
    queued = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(queued.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Workspace Web admin",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )

    grant_url = f"/api/mcp/clients/{mcp_client.id}/development-sources/{source.id}"
    assert (
        client.get(f"/api/mcp/clients/{mcp_client.id}/development-sources").status_code
        == 401
    )
    csrf = login(client)
    assert client.post(grant_url, headers=ORIGIN).status_code == 403
    mutation_headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    granted = client.post(grant_url, headers=mutation_headers)
    assert granted.status_code == 200
    assert granted.json()["development_source_grants"] == [str(source.id)]
    eligible = client.get(f"/api/mcp/clients/{mcp_client.id}/workspaces/eligible")
    assert eligible.status_code == 200
    assert [item["workspace_id"] for item in eligible.json()] == [
        str(queued.workspace_id)
    ]
    bind_url = (
        f"/api/mcp/clients/{mcp_client.id}/workspaces/{queued.workspace_id}/binding"
    )
    bound = client.post(bind_url, headers=mutation_headers)
    assert bound.status_code == 201
    assert bound.json()["binding_origin"] == "web_admin"
    assert bound.json()["owning_client_enabled"] is True
    assert bound.json()["retention_classification"] == "active"
    assert bound.json()["retention_deadline_at"] is None
    assert bound.json()["automatic_cleanup_blocked"] is False
    token = administration.issue_credential(mcp_client.id).token
    cast(Any, context.mcp_transport.development_read_limiter)._clock = lambda: 100.0
    caplog.set_level(logging.INFO, logger="kodi_lab.mcp")
    tool_calls: tuple[tuple[str, dict[str, object]], ...] = (
        ("development_source_list", {}),
        ("workspace_status", {"workspace_id": str(queued.workspace_id)}),
        ("workspace_tree", {"workspace_id": str(queued.workspace_id)}),
        (
            "workspace_read",
            {
                "workspace_id": str(queued.workspace_id),
                "relative_path": "main.py",
            },
        ),
        (
            "workspace_validate",
            {"workspace_id": str(queued.workspace_id), "expected_generation": 0},
        ),
    )
    for index, (tool_name, arguments) in enumerate(tool_calls, start=1):
        params = {"name": tool_name, "arguments": arguments}
        modern = _post(
            client, token, "tools/call", params=params, request_id=100 + index
        )
        legacy = _legacy_post(
            client, token, "tools/call", params=params, request_id=200 + index
        )
        assert modern.status_code == legacy.status_code == 200
        modern_result = modern.json()["result"]
        legacy_result = legacy.json()["result"]
        assert modern_result["isError"] is False
        modern_content = dict(modern_result["structuredContent"])
        legacy_content = dict(legacy_result["structuredContent"])
        if tool_name == "workspace_validate":
            modern_content.pop("validated_at")
            legacy_content.pop("validated_at")
        assert modern_content == legacy_content
    for request_id in range(10):
        admitted = _post(
            client,
            token,
            "tools/call",
            params={"name": "development_source_list", "arguments": {}},
            request_id=300 + request_id,
        ).json()["result"]
        assert admitted["isError"] is False
    limited = _post(
        client,
        token,
        "tools/call",
        params={"name": "development_source_list", "arguments": {}},
        request_id=399,
    ).json()["result"]
    assert limited["structuredContent"] == {"error": "rate_limited"}
    assert "print('safe')" not in caplog.text
    assert "main.py" not in caplog.text
    assert str(context.settings.development_root) not in caplog.text
    page = client.get("/mcp-clients")
    assert str(queued.workspace_id) in page.text
    assert "retention active" in page.text
    assert str(context.settings.development_root) not in page.text

    revoked = client.delete(grant_url, headers=mutation_headers)
    assert revoked.status_code == 200
    assert revoked.json()["development_source_grants"] == []
    bindings = client.get(f"/api/mcp/clients/{mcp_client.id}/workspace-bindings")
    assert [item["workspace_id"] for item in bindings.json()] == [
        str(queued.workspace_id)
    ]


def test_target_authority_recomputes_grants_and_preserves_uuid_privacy(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Reevaluation client",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    token = administration.issue_credential(mcp_client.id).token

    before = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {}},
    ).json()["result"]["structuredContent"]
    assert before["targets"] == []

    earlier_id = UUID("00000000-0000-4000-8000-000000000100")
    with database.begin() as session:
        session.add(
            TargetRow(
                id=str(earlier_id),
                name="Earlier lexical target",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(earlier_id),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
    administration.assign_target_preset(earlier_id, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(mcp_client.id, earlier_id)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    after = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {"limit": 1}},
    ).json()["result"]["structuredContent"]
    assert len(after["targets"]) == 1
    assert after["targets"][0]["target_id"] == str(earlier_id)
    assert after["next_cursor"] == str(earlier_id)
    second_page = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "target_list",
            "arguments": {"limit": 1, "cursor": after["next_cursor"]},
        },
    ).json()["result"]["structuredContent"]
    assert [item["target_id"] for item in second_page["targets"]] == [str(INSTANCE_ID)]
    assert second_page["next_cursor"] is None

    administration.remove_target_grant(mcp_client.id, INSTANCE_ID)
    errors = []
    for target_id in (INSTANCE_ID, uuid4()):
        response = _post(
            client,
            token,
            "tools/call",
            params={
                "name": "target_status",
                "arguments": {"target_id": str(target_id)},
            },
        )
        assert response.status_code == 200
        errors.append(response.json()["result"])
    assert [item["isError"] for item in errors] == [True, True]
    assert all(
        "target_not_found_or_not_authorized" in item["content"][0]["text"]
        for item in errors
    )
    legacy_errors = []
    for target_id in (INSTANCE_ID, uuid4()):
        response = _legacy_post(
            client,
            token,
            "tools/call",
            params={
                "name": "target_status",
                "arguments": {"target_id": str(target_id)},
            },
        )
        assert response.status_code == 200
        legacy_errors.append(response.json()["result"])
    assert [item["structuredContent"] for item in legacy_errors] == [
        {"error": "target_not_found_or_not_authorized"},
        {"error": "target_not_found_or_not_authorized"},
    ]


def test_workspace_mutation_modern_legacy_replay_and_generation_parity(
    client: TestClient,
    database: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = cast(FastAPI, client.app)
    context = app.state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.mcp.mutation"
    (source_root / "resources").mkdir(parents=True)
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.mcp.mutation" name="Mutation" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("old\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="MCP mutation source",
        relative_locator="plugin.video.mcp.mutation",
    )
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Workspace mutation parity",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(mcp_client.id, source.id)
    token = administration.issue_credential(mcp_client.id).token
    caplog.set_level(logging.INFO, logger="kodi_lab.mcp")
    request_id = str(uuid4())
    arguments = {"request_id": request_id, "source_id": str(source.id)}

    modern = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_create", "arguments": arguments},
        request_id=501,
    ).json()["result"]
    legacy = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_create", "arguments": arguments},
        request_id=502,
    ).json()["result"]
    assert modern["isError"] is legacy["isError"] is False
    assert (
        modern["structuredContent"]["workspace_id"]
        == legacy["structuredContent"]["workspace_id"]
    )
    assert (
        modern["structuredContent"]["creation_operation_id"]
        == legacy["structuredContent"]["creation_operation_id"]
    )
    created = modern["structuredContent"]
    workspace_id = UUID(created["workspace_id"])
    operation_id = UUID(created["creation_operation_id"])
    deadline = time.monotonic() + 5
    while context.agent_workspaces.creation_status(operation_id).state != "succeeded":
        if time.monotonic() >= deadline:
            raise AssertionError("workspace creation did not complete")
        time.sleep(0.01)
    with database() as session:
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )

    edit_calls = (
        (
            _post,
            {
                "workspace_id": str(workspace_id),
                "expected_generation": 0,
                "operation": "create",
                "relative_path": "resources/private-edit-name.py",
                "content": "secret-mcp-edit-payload\n",
            },
            1,
        ),
        (
            _legacy_post,
            {
                "workspace_id": str(workspace_id),
                "expected_generation": 1,
                "operation": "replace",
                "relative_path": "resources/private-edit-name.py",
                "content": "two\n",
            },
            2,
        ),
        (
            _post,
            {
                "workspace_id": str(workspace_id),
                "expected_generation": 2,
                "operation": "exact_patch",
                "relative_path": "resources/private-edit-name.py",
                "expected_text": "two\n",
                "replacement_text": "three\n",
            },
            3,
        ),
    )
    for caller, edit, generation in edit_calls:
        response = caller(
            client,
            token,
            "tools/call",
            params={"name": "workspace_edit", "arguments": edit},
            request_id=510 + generation,
        ).json()["result"]
        assert response["isError"] is False
        assert response["structuredContent"]["new_generation"] == generation
    read = _legacy_post(
        client,
        token,
        "tools/call",
        params={
            "name": "workspace_read",
            "arguments": {
                "workspace_id": str(workspace_id),
                "relative_path": "resources/private-edit-name.py",
            },
        },
        request_id=520,
    ).json()["result"]
    assert read["structuredContent"]["text"] == "three\n"
    assert read["structuredContent"]["observed_generation"] == 3
    validation_arguments = {
        "workspace_id": str(workspace_id),
        "expected_generation": 3,
    }
    modern_validation = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_validate", "arguments": validation_arguments},
        request_id=521,
    ).json()["result"]
    legacy_validation = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_validate", "arguments": validation_arguments},
        request_id=522,
    ).json()["result"]
    assert modern_validation["isError"] is legacy_validation["isError"] is False
    modern_content = dict(modern_validation["structuredContent"])
    legacy_content = dict(legacy_validation["structuredContent"])
    modern_content.pop("validated_at")
    legacy_content.pop("validated_at")
    assert modern_content == legacy_content
    assert modern_validation["structuredContent"]["changed_file_count"] == 1
    assert "private-edit-name.py" not in caplog.text
    assert "secret-mcp-edit-payload" not in caplog.text


def test_workspace_edit_rate_class_is_two_per_second_with_burst_four(
    client: TestClient,
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    limiter = context.mcp_transport.development_edit_limiter
    now = [100.0]
    cast(Any, limiter)._clock = lambda: now[0]
    assert [limiter.allow("client") for _index in range(4)] == [True] * 4
    assert limiter.allow("client") is False
    now[0] += 0.5
    assert limiter.allow("client") is True


def test_workspace_deploy_execute_modern_legacy_schema_and_replay(
    client: TestClient,
) -> None:
    _make_target_ready(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.mcp.slice4"
    source_root.mkdir(parents=True)
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.mcp.slice4" name="Slice 4" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("pass\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="MCP Slice 4 source",
        relative_locator="plugin.video.mcp.slice4",
    )
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Slice 4 protocol replay",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(mcp_client.id, source.id)
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    token = administration.issue_credential(mcp_client.id).token
    created = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "workspace_create",
            "arguments": {
                "request_id": str(uuid4()),
                "source_id": str(source.id),
            },
        },
    ).json()["result"]["structuredContent"]
    workspace_id = UUID(created["workspace_id"])
    operation_id = UUID(created["creation_operation_id"])
    deadline = time.monotonic() + 5
    while context.agent_workspaces.creation_status(operation_id).state != "succeeded":
        if time.monotonic() >= deadline:
            raise AssertionError("workspace creation did not complete")
        time.sleep(0.01)

    tools = _post(client, token, "tools/list").json()["result"]["tools"]
    for name in ("promotion_request", "workspace_deploy", "workspace_execute"):
        schema = next(item for item in tools if item["name"] == name)["inputSchema"]
        assert set(schema["properties"]) == {
            "request_id",
            "workspace_id",
            "expected_generation",
            "target_id",
        }
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False
    assert "promotion_approve" not in {item["name"] for item in tools}

    deployment_calls = 0
    execution_calls = 0
    deployments: dict[UUID, WorkspaceDeploymentResult] = {}
    executions: dict[UUID, WorkspaceExecutionResult] = {}

    def deploy(**kwargs: Any) -> WorkspaceDeploymentResult:
        nonlocal deployment_calls
        request_id = cast(UUID, kwargs["request_id"])
        if request_id not in deployments:
            deployment_calls += 1
            deployments[request_id] = WorkspaceDeploymentResult(
                request_id=request_id,
                deployment_id=uuid4(),
                workspace_id=workspace_id,
                source_id=source.id,
                target_id=INSTANCE_ID,
                generation=0,
                addon_id="plugin.video.mcp.slice4",
                addon_version="1.0.0",
                state="succeeded",
                result_classification="synchronized",
                inventory_classification="matched",
                addon_enabled=True,
                started_at=datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
                completed_at=datetime(2026, 9, 20, 18, 0, 1, tzinfo=UTC),
            )
        return deployments[request_id]

    def execute(**kwargs: Any) -> WorkspaceExecutionResult:
        nonlocal execution_calls
        request_id = cast(UUID, kwargs["request_id"])
        if request_id not in executions:
            execution_calls += 1
            executions[request_id] = WorkspaceExecutionResult(
                request_id=request_id,
                execution_id=uuid4(),
                deployment_id=next(iter(deployments.values())).deployment_id,
                workspace_id=workspace_id,
                source_id=source.id,
                target_id=INSTANCE_ID,
                generation=0,
                addon_id="plugin.video.mcp.slice4",
                addon_version="1.0.0",
                state="succeeded",
                execution_classification="directory_result",
                result_classification="smoke_passed",
                item_count=1,
                started_at=datetime(2026, 9, 20, 18, 1, tzinfo=UTC),
                completed_at=datetime(2026, 9, 20, 18, 1, 1, tzinfo=UTC),
            )
        return executions[request_id]

    context.mcp_workspace_development.deploy = deploy
    context.mcp_workspace_development.execute = execute
    arguments = {
        "request_id": str(uuid4()),
        "workspace_id": str(workspace_id),
        "expected_generation": 0,
        "target_id": str(INSTANCE_ID),
    }
    modern_deploy = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_deploy", "arguments": arguments},
    ).json()["result"]
    legacy_deploy = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_deploy", "arguments": arguments},
    ).json()["result"]
    assert modern_deploy["structuredContent"] == legacy_deploy["structuredContent"]
    assert deployment_calls == 1

    other_client = administration.create_client(
        display_name="Slice 4 isolated client",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(other_client.id, source.id)
    administration.grant_target(other_client.id, INSTANCE_ID)
    other_token = administration.issue_credential(other_client.id).token
    isolated = _post(
        client,
        other_token,
        "tools/call",
        params={"name": "workspace_deploy", "arguments": arguments},
    ).json()["result"]
    assert isolated["structuredContent"] == {
        "error": "workspace_not_found_or_not_authorized"
    }
    assert deployment_calls == 1

    execute_arguments = {**arguments, "request_id": str(uuid4())}
    modern_execute = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_execute", "arguments": execute_arguments},
    ).json()["result"]
    legacy_execute = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_execute", "arguments": execute_arguments},
    ).json()["result"]
    assert modern_execute["structuredContent"] == legacy_execute["structuredContent"]
    assert execution_calls == 1

    reverse_arguments = {**arguments, "request_id": str(uuid4())}
    legacy_first = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_execute", "arguments": reverse_arguments},
    ).json()["result"]
    modern_replay = _post(
        client,
        token,
        "tools/call",
        params={"name": "workspace_execute", "arguments": reverse_arguments},
    ).json()["result"]
    assert legacy_first["structuredContent"] == modern_replay["structuredContent"]
    assert execution_calls == 2

    promotion_calls = 0
    promotion_results: dict[UUID, PromotionRequestResult] = {}

    def request_promotion(**kwargs: Any) -> PromotionRequestResult:
        nonlocal promotion_calls
        request_id = cast(UUID, kwargs["request_id"])
        if request_id not in promotion_results:
            promotion_calls += 1
            promotion_results[request_id] = PromotionRequestResult(
                request_id=request_id,
                promotion_plan_id=uuid4(),
                workspace_id=workspace_id,
                source_id=source.id,
                target_id=INSTANCE_ID,
                generation=0,
                review_state="reviewable",
                changed_file_count=1,
                added_file_count=0,
                modified_file_count=1,
                candidate_version="1.0.0",
                expires_at=datetime(2026, 9, 21, 18, tzinfo=UTC),
                readiness_classification="ready_for_web_review",
                conflict_classification="no_conflict",
            )
        return promotion_results[request_id]

    context.mcp_promotion_requests.request = request_promotion
    promotion_arguments = {**arguments, "request_id": str(uuid4())}
    modern_promotion = _post(
        client,
        token,
        "tools/call",
        params={"name": "promotion_request", "arguments": promotion_arguments},
    ).json()["result"]
    legacy_promotion = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "promotion_request", "arguments": promotion_arguments},
    ).json()["result"]
    assert (
        modern_promotion["structuredContent"] == legacy_promotion["structuredContent"]
    )
    assert promotion_calls == 1

    isolated_promotion = _post(
        client,
        other_token,
        "tools/call",
        params={"name": "promotion_request", "arguments": promotion_arguments},
    ).json()["result"]
    assert isolated_promotion["structuredContent"] == {
        "error": "workspace_not_found_or_not_authorized"
    }
    assert promotion_calls == 1

    administration.remove_development_source_grant(mcp_client.id, source.id)
    revoked_source = _post(
        client,
        token,
        "tools/call",
        params={"name": "promotion_request", "arguments": promotion_arguments},
    ).json()["result"]
    assert revoked_source["structuredContent"] == {
        "error": "workspace_not_found_or_not_authorized"
    }
    administration.grant_development_source(mcp_client.id, source.id)
    administration.remove_target_grant(mcp_client.id, INSTANCE_ID)
    revoked_target = _post(
        client,
        token,
        "tools/call",
        params={"name": "promotion_request", "arguments": promotion_arguments},
    ).json()["result"]
    assert revoked_target["structuredContent"] == {
        "error": "target_not_found_or_not_authorized"
    }
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    administration.set_client_enabled(mcp_client.id, False)
    assert (
        _post(
            client,
            token,
            "tools/call",
            params={"name": "promotion_request", "arguments": promotion_arguments},
        ).status_code
        == 401
    )
    assert promotion_calls == 1

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import ImageContent
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_promotion_requests import PromotionRequestResult
from kodi_lab.application.mcp_workspace_development import (
    WorkspaceDeploymentResult,
    WorkspaceExecutionResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import PNG_SIGNATURE, validate_png
from kodi_lab.mcp.stdio_proxy import (
    ENDPOINT_ENVIRONMENT_VARIABLE,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from kodi_lab.storage.models import McpCredentialRow, McpPresetRow

from .conftest import (
    EXTERNAL_TARGET_ID,
    INSTANCE_ID,
    ORIGIN,
    FakeBroker,
    FakeExternalKodi,
    login,
    seed_external_target,
)

STDIO_EXECUTABLE = Path(sys.executable).with_name("kodi-lab-mcp-stdio")


class _CaptureModernRequests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.methods: list[str] = []
        self.request_headers: list[dict[str, str]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            body.extend(message.get("body", b""))
            more = message.get("more_body", False)
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = {}
        if isinstance(decoded, dict) and isinstance(decoded.get("method"), str):
            self.methods.append(cast(str, decoded["method"]))
        self.request_headers.append(
            {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
        )
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


@contextmanager
def _live_loopback_server(app: ASGIApp) -> Iterator[tuple[str, _CaptureModernRequests]]:
    capture = _CaptureModernRequests(app)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = cast(tuple[str, int], listener.getsockname())[1]
    server = uvicorn.Server(
        uvicorn.Config(
            capture,
            log_config=None,
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=False
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            raise RuntimeError("loopback test server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp", capture
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("loopback test server did not stop")


def _administration(client: TestClient) -> McpAdministrationService:
    app = cast(FastAPI, client.app)
    return cast(McpAdministrationService, app.state.kodi_lab.mcp_administration)


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
        status = client.get(f"/api/instances/{INSTANCE_ID}/visual-status").json()
        if status["screenshot_available_now"]:
            return
        time.sleep(0.01)
    raise AssertionError("managed target did not become ready")


@asynccontextmanager
async def _direct_client(endpoint: str, token: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
    ) as http:
        streams = streamable_http_client(
            endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="auto", cache=None) as client:
            yield client


def test_official_sdk_http_stdio_equivalence_and_next_request_authority(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    _make_target_ready(client)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Stdio equivalence",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    issued = administration.issue_credential(mcp_client.id)

    with _live_loopback_server(client.app) as (endpoint, capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: issued.token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, issued.token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                assert direct.protocol_version == stdio.protocol_version == "2026-07-28"
                http_tools = await direct.list_tools()
                stdio_tools = await stdio.list_tools()
                assert [item.model_dump() for item in http_tools.tools] == [
                    item.model_dump() for item in stdio_tools.tools
                ]
                assert [item.name for item in stdio_tools.tools] == [
                    "addon_list",
                    "screenshot_capture",
                    "target_list",
                    "target_status",
                ]

                http_list, stdio_list = await asyncio.gather(
                    direct.call_tool("target_list", {}),
                    stdio.call_tool("target_list", {}),
                )
                assert http_list.structured_content == stdio_list.structured_content
                http_status, stdio_status = await asyncio.gather(
                    direct.call_tool("target_status", {"target_id": str(INSTANCE_ID)}),
                    stdio.call_tool("target_status", {"target_id": str(INSTANCE_ID)}),
                )
                assert http_status.structured_content == stdio_status.structured_content
                assert not stdio_status.is_error

                http_addons, stdio_addons = await asyncio.gather(
                    direct.call_tool("addon_list", {"target_id": str(INSTANCE_ID)}),
                    stdio.call_tool("addon_list", {"target_id": str(INSTANCE_ID)}),
                )
                assert http_addons.structured_content == stdio_addons.structured_content
                assert not stdio_addons.is_error

                http_screenshot = await direct.call_tool(
                    "screenshot_capture", {"target_id": str(INSTANCE_ID)}
                )
                stdio_screenshot = await stdio.call_tool(
                    "screenshot_capture", {"target_id": str(INSTANCE_ID)}
                )
                assert not http_screenshot.is_error and not stdio_screenshot.is_error
                assert (
                    len(http_screenshot.content) == len(stdio_screenshot.content) == 1
                )
                http_image = http_screenshot.content[0]
                stdio_image = stdio_screenshot.content[0]
                assert isinstance(http_image, ImageContent)
                assert isinstance(stdio_image, ImageContent)
                assert http_image.mime_type == stdio_image.mime_type == "image/png"
                http_png = base64.b64decode(http_image.data, validate=True)
                stdio_png = base64.b64decode(stdio_image.data, validate=True)
                assert http_png == stdio_png
                assert http_png.startswith(PNG_SIGNATURE)
                validated = validate_png(http_png)
                for result in (http_screenshot, stdio_screenshot):
                    metadata = cast(dict[str, object], result.structured_content)
                    assert metadata["target_id"] == str(INSTANCE_ID)
                    assert metadata["mime_type"] == "image/png"
                    assert (metadata["width"], metadata["height"]) == (
                        validated.width,
                        validated.height,
                    )
                    assert metadata["byte_size"] == validated.byte_size
                assert broker.screenshot_calls == 2

                unknown = str(uuid4())
                http_unknown, stdio_unknown = await asyncio.gather(
                    direct.call_tool("target_status", {"target_id": unknown}),
                    stdio.call_tool("target_status", {"target_id": unknown}),
                )
                assert (
                    http_unknown.structured_content == stdio_unknown.structured_content
                )
                assert stdio_unknown.is_error

                administration.remove_target_grant(mcp_client.id, INSTANCE_ID)
                http_removed, stdio_removed = await asyncio.gather(
                    direct.call_tool("target_status", {"target_id": str(INSTANCE_ID)}),
                    stdio.call_tool("target_status", {"target_id": str(INSTANCE_ID)}),
                )
                assert (
                    http_removed.structured_content == stdio_removed.structured_content
                )
                assert stdio_removed.is_error
                administration.grant_target(mcp_client.id, INSTANCE_ID)
                assert not (
                    await stdio.call_tool(
                        "target_status", {"target_id": str(INSTANCE_ID)}
                    )
                ).is_error

                deny_preset_id = uuid4()
                with database.begin() as session:
                    session.add(
                        McpPresetRow(
                            id=str(deny_preset_id),
                            display_name="Stdio test deny policy",
                            builtin=False,
                            assignable=True,
                        )
                    )
                administration.assign_client_preset(mcp_client.id, deny_preset_id)
                assert (
                    await stdio.call_tool(
                        "target_status", {"target_id": str(INSTANCE_ID)}
                    )
                ).is_error
                administration.assign_client_preset(
                    mcp_client.id, READ_ONLY_INSPECTION_PRESET_ID
                )
                assert not (
                    await stdio.call_tool(
                        "target_status", {"target_id": str(INSTANCE_ID)}
                    )
                ).is_error

                administration.assign_target_preset(INSTANCE_ID, deny_preset_id)
                assert (
                    await stdio.call_tool(
                        "target_status", {"target_id": str(INSTANCE_ID)}
                    )
                ).is_error
                administration.assign_target_preset(
                    INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID
                )
                assert not (
                    await stdio.call_tool(
                        "target_status", {"target_id": str(INSTANCE_ID)}
                    )
                ).is_error

                administration.set_client_enabled(mcp_client.id, False)
                with pytest.raises(MCPError):
                    await direct.list_tools()
                with pytest.raises(MCPError):
                    await stdio.list_tools()
                administration.set_client_enabled(mcp_client.id, True)
                assert [item.name for item in (await stdio.list_tools()).tools] == [
                    "addon_list",
                    "screenshot_capture",
                    "target_list",
                    "target_status",
                ]

                with database.begin() as session:
                    row = session.get(McpCredentialRow, str(issued.credential.id))
                    assert row is not None
                    original_created = row.created_at
                    original_expiry = row.expires_at
                    row.created_at = "2000-01-01T00:00:00.000000Z"
                    row.expires_at = "2000-01-01T00:00:00.000001Z"
                with pytest.raises(MCPError):
                    await direct.list_tools()
                with pytest.raises(MCPError):
                    await stdio.list_tools()
                with database.begin() as session:
                    row = session.get(McpCredentialRow, str(issued.credential.id))
                    assert row is not None
                    row.created_at = original_created
                    row.expires_at = original_expiry

                administration.revoke_credential(mcp_client.id, issued.credential.id)
                with pytest.raises(MCPError):
                    await direct.list_tools()
                with pytest.raises(MCPError):
                    await stdio.list_tools()

        asyncio.run(exercise())

        async def malformed_token_fails_generically() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: "malformed-token",
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with Client(parameters, mode="2026-07-28", cache=None) as stdio:
                with pytest.raises(MCPError, match="authority rejected"):
                    await stdio.list_tools()

        asyncio.run(malformed_token_fails_generically())

    assert "server/discover" in capture.methods
    assert "tools/list" in capture.methods
    assert "tools/call" in capture.methods
    assert "initialize" not in capture.methods
    assert "notifications/initialized" not in capture.methods
    assert all("mcp-session-id" not in headers for headers in capture.request_headers)


def test_remote_action_replay_is_shared_across_http_and_stdio(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    _make_target_ready(client)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Cross-transport control",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    issued = administration.issue_credential(mcp_client.id)

    with _live_loopback_server(client.app) as (endpoint, _capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: issued.token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, issued.token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                assert [item.name for item in (await direct.list_tools()).tools] == [
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
                http_request = str(uuid4())
                arguments = {
                    "request_id": http_request,
                    "target_id": str(INSTANCE_ID),
                    "action": "volume_up",
                }
                first = await direct.call_tool("remote_action", arguments)
                replay = await stdio.call_tool("remote_action", arguments)
                assert not first.is_error and not replay.is_error
                assert first.structured_content == replay.structured_content
                assert broker.remote_calls == [BrowserRemoteAction.VOLUME_UP]

                stdio_request = str(uuid4())
                reverse_arguments = {
                    "request_id": stdio_request,
                    "target_id": str(INSTANCE_ID),
                    "action": "volume_down",
                }
                reverse_first = await stdio.call_tool(
                    "remote_action", reverse_arguments
                )
                reverse_replay = await direct.call_tool(
                    "remote_action", reverse_arguments
                )
                assert not reverse_first.is_error and not reverse_replay.is_error
                assert (
                    reverse_first.structured_content
                    == reverse_replay.structured_content
                )
                assert broker.remote_calls == [
                    BrowserRemoteAction.VOLUME_UP,
                    BrowserRemoteAction.VOLUME_DOWN,
                ]

        asyncio.run(exercise())


def test_external_target_http_stdio_status_inventory_and_replay_parity(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External stdio parity",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    with _live_loopback_server(client.app) as (endpoint, _capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                for tool in ("target_status", "addon_list"):
                    arguments = {"target_id": str(EXTERNAL_TARGET_ID)}
                    direct_result = await direct.call_tool(tool, arguments)
                    stdio_result = await stdio.call_tool(tool, arguments)
                    assert not direct_result.is_error and not stdio_result.is_error
                    direct_content = dict(direct_result.structured_content or {})
                    stdio_content = dict(stdio_result.structured_content or {})
                    direct_content.pop("observed_at", None)
                    stdio_content.pop("observed_at", None)
                    assert direct_content == stdio_content

                remote_arguments = {
                    "request_id": str(uuid4()),
                    "target_id": str(EXTERNAL_TARGET_ID),
                    "action": "home",
                }
                first = await direct.call_tool("remote_action", remote_arguments)
                replay = await stdio.call_tool("remote_action", remote_arguments)
                assert not first.is_error and not replay.is_error
                assert first.structured_content == replay.structured_content

        asyncio.run(exercise())

    assert external_kodi.mutations == 1


def test_development_inspection_is_identical_over_modern_http_and_stdio(
    client: TestClient,
) -> None:
    app = cast(FastAPI, client.app)
    context = app.state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.stdio.inspect"
    source_root.mkdir()
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.stdio.inspect" name="Stdio" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("one\ntwo\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="Stdio inspection source",
        relative_locator="plugin.video.stdio.inspect",
    )
    queued = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(queued.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Stdio workspace inspection",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(mcp_client.id, source.id)
    administration.bind_workspace(mcp_client.id, queued.workspace_id)
    issued = administration.issue_credential(mcp_client.id)

    with _live_loopback_server(client.app) as (endpoint, _capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: issued.token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, issued.token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                assert [item.name for item in (await stdio.list_tools()).tools] == [
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
                calls: tuple[tuple[str, dict[str, str]], ...] = (
                    ("development_source_list", {}),
                    (
                        "workspace_status",
                        {"workspace_id": str(queued.workspace_id)},
                    ),
                    ("workspace_tree", {"workspace_id": str(queued.workspace_id)}),
                    (
                        "workspace_read",
                        {
                            "workspace_id": str(queued.workspace_id),
                            "relative_path": "main.py",
                        },
                    ),
                )
                for name, arguments in calls:
                    http_result, stdio_result = await asyncio.gather(
                        direct.call_tool(name, arguments),
                        stdio.call_tool(name, arguments),
                    )
                    assert not http_result.is_error and not stdio_result.is_error
                    assert (
                        http_result.structured_content
                        == stdio_result.structured_content
                    )

        asyncio.run(exercise())


def test_stdio_first_workspace_creation_replays_over_http_and_edits_share_cas(
    client: TestClient,
) -> None:
    app = cast(FastAPI, client.app)
    context = app.state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.stdio.mutation"
    (source_root / "resources").mkdir(parents=True)
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.stdio.mutation" name="Stdio" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("old\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="Stdio mutation source",
        relative_locator="plugin.video.stdio.mutation",
    )
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Stdio workspace mutation",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(mcp_client.id, source.id)
    issued = administration.issue_credential(mcp_client.id)

    with _live_loopback_server(client.app) as (endpoint, _capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: issued.token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, issued.token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                request_id = str(uuid4())
                arguments = {
                    "request_id": request_id,
                    "source_id": str(source.id),
                }
                first = await stdio.call_tool("workspace_create", arguments)
                replay = await direct.call_tool("workspace_create", arguments)
                assert not first.is_error and not replay.is_error
                assert first.structured_content is not None
                assert replay.structured_content is not None
                assert (
                    first.structured_content["workspace_id"]
                    == replay.structured_content["workspace_id"]
                )
                assert (
                    first.structured_content["creation_operation_id"]
                    == replay.structured_content["creation_operation_id"]
                )
                workspace_id = first.structured_content["workspace_id"]
                operation_id = first.structured_content["creation_operation_id"]
                deadline = time.monotonic() + 5
                while (
                    context.agent_workspaces.creation_status(UUID(operation_id)).state
                    != "succeeded"
                ):
                    if time.monotonic() >= deadline:
                        raise AssertionError("workspace creation did not complete")
                    await asyncio.sleep(0.01)

                created = await stdio.call_tool(
                    "workspace_edit",
                    {
                        "workspace_id": workspace_id,
                        "expected_generation": 0,
                        "operation": "create",
                        "relative_path": "resources/new.py",
                        "content": "one\n",
                    },
                )
                read = await direct.call_tool(
                    "workspace_read",
                    {
                        "workspace_id": workspace_id,
                        "relative_path": "resources/new.py",
                    },
                )
                replaced = await direct.call_tool(
                    "workspace_edit",
                    {
                        "workspace_id": workspace_id,
                        "expected_generation": 1,
                        "operation": "replace",
                        "relative_path": "resources/new.py",
                        "content": "two\n",
                    },
                )
                patched = await stdio.call_tool(
                    "workspace_edit",
                    {
                        "workspace_id": workspace_id,
                        "expected_generation": 2,
                        "operation": "exact_patch",
                        "relative_path": "resources/new.py",
                        "expected_text": "two\n",
                        "replacement_text": "three\n",
                    },
                )
                assert all(
                    not result.is_error for result in (created, read, replaced, patched)
                )
                assert read.structured_content is not None
                assert read.structured_content["text"] == "one\n"
                assert patched.structured_content is not None
                assert patched.structured_content["new_generation"] == 3
                validation_arguments = {
                    "workspace_id": workspace_id,
                    "expected_generation": 3,
                }
                stdio_validation = await stdio.call_tool(
                    "workspace_validate", validation_arguments
                )
                direct_validation = await direct.call_tool(
                    "workspace_validate", validation_arguments
                )
                assert not stdio_validation.is_error
                assert not direct_validation.is_error
                stdio_content = dict(stdio_validation.structured_content or {})
                direct_content = dict(direct_validation.structured_content or {})
                stdio_content.pop("validated_at")
                direct_content.pop("validated_at")
                assert stdio_content == direct_content

        asyncio.run(exercise())


def test_stdio_process_missing_token_and_eof_are_clean_and_secret_free() -> None:
    assert STDIO_EXECUTABLE.is_file()
    command = [str(STDIO_EXECUTABLE)]
    environment = dict(os.environ)
    environment.pop(TOKEN_ENVIRONMENT_VARIABLE, None)
    missing = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=environment,
        input=b"",
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stdout == b""
    assert b"missing or malformed" in missing.stderr

    secret = "stdio-eof-secret"
    environment[TOKEN_ENVIRONMENT_VARIABLE] = secret
    process = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stdout is not None and process.stdout.read() == b""
    assert process.stderr is not None
    stderr = process.stderr.read()
    assert secret.encode() not in stderr


def test_workspace_deploy_execute_replay_is_shared_across_stdio_and_http(
    client: TestClient,
) -> None:
    _make_target_ready(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    source_root = context.settings.development_root / "plugin.video.mcp.stdio.slice4"
    source_root.mkdir(parents=True)
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.mcp.stdio.slice4" name="Slice 4" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("pass\n", encoding="utf-8")
    source = context.development_source_administration.register_source(
        logical_name="MCP stdio Slice 4 source",
        relative_locator="plugin.video.mcp.stdio.slice4",
    )
    queued = context.agent_workspaces.queue_creation(source.id)
    lease = context.agent_workspaces.acquire(queued.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Slice 4 stdio replay",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.grant_development_source(mcp_client.id, source.id)
    administration.bind_workspace(mcp_client.id, queued.workspace_id)
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    issued = administration.issue_credential(mcp_client.id)
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
                workspace_id=queued.workspace_id,
                source_id=source.id,
                target_id=INSTANCE_ID,
                generation=0,
                addon_id="plugin.video.mcp.stdio.slice4",
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
                workspace_id=queued.workspace_id,
                source_id=source.id,
                target_id=INSTANCE_ID,
                generation=0,
                addon_id="plugin.video.mcp.stdio.slice4",
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
                workspace_id=queued.workspace_id,
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
    with _live_loopback_server(client.app) as (endpoint, _capture):

        async def exercise() -> None:
            parameters = StdioServerParameters(
                command=str(STDIO_EXECUTABLE),
                env={
                    TOKEN_ENVIRONMENT_VARIABLE: issued.token,
                    ENDPOINT_ENVIRONMENT_VARIABLE: endpoint,
                },
                cwd=Path.cwd(),
            )
            async with (
                _direct_client(endpoint, issued.token) as direct,
                Client(parameters, mode="auto", cache=None) as stdio,
            ):
                arguments = {
                    "request_id": str(uuid4()),
                    "workspace_id": str(queued.workspace_id),
                    "expected_generation": 0,
                    "target_id": str(INSTANCE_ID),
                }
                first = await stdio.call_tool("workspace_deploy", arguments)
                replay = await direct.call_tool("workspace_deploy", arguments)
                assert not first.is_error and not replay.is_error
                assert first.structured_content == replay.structured_content
                execution_arguments = {**arguments, "request_id": str(uuid4())}
                executed = await stdio.call_tool(
                    "workspace_execute", execution_arguments
                )
                execution_replay = await direct.call_tool(
                    "workspace_execute", execution_arguments
                )
                assert not executed.is_error and not execution_replay.is_error
                assert (
                    executed.structured_content == execution_replay.structured_content
                )
                promotion_arguments = {**arguments, "request_id": str(uuid4())}
                promoted = await stdio.call_tool(
                    "promotion_request", promotion_arguments
                )
                promotion_replay = await direct.call_tool(
                    "promotion_request", promotion_arguments
                )
                assert not promoted.is_error and not promotion_replay.is_error
                assert (
                    promoted.structured_content == promotion_replay.structured_content
                )

        asyncio.run(exercise())
    assert deployment_calls == 1
    assert execution_calls == 1
    assert promotion_calls == 1

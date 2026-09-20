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
from pathlib import Path
from typing import cast
from uuid import uuid4

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
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.kodi.screenshots import PNG_SIGNATURE, validate_png
from kodi_lab.mcp.stdio_proxy import (
    ENDPOINT_ENVIRONMENT_VARIABLE,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from kodi_lab.storage.models import McpCredentialRow, McpPresetRow

from .conftest import INSTANCE_ID, ORIGIN, FakeBroker, login

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

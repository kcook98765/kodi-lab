from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
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
from mcp.types import TextContent
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import OperationReply
from kodi_lab.mcp.stdio_proxy import (
    ENDPOINT_ENVIRONMENT_VARIABLE,
    TOKEN_ENVIRONMENT_VARIABLE,
)
from tests.experiments.test_managed_v2 import package

from .conftest import (
    EXTERNAL_TARGET_ID,
    FakeExternalKodi,
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


def test_managed_workspace_inspection_is_identical_over_http_and_stdio(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    administration = _administration(client)
    row = administration.create_client(
        display_name="Managed stdio workspace inspection",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
        trusted_managed_lab=True,
    )
    owner, token = str(row.id), administration.issue_credential(row.id).token
    artifact = controller.packages.approve(package())
    controller.upload_package(artifact.data, artifact.digest, actor=owner)
    controller.perform("workspace_open", {"digest": artifact.digest}, actor=owner)

    def dispatch(
        _broker: BrokerClient, actor: str, operation: str, args: Mapping[str, str]
    ) -> OperationReply:
        assert actor == owner
        return controller.perform(operation, dict(args), actor=actor)

    monkeypatch.setattr(BrokerClient, "request", dispatch)
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
                listed = (await stdio.list_tools()).tools
                assert listed == (await direct.list_tools()).tools
                names = {item.name for item in listed}
                assert {"managed_workspace_tree", "managed_workspace_read"} <= names
                assert {
                    "workspace_tree",
                    "workspace_read",
                    "managed_select",
                }.isdisjoint(names)
                for name, arguments, expected in (
                    (
                        "managed_workspace_tree",
                        {"addon": artifact.id},
                        ["addon.xml", "default.py"],
                    ),
                    (
                        "managed_workspace_read",
                        {"addon": artifact.id, "path": "default.py"},
                        "print('inert fixture')",
                    ),
                ):
                    for transport in (direct, stdio):
                        result = await transport.call_tool(name, arguments)
                        assert not result.is_error
                        content = result.content[0]
                        assert isinstance(content, TextContent)
                        value = json.loads(content.text)["result"]
                        assert (
                            value["content"] if isinstance(value, dict) else value
                        ) == expected

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

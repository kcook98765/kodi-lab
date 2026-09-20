"""Authenticated primary-modern and bounded-legacy MCP HTTP integration."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs
from uuid import UUID

from mcp.server.caching import CacheHint
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)
from mcp_types.jsonrpc import (
    HEADER_MISMATCH,
    METHOD_NOT_FOUND,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab.application.mcp_administration import (
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_remote_actions import McpRemoteActionResult
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION, McpToolRegistry
from kodi_lab.mcp.tools import (
    AddonListResult,
    DevelopmentSourceListResult,
    McpTargetToolService,
    ScreenshotCaptureMetadata,
    ScreenshotToolResult,
    TargetListResult,
    TargetStatusResult,
    WorkspaceReadResult,
    WorkspaceStatusResult,
    WorkspaceTreeResult,
)

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (
    MCP_PROTOCOL_VERSION,
    MCP_LEGACY_PROTOCOL_VERSION,
)
MCP_MAX_REQUEST_BODY = 64 * 1024
MCP_MAX_IMAGE_BASE64_BYTES = 4 * ((MAX_PNG_BYTES + 2) // 3)
MCP_SERVER_VERSION = "0.0.0"
_MODERN_MCP_METHODS = frozenset({"server/discover", "tools/list", "tools/call"})
_LEGACY_MCP_METHODS = frozenset(
    {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"}
)

logger = logging.getLogger("kodi_lab.mcp")
_principal_context: contextvars.ContextVar[McpPrincipal] = contextvars.ContextVar(
    "kodi_lab_mcp_principal"
)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float
    seen: float


class BoundedTokenBucketLimiter:
    """Monotonic token buckets with idle-only retirement and a hard ceiling."""

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        maximum_buckets: int,
        idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._maximum = maximum_buckets
        self._idle = idle_seconds
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    @property
    def bucket_count(self) -> int:
        return len(self._buckets)

    def allow(self, key: str) -> bool:
        now = self._clock()
        self._expire(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._maximum:
                return False
            bucket = _Bucket(self._burst, now, now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.updated = now
            bucket.seen = now
            self._buckets.move_to_end(key)
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def _expire(self, now: float) -> None:
        while self._buckets:
            key, bucket = next(iter(self._buckets.items()))
            if now - bucket.seen < self._idle:
                break
            del self._buckets[key]


class BoundedConcurrencyLimiter:
    """Immediate per-client/global admission without queued request growth."""

    def __init__(self, *, per_client: int = 8, global_limit: int = 32) -> None:
        self._per_client_limit = per_client
        self._global_limit = global_limit
        self._lock = asyncio.Lock()
        self._global = 0
        self._clients: dict[UUID, int] = {}

    async def acquire(self, client_id: UUID) -> bool:
        async with self._lock:
            current = self._clients.get(client_id, 0)
            if self._global >= self._global_limit or current >= self._per_client_limit:
                return False
            self._global += 1
            self._clients[client_id] = current + 1
            return True

    async def release(self, client_id: UUID) -> None:
        async with self._lock:
            current = self._clients.get(client_id)
            if current is None:
                raise RuntimeError("MCP concurrency slot released twice")
            self._global -= 1
            if current == 1:
                del self._clients[client_id]
            else:
                self._clients[client_id] = current - 1


class McpTokenAuthenticator(Protocol):
    def authenticate(self, token: str) -> McpPrincipal | None: ...


class _TargetListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class _TargetStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: UUID


class _AddonListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: UUID
    cursor: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=50, ge=1, le=100)


class _ScreenshotCaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: UUID


class _RemoteActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    target_id: UUID
    action: BrowserRemoteAction


class _DevelopmentSourceListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: str | None = Field(default=None, min_length=36, max_length=36)
    limit: int = Field(default=50, ge=1, le=100)


class _WorkspaceStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID | None = None
    cursor: str | None = Field(default=None, min_length=36, max_length=36)
    limit: int = Field(default=50, ge=1, le=100)


class _WorkspaceTreeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    relative_path: str = Field(default="", max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=100, ge=1, le=200)


class _WorkspaceReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    relative_path: str = Field(min_length=1, max_length=512)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=2000)


class ModernMcpAuthMiddleware:
    """Deterministic protocol gate, bearer authentication, and shared admission."""

    def __init__(
        self,
        app: ASGIApp,
        authentication: McpTokenAuthenticator,
        *,
        failed_auth: BoundedTokenBucketLimiter | None = None,
        authenticated: BoundedTokenBucketLimiter | None = None,
        concurrency: BoundedConcurrencyLimiter | None = None,
    ) -> None:
        self._app = app
        self._authentication = authentication
        self.failed_auth = failed_auth or BoundedTokenBucketLimiter(
            rate=5.0, burst=20, maximum_buckets=1024, idle_seconds=15 * 60
        )
        self.authenticated = authenticated or BoundedTokenBucketLimiter(
            rate=20.0, burst=40, maximum_buckets=1024, idle_seconds=15 * 60
        )
        self.concurrency = concurrency or BoundedConcurrencyLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await Response(status_code=404)(scope, receive, send)
            return
        if scope["method"] != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(
                scope, receive, send
            )
            return
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        if length is not None:
            try:
                if int(length) > MCP_MAX_REQUEST_BODY:
                    await Response(status_code=413)(scope, receive, send)
                    return
            except ValueError:
                await Response(status_code=400)(scope, receive, send)
                return
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            await Response(status_code=415)(scope, receive, send)
            return
        body = await self._read_body(receive)
        if body is None:
            await Response(status_code=413)(scope, receive, send)
            return
        request_id = self._request_id(body)
        replay_receive = self._replay(body)
        method = self._request_method(body)
        version = headers.get("mcp-protocol-version")
        if version is None:
            initialize_version = self._initial_initialize_version(body)
            if initialize_version == MCP_LEGACY_PROTOCOL_VERSION:
                version = MCP_LEGACY_PROTOCOL_VERSION
            elif initialize_version is not None:
                await self._protocol_error(
                    scope,
                    replay_receive,
                    send,
                    code=UNSUPPORTED_PROTOCOL_VERSION,
                    message="Unsupported protocol version",
                    data={
                        "supported": [MCP_LEGACY_PROTOCOL_VERSION],
                        "requested": initialize_version[:64],
                    },
                    request_id=request_id,
                )
                return
            else:
                await self._protocol_error(
                    scope,
                    replay_receive,
                    send,
                    code=HEADER_MISMATCH,
                    message="MCP-Protocol-Version header is required",
                    request_id=request_id,
                )
                return
        if version not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
            await self._protocol_error(
                scope,
                replay_receive,
                send,
                code=UNSUPPORTED_PROTOCOL_VERSION,
                message="Unsupported protocol version",
                data={
                    "supported": self._supported_versions_for_body(body),
                    "requested": version[:64],
                },
                request_id=request_id,
            )
            return
        if headers.get("mcp-session-id") is not None:
            await self._protocol_error(
                scope,
                replay_receive,
                send,
                code=HEADER_MISMATCH,
                message="Mcp-Session-Id is not valid for this protocol",
                request_id=request_id,
            )
            return
        declared_method = headers.get("mcp-method")
        if declared_method is not None and declared_method != method:
            await self._protocol_error(
                scope,
                replay_receive,
                send,
                code=HEADER_MISMATCH,
                message="Mcp-Method header does not match request body",
                request_id=request_id,
            )
            return
        declared_tool = headers.get("mcp-name")
        body_tool = self._request_tool_name(body)
        if declared_tool is not None and (
            method != "tools/call" or declared_tool != body_tool
        ):
            await self._protocol_error(
                scope,
                replay_receive,
                send,
                code=HEADER_MISMATCH,
                message="Mcp-Name header does not match request body",
                request_id=request_id,
            )
            return
        query = parse_qs(
            scope.get("query_string", b"").decode("latin-1"),
            keep_blank_values=True,
        )
        if {key.lower() for key in query}.intersection(
            {"token", "access_token", "bearer_token"}
        ):
            await self._authentication_failed(scope, replay_receive, send)
            return
        authorization = headers.get("authorization")
        token = None
        if authorization is not None and authorization.startswith("Bearer "):
            candidate = authorization[7:]
            if candidate and " " not in candidate and "\t" not in candidate:
                token = candidate
        principal = None if token is None else self._authentication.authenticate(token)
        if principal is None:
            await self._authentication_failed(scope, replay_receive, send)
            return
        allowed_methods = (
            _MODERN_MCP_METHODS
            if version == MCP_PROTOCOL_VERSION
            else _LEGACY_MCP_METHODS
        )
        if method is not None and method not in allowed_methods:
            await self._protocol_error(
                scope,
                replay_receive,
                send,
                code=METHOD_NOT_FOUND,
                message="Method not found",
                request_id=request_id,
                status_code=404,
            )
            return
        client_key = str(principal.client_id)
        if not self.authenticated.allow(client_key):
            await self._rate_limited(scope, replay_receive, send)
            return
        if not await self.concurrency.acquire(principal.client_id):
            await self._rate_limited(scope, replay_receive, send)
            return
        reset = _principal_context.set(principal)
        try:
            await self._app(scope, replay_receive, self._without_session_header(send))
        finally:
            _principal_context.reset(reset)
            await self.concurrency.release(principal.client_id)

    async def _authentication_failed(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        peer = scope.get("client")
        key = "unknown" if not peer else str(peer[0])
        if not self.failed_auth.allow(key):
            await self._rate_limited(scope, receive, send)
            return
        await JSONResponse(
            {"error": "authentication_failed"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )(scope, receive, send)

    @staticmethod
    async def _protocol_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
        request_id: str | int | None = None,
        status_code: int = 400,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = dict(data)
        await JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": error},
            status_code=status_code,
        )(scope, receive, send)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return bytes(body)
            body.extend(message.get("body", b""))
            if len(body) > MCP_MAX_REQUEST_BODY:
                return None
            more = message.get("more_body", False)
        return bytes(body)

    @staticmethod
    def _request_id(body: bytes) -> str | int | None:
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        value = decoded.get("id")
        return (
            value
            if isinstance(value, (str, int)) and not isinstance(value, bool)
            else None
        )

    @staticmethod
    def _request_method(body: bytes) -> str | None:
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict):
            return None
        value = decoded.get("method")
        return value if isinstance(value, str) else None

    @staticmethod
    def _request_tool_name(body: bytes) -> str | None:
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or decoded.get("method") != "tools/call":
            return None
        params = decoded.get("params")
        if not isinstance(params, dict):
            return None
        value = params.get("name")
        return value if isinstance(value, str) else None

    @staticmethod
    def _initial_initialize_version(body: bytes) -> str | None:
        """Return an explicit headerless handshake offer, never an inferred era."""

        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or decoded.get("jsonrpc") != "2.0":
            return None
        if decoded.get("method") != "initialize":
            return None
        request_id = decoded.get("id")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            return None
        params = decoded.get("params")
        if not isinstance(params, dict):
            return None
        version = params.get("protocolVersion")
        return version if isinstance(version, str) else None

    @staticmethod
    def _supported_versions_for_body(body: bytes) -> list[str]:
        """Report only the explicitly selected era's bounded compatibility set."""

        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return list(MCP_SUPPORTED_PROTOCOL_VERSIONS)
        if not isinstance(decoded, dict):
            return list(MCP_SUPPORTED_PROTOCOL_VERSIONS)
        params = decoded.get("params")
        if isinstance(params, dict):
            metadata = params.get("_meta")
            if isinstance(metadata, dict) and isinstance(
                metadata.get("io.modelcontextprotocol/protocolVersion"), str
            ):
                return [MCP_PROTOCOL_VERSION]
        if decoded.get("method") == "initialize":
            return [MCP_LEGACY_PROTOCOL_VERSION]
        return list(MCP_SUPPORTED_PROTOCOL_VERSIONS)

    @staticmethod
    def _replay(body: bytes) -> Receive:
        sent = False

        async def receive() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return receive

    @staticmethod
    async def _rate_limited(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"Retry-After": "1"},
        )(scope, receive, send)

    @staticmethod
    def _without_session_header(send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"mcp-session-id"
                ]
            await send(message)

        return wrapped


@dataclass(frozen=True, slots=True)
class McpHttpTransport:
    app: ASGIApp
    server: Server[dict[str, Any]]
    development_read_limiter: BoundedTokenBucketLimiter


def build_mcp_http_transport(
    *,
    authentication: McpAuthenticationService,
    authorization: McpAuthorizationService,
    registry: McpToolRegistry,
    tools: McpTargetToolService,
    failed_auth_limiter: BoundedTokenBucketLimiter | None = None,
    authenticated_limiter: BoundedTokenBucketLimiter | None = None,
    concurrency_limiter: BoundedConcurrencyLimiter | None = None,
    development_read_limiter: BoundedTokenBucketLimiter | None = None,
) -> McpHttpTransport:
    read_annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    capture_annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
    control_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    development_limiter = development_read_limiter or BoundedTokenBucketLimiter(
        rate=10.0,
        burst=20,
        maximum_buckets=1024,
        idle_seconds=15 * 60,
    )
    inventory = {
        "addon_list": Tool(
            name="addon_list",
            title="List installed Kodi add-ons",
            description=(
                "List one authorized target's bounded semantic add-on inventory."
            ),
            input_schema=_AddonListInput.model_json_schema(),
            output_schema=AddonListResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "screenshot_capture": Tool(
            name="screenshot_capture",
            title="Capture a Kodi screenshot",
            description=(
                "Capture one authorized target and return native PNG image content."
            ),
            input_schema=_ScreenshotCaptureInput.model_json_schema(),
            output_schema=ScreenshotCaptureMetadata.model_json_schema(),
            annotations=capture_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "remote_action": Tool(
            name="remote_action",
            title="Control Kodi with one semantic action",
            description=(
                "Dispatch one replay-safe semantic remote action to an authorized "
                "internal target."
            ),
            input_schema=_RemoteActionInput.model_json_schema(),
            output_schema=McpRemoteActionResult.model_json_schema(),
            annotations=control_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "target_list": Tool(
            name="target_list",
            title="List authorized Kodi targets",
            description=(
                "List explicitly granted Kodi Lab targets with bounded status."
            ),
            input_schema=_TargetListInput.model_json_schema(),
            output_schema=TargetListResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "target_status": Tool(
            name="target_status",
            title="Inspect one Kodi target",
            description=(
                "Return bounded semantic status for one explicit authorized target."
            ),
            input_schema=_TargetStatusInput.model_json_schema(),
            output_schema=TargetStatusResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "development_source_list": Tool(
            name="development_source_list",
            title="List granted development sources",
            description="List bounded metadata for Web-approved development sources.",
            input_schema=_DevelopmentSourceListInput.model_json_schema(),
            output_schema=DevelopmentSourceListResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_status": Tool(
            name="workspace_status",
            title="Inspect owned workspace status",
            description="Inspect one or list bounded MCP-owned workspace statuses.",
            input_schema=_WorkspaceStatusInput.model_json_schema(),
            output_schema=WorkspaceStatusResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_tree": Tool(
            name="workspace_tree",
            title="List one workspace directory",
            description=(
                "List one bounded non-recursive directory in an owned workspace."
            ),
            input_schema=_WorkspaceTreeInput.model_json_schema(),
            output_schema=WorkspaceTreeResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_read": Tool(
            name="workspace_read",
            title="Read workspace UTF-8 text",
            description="Read one bounded UTF-8 line segment from an owned workspace.",
            input_schema=_WorkspaceReadInput.model_json_schema(),
            output_schema=WorkspaceReadResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
    }

    async def list_tools(
        _context: ServerRequestContext[dict[str, Any]],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        principal = _principal_context.get()
        allowed = authorization.discoverable_tools(principal)
        return ListToolsResult(tools=[inventory[name] for name in allowed])

    async def call_tool(
        _context: ServerRequestContext[dict[str, Any]],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        principal = _principal_context.get()
        started = _timestamp()
        target_id: UUID | None = None
        source_id: UUID | None = None
        workspace_id: UUID | None = None
        relative_path: str | None = None
        try:
            arguments = params.arguments or {}
            result: (
                AddonListResult
                | McpRemoteActionResult
                | ScreenshotToolResult
                | TargetListResult
                | TargetStatusResult
                | DevelopmentSourceListResult
                | WorkspaceReadResult
                | WorkspaceStatusResult
                | WorkspaceTreeResult
            )
            if params.name in {
                "development_source_list",
                "workspace_read",
                "workspace_status",
                "workspace_tree",
            } and not development_limiter.allow(str(principal.client_id)):
                raise McpAuthorizationError("rate_limited")
            if params.name == "addon_list":
                parsed_addons = _AddonListInput.model_validate(arguments)
                target_id = parsed_addons.target_id
                result = await asyncio.to_thread(
                    tools.addon_list,
                    principal,
                    target_id=target_id,
                    cursor=parsed_addons.cursor,
                    limit=parsed_addons.limit,
                )
            elif params.name == "remote_action":
                parsed_remote = _RemoteActionInput.model_validate(arguments)
                target_id = parsed_remote.target_id
                result = await asyncio.to_thread(
                    tools.remote_action,
                    principal,
                    target_id=target_id,
                    request_id=parsed_remote.request_id,
                    action=parsed_remote.action,
                )
            elif params.name == "screenshot_capture":
                parsed_screenshot = _ScreenshotCaptureInput.model_validate(arguments)
                target_id = parsed_screenshot.target_id
                result = await asyncio.to_thread(
                    tools.screenshot_capture,
                    principal,
                    target_id=target_id,
                )
            elif params.name == "target_list":
                parsed_list = _TargetListInput.model_validate(arguments)
                result = tools.target_list(
                    principal,
                    cursor=parsed_list.cursor,
                    limit=parsed_list.limit,
                )
            elif params.name == "target_status":
                parsed_status = _TargetStatusInput.model_validate(arguments)
                target_id = parsed_status.target_id
                result = tools.target_status(principal, target_id=target_id)
            elif params.name == "development_source_list":
                parsed_sources = _DevelopmentSourceListInput.model_validate(arguments)
                result = tools.development_source_list(
                    principal,
                    cursor=parsed_sources.cursor,
                    limit=parsed_sources.limit,
                )
            elif params.name == "workspace_status":
                parsed_workspace_status = _WorkspaceStatusInput.model_validate(
                    arguments
                )
                workspace_id = parsed_workspace_status.workspace_id
                result = tools.workspace_status(
                    principal,
                    workspace_id=workspace_id,
                    cursor=parsed_workspace_status.cursor,
                    limit=parsed_workspace_status.limit,
                )
                if workspace_id is not None and result.workspaces:
                    source_id = result.workspaces[0].source_id
            elif params.name == "workspace_tree":
                parsed_tree = _WorkspaceTreeInput.model_validate(arguments)
                workspace_id = parsed_tree.workspace_id
                relative_path = parsed_tree.relative_path
                result = await asyncio.to_thread(
                    tools.workspace_tree,
                    principal,
                    workspace_id=workspace_id,
                    relative_path=relative_path,
                    cursor=parsed_tree.cursor,
                    limit=parsed_tree.limit,
                )
                source_id = result.source_id
            elif params.name == "workspace_read":
                parsed_read = _WorkspaceReadInput.model_validate(arguments)
                workspace_id = parsed_read.workspace_id
                relative_path = parsed_read.relative_path
                result = await asyncio.to_thread(
                    tools.workspace_read,
                    principal,
                    workspace_id=workspace_id,
                    relative_path=relative_path,
                    start_line=parsed_read.start_line,
                    max_lines=parsed_read.max_lines,
                )
                source_id = result.source_id
            else:
                raise McpAuthorizationError("permission_denied")
        except ValidationError:
            outcome = "invalid_params"
        except McpAuthorizationError as error:
            outcome = error.code
        else:
            if isinstance(result, ScreenshotToolResult):
                encoded = base64.b64encode(result.png).decode("ascii")
                if len(encoded) > MCP_MAX_IMAGE_BASE64_BYTES:
                    outcome = "response_too_large"
                else:
                    content = result.metadata.model_dump(mode="json")
                    _audit(
                        principal,
                        params.name,
                        target_id,
                        started,
                        "success",
                        source_id=source_id,
                        workspace_id=workspace_id,
                    )
                    return CallToolResult(
                        content=[ImageContent(data=encoded, mime_type=PNG_MIME_TYPE)],
                        structured_content=content,
                        is_error=False,
                    )
                _audit(
                    principal,
                    params.name,
                    target_id,
                    started,
                    outcome,
                    source_id=source_id,
                    workspace_id=workspace_id,
                )
                return CallToolResult(
                    content=[TextContent(text=outcome)],
                    structured_content={"error": outcome},
                    is_error=True,
                )
            content = result.model_dump(mode="json")
            _audit(
                principal,
                params.name,
                target_id,
                started,
                "success",
                source_id=source_id,
                workspace_id=workspace_id,
            )
            return CallToolResult(
                content=[
                    TextContent(
                        text=json.dumps(content, separators=(",", ":"), sort_keys=True)
                    )
                ],
                structured_content=content,
                is_error=False,
            )
        _audit(
            principal,
            params.name,
            target_id,
            started,
            outcome,
            source_id=source_id,
            workspace_id=workspace_id,
        )
        return CallToolResult(
            content=[TextContent(text=outcome)],
            structured_content={"error": outcome},
            is_error=True,
        )

    server: Server[dict[str, Any]] = Server(
        name="kodi-lab",
        title="Kodi Lab",
        description="Authenticated Kodi Lab target inspection",
        instructions=(
            "Kodi Lab MCP schema 1. Every target-scoped call requires an "
            "explicit target_id."
        ),
        version=MCP_SERVER_VERSION,
        cache_hints={
            "server/discover": CacheHint(ttl_ms=0, scope="private"),
            "tools/list": CacheHint(ttl_ms=0, scope="private"),
        },
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )

    sdk_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MCP_MAX_REQUEST_BODY,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
            allowed_origins=[
                "http://127.0.0.1",
                "http://127.0.0.1:*",
                "http://localhost",
                "http://localhost:*",
            ],
        ),
        host="127.0.0.1",
    )
    protected = ModernMcpAuthMiddleware(
        sdk_app,
        authentication,
        failed_auth=failed_auth_limiter,
        authenticated=authenticated_limiter,
        concurrency=concurrency_limiter,
    )
    return McpHttpTransport(
        app=protected,
        server=server,
        development_read_limiter=development_limiter,
    )


def _audit(
    principal: McpPrincipal,
    tool_name: str,
    target_id: UUID | None,
    started_at: str,
    outcome: str,
    *,
    source_id: UUID | None = None,
    workspace_id: UUID | None = None,
) -> None:
    logger.info(
        "mcp_request client_id=%s credential_id=%s tool=%s target_id=%s "
        "source_id=%s workspace_id=%s "
        "started_at=%s completed_at=%s outcome=%s",
        principal.client_id,
        principal.credential_id,
        tool_name,
        target_id,
        source_id,
        workspace_id,
        started_at,
        _timestamp(),
        outcome,
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

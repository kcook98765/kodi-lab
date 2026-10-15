"""Authenticated primary-modern and bounded-legacy MCP HTTP integration."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs
from uuid import UUID, uuid4

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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kodi_lab import __version__
from kodi_lab.application.mcp_administration import (
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_promotion_requests import PromotionRequestResult
from kodi_lab.application.mcp_remote_actions import McpRemoteActionResult
from kodi_lab.application.mcp_workspace_development import (
    EXECUTION_DEPENDENCY_REASONS,
    WORKSPACE_SMOKE_SUCCESS_DESCRIPTION,
    WorkspaceDeploymentResult,
    WorkspaceExecutionResult,
)
from kodi_lab.application.mcp_workspace_mutations import WorkspaceCreateResult
from kodi_lab.application.production_transient_approvals import (
    ProductionApprovalRequiredResult,
    ProductionApprovalTerminalResult,
)
from kodi_lab.application.workspace_execution_recovery import (
    WorkspaceExecutionRecoveryResult,
)
from kodi_lab.domain.smoke_diagnostics import AddonExceptionDiagnostic
from kodi_lab.kodi.addons import KodiJsonRpcDiagnostic
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.kodi.screenshots import MAX_PNG_BYTES, PNG_MIME_TYPE
from kodi_lab.kodi.ui_state import UiObservation
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.lan import MCP_INSTANCE_HEADER
from kodi_lab.mcp.registry import KODI_LAB_MCP_SCHEMA_VERSION, McpToolRegistry
from kodi_lab.mcp.tools import (
    AddonListResult,
    DevelopmentSourceListResult,
    McpTargetToolService,
    ScreenshotCaptureMetadata,
    ScreenshotToolResult,
    TargetListResult,
    TargetStatusResult,
    WorkspaceCaptureToolInput,
    WorkspaceCaptureToolResult,
    WorkspaceDeployInput,
    WorkspaceEditInput,
    WorkspaceEditResult,
    WorkspaceExecuteInput,
    WorkspaceExecutionRecoverInput,
    WorkspaceReadResult,
    WorkspaceSourceScanToolResult,
    WorkspaceStatusResult,
    WorkspaceTargetOperationInput,
    WorkspaceTreeResult,
    WorkspaceValidateInput,
    WorkspaceValidateResult,
)
from kodi_lab.screenshot_diagnostics import (
    emit_screenshot_observation,
    emit_screenshot_stage,
    observe_screenshot_diagnostic,
    screenshot_diagnostic_trace,
)

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_LEGACY_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (
    MCP_PROTOCOL_VERSION,
    MCP_LEGACY_PROTOCOL_VERSION,
)
MCP_MAX_REQUEST_BODY = 64 * 1024
MCP_MAX_IMAGE_BASE64_BYTES = 4 * ((MAX_PNG_BYTES + 2) // 3)
MCP_SERVER_VERSION = __version__
_MODERN_MCP_METHODS = frozenset({"server/discover", "tools/list", "tools/call"})
_LEGACY_MCP_METHODS = frozenset(
    {"initialize", "notifications/initialized", "ping", "tools/list", "tools/call"}
)

logger = logging.getLogger("kodi_lab.mcp")
_principal_context: contextvars.ContextVar[McpPrincipal] = contextvars.ContextVar(
    "kodi_lab_mcp_principal"
)

_MCP_AUDIT_SCHEMA_VERSION = 1
_MCP_AUDIT_SAFE_TOOL_NAMES = frozenset(
    {
        "inspect_ui_state",
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
        "workspace_source_scan",
        "workspace_capture",
        "workspace_execute",
        "workspace_execution_recover",
        "workspace_read",
        "workspace_status",
        "workspace_tree",
        "workspace_validate",
    }
)
_MCP_AUDIT_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MCP_AUDIT_AUTHORIZATION_CODES = frozenset(
    {
        "permission_denied",
        "source_not_found_or_not_authorized",
        "target_not_found_or_not_authorized",
        "workspace_not_found_or_not_authorized",
    }
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
    request_id: UUID | None = None


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


class _WorkspaceSourceScanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID = Field(
        description=(
            "Bound workspace ID from workspace_status; this is not a target ID "
            "or source ID."
        )
    )


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


class _WorkspaceCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    source_id: UUID


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
        transport_security: TransportSecuritySettings | None = None,
        instance_id: str | None = None,
    ) -> None:
        self._app = app
        self._authentication = authentication
        self._transport_security = transport_security
        self._instance_id = instance_id
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
        headers = Headers(scope=scope)
        if self._transport_security is not None:
            # Check before authentication/protocol dispatch, including GET/DELETE.
            # The SDK logs rejected caller-controlled headers; do not pass them on.
            for name, allowed, status_code in (
                ("host", self._transport_security.allowed_hosts, 421),
                ("origin", self._transport_security.allowed_origins, 403),
            ):
                values = headers.getlist(name)
                if (
                    (name == "host" and not values)
                    or len(values) > 1
                    or (values and not self._allowed_header(values[0], allowed))
                ):
                    await Response(
                        f"Invalid {name.title()} header", status_code=status_code
                    )(scope, receive, send)
                    return
        if any(
            len(headers.getlist(name)) > 1
            for name in (
                "authorization",
                "mcp-protocol-version",
                "mcp-method",
                "mcp-name",
                "content-length",
                "content-type",
            )
        ):
            await Response(status_code=400)(scope, receive, send)
            return
        if scope["method"] != "POST":
            await Response(status_code=405, headers={"Allow": "POST"})(
                scope, receive, send
            )
            return
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
            headers={
                "WWW-Authenticate": "Bearer",
                **(
                    {MCP_INSTANCE_HEADER: self._instance_id}
                    if self._instance_id is not None
                    else {}
                ),
            },
        )(scope, receive, send)

    @staticmethod
    def _allowed_header(value: str, allowed: list[str]) -> bool:
        if value in allowed:
            return True
        for pattern in allowed:
            if pattern.endswith(":*") and value.startswith(pattern[:-1]):
                port = value[len(pattern) - 1 :]
                if (
                    1 <= len(port) <= 5
                    and port.isascii()
                    and port.isdecimal()
                    and 1 <= int(port) <= 65535
                ):
                    return True
        return False

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
    development_edit_limiter: BoundedTokenBucketLimiter
    instance_id: str


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
    development_edit_limiter: BoundedTokenBucketLimiter | None = None,
    additional_allowed_hosts: tuple[str, ...] = (),
    additional_allowed_origins: tuple[str, ...] = (),
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
    edit_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
    execution_annotations = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
    remote_action_output_schema = TypeAdapter(
        McpRemoteActionResult
        | ProductionApprovalRequiredResult
        | ProductionApprovalTerminalResult
    ).json_schema()
    # MCP requires an object-root output schema; the additive result variants
    # remain closed beneath the root-level anyOf.
    remote_action_output_schema["type"] = "object"
    development_limiter = development_read_limiter or BoundedTokenBucketLimiter(
        rate=10.0,
        burst=20,
        maximum_buckets=1024,
        idle_seconds=15 * 60,
    )
    edit_limiter = development_edit_limiter or BoundedTokenBucketLimiter(
        rate=2.0,
        burst=4,
        maximum_buckets=1024,
        idle_seconds=15 * 60,
    )
    inventory = {
        "inspect_ui_state": Tool(
            name="inspect_ui_state",
            title="Inspect Kodi UI state",
            description=(
                "Inspect the foreground window/dialog, actual focused control, "
                "associated container and selected item, basic playback and UI flags. "
                "Use before/after navigation to understand what Kodi is showing; "
                "structured state is usually cheaper than a screenshot. Focus and "
                "selected item are distinct: associated=true means item metadata "
                "belongs to the current container context. Snapshots are non-atomic. "
                "Includes original Kodi IDs/labels and semantic resolution for the "
                "base window and foreground dialog. Resolution source/status "
                "distinguish reported names, verified mappings, ambiguity and unknowns."
            ),
            input_schema=_TargetStatusInput.model_json_schema(),
            output_schema=UiObservation.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
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
            output_schema=remote_action_output_schema,
            annotations=control_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "promotion_request": Tool(
            name="promotion_request",
            title="Request human-reviewed source promotion",
            description=(
                "Prepare one replay-safe exact-generation promotion plan for "
                "Web administrator review; this tool cannot approve or apply it."
            ),
            input_schema=WorkspaceTargetOperationInput.model_json_schema(),
            output_schema=PromotionRequestResult.model_json_schema(),
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
        "workspace_create": Tool(
            name="workspace_create",
            title="Create an owned workspace",
            description=(
                "Replay-safely create one server-located workspace from a granted "
                "development source."
            ),
            input_schema=_WorkspaceCreateInput.model_json_schema(),
            output_schema=WorkspaceCreateResult.model_json_schema(),
            annotations=control_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_edit": Tool(
            name="workspace_edit",
            title="Edit workspace UTF-8 text",
            description=(
                "Edit one file in an owned ready workspace. All modes require "
                "workspace_id, relative_path, and expected_generation (the currently "
                "observed generation before the edit). create requires content as "
                "the complete initial content of a new file; replace requires "
                "content as the complete resulting content of an existing file; "
                "exact_patch requires expected_text equal to the complete "
                "current file exactly and replacement_text as the complete "
                "resulting file. Omit text fields from other modes."
            ),
            input_schema=WorkspaceEditInput.model_json_schema(),
            output_schema=WorkspaceEditResult.model_json_schema(),
            annotations=edit_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_source_scan": Tool(
            name="workspace_source_scan",
            title="Scan a bound workspace's registered source",
            description=(
                "Freshly inspect exact source identity, version and tree digest for "
                "one granted source and bound workspace. "
                "Does not change workspace bytes."
            ),
            input_schema=_WorkspaceSourceScanInput.model_json_schema(),
            output_schema=WorkspaceSourceScanToolResult.model_json_schema(),
            annotations=read_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_capture": Tool(
            name="workspace_capture",
            title="Capture registered source into a bound workspace",
            description=(
                "Capture the current registered source into its bound, ready, "
                "clean workspace. expected_generation is the workspace generation "
                "currently observed in workspace_status or workspace_source_scan, "
                "not the generation to create. A changed capture advances it by "
                "one; unchanged source leaves it unchanged. After "
                "workspace_generation_conflict, refresh workspace_status and use "
                "its current generation if capture is still intended."
            ),
            input_schema=WorkspaceCaptureToolInput.model_json_schema(),
            output_schema=WorkspaceCaptureToolResult.model_json_schema(),
            annotations=edit_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_deploy": Tool(
            name="workspace_deploy",
            title="Deploy an exact workspace generation",
            description=(
                "Deploy one validated exact workspace generation. Managed disposable "
                "(internal_lab) targets use direct deployment without repository "
                "publication, Web approval, user Kodi install/update, or Web Recheck. "
                "An opted-in external_development target with repository readiness "
                "uses repository publication, Web approval, user Kodi install/update, "
                "and exact installed-tree Recheck. external_production targets never "
                "accept development workspace deployment in any mode."
            ),
            input_schema=WorkspaceDeployInput.model_json_schema(),
            output_schema=WorkspaceDeploymentResult.model_json_schema(),
            annotations=control_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_execute": Tool(
            name="workspace_execute",
            title="Run a bounded workspace smoke or diagnostic",
            description=(
                "For an exact completed deployment, run the default bounded "
                "Python video-plugin smoke or the managed-target fixed negative "
                "Kodi RPC diagnostic. Both requests are server-owned. Plugin "
                "code may use the target's existing network access. "
                + WORKSPACE_SMOKE_SUCCESS_DESCRIPTION
            ),
            input_schema=WorkspaceExecuteInput.model_json_schema(),
            output_schema=WorkspaceExecutionResult.model_json_schema(),
            annotations=execution_annotations,
            _meta={"kodiLabMcpSchemaVersion": KODI_LAB_MCP_SCHEMA_VERSION},
        ),
        "workspace_execution_recover": Tool(
            name="workspace_execution_recover",
            title="Recover one ambiguous workspace smoke",
            description=(
                "Recover only an eligible, previously dispatched, timeout-ambiguous "
                "workspace smoke, or a completed non-timeout ambiguity with an "
                "independently verified later stop, on its managed disposable "
                "runtime. Supply that "
                "execution's operation ID. A definitive execution_failed result "
                "is not eligible. Recovery is a separate operation and does not "
                "rerun the smoke. Contain the original smoke, then verify readiness "
                "and deployment provenance."
            ),
            input_schema=WorkspaceExecutionRecoverInput.model_json_schema(),
            output_schema=WorkspaceExecutionRecoveryResult.model_json_schema(),
            annotations=control_annotations,
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
        "workspace_validate": Tool(
            name="workspace_validate",
            title="Validate workspace structure",
            description=(
                "Validate one exact workspace generation using closed structural "
                "checks; no command or test selector is accepted."
            ),
            input_schema=WorkspaceValidateInput.model_json_schema(),
            output_schema=WorkspaceValidateResult.model_json_schema(),
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
        request_id: UUID | None = None
        generation: int | None = None
        promotion_plan_id: UUID | None = None
        audit_outcome = "internal_error"
        result_code = "internal_error"
        result_reason: str | None = None
        result_diagnostic: KodiJsonRpcDiagnostic | None = None
        result_addon_exception: AddonExceptionDiagnostic | None = None
        result_capture_conflict_observed_generation: int | None = None
        diagnostic_context = None
        try:
            arguments = params.arguments or {}
            result: (
                AddonListResult
                | UiObservation
                | McpRemoteActionResult
                | ProductionApprovalRequiredResult
                | ProductionApprovalTerminalResult
                | ScreenshotToolResult
                | TargetListResult
                | TargetStatusResult
                | DevelopmentSourceListResult
                | WorkspaceReadResult
                | WorkspaceCreateResult
                | WorkspaceEditResult
                | WorkspaceSourceScanToolResult
                | WorkspaceCaptureToolResult
                | WorkspaceStatusResult
                | WorkspaceTreeResult
                | WorkspaceValidateResult
                | WorkspaceDeploymentResult
                | WorkspaceExecutionResult
                | WorkspaceExecutionRecoveryResult
                | PromotionRequestResult
            )
            if params.name in {
                "development_source_list",
                "workspace_read",
                "workspace_status",
                "workspace_tree",
                "workspace_validate",
                "workspace_source_scan",
            } and not development_limiter.allow(str(principal.client_id)):
                raise McpAuthorizationError("rate_limited")
            if params.name in {
                "workspace_edit",
                "workspace_capture",
            } and not edit_limiter.allow(str(principal.client_id)):
                raise McpAuthorizationError("rate_limited")
            if params.name == "inspect_ui_state":
                parsed_ui_state = _TargetStatusInput.model_validate(arguments)
                target_id = parsed_ui_state.target_id
                result = await asyncio.to_thread(
                    tools.inspect_ui_state, principal, target_id=target_id
                )
            elif params.name == "addon_list":
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
                request_id = parsed_remote.request_id
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
                if parsed_screenshot.request_id is not None:
                    diagnostic_request_id = parsed_screenshot.request_id
                    request_id = diagnostic_request_id
                    protocol_request: dict[str, str | int] = (
                        observe_screenshot_diagnostic(
                            lambda: mcp_audit_request_correlation(_context.request_id)
                        )
                        or {"type": "unrecognized"}
                    )
                    diagnostic_context = observe_screenshot_diagnostic(
                        lambda: screenshot_diagnostic_trace(
                            protocol_request=protocol_request,
                            client_id=principal.client_id,
                            target_id=parsed_screenshot.target_id,
                            request_id=diagnostic_request_id,
                        )
                    )
                    if diagnostic_context is not None:
                        observe_screenshot_diagnostic(diagnostic_context.__enter__)
                    emit_screenshot_stage(
                        "mcp_authenticated", "passed", "mcp_request_authenticated"
                    )
                result = await asyncio.to_thread(
                    tools.screenshot_capture,
                    principal,
                    target_id=target_id,
                    request_id=parsed_screenshot.request_id,
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
                result = await asyncio.to_thread(
                    tools.target_status, principal, target_id=target_id
                )
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
            elif params.name == "workspace_create":
                parsed_create = _WorkspaceCreateInput.model_validate(arguments)
                source_id = parsed_create.source_id
                result = await asyncio.to_thread(
                    tools.workspace_create,
                    principal,
                    request_id=parsed_create.request_id,
                    source_id=source_id,
                )
                workspace_id = result.workspace_id
            elif params.name == "workspace_edit":
                parsed_edit = WorkspaceEditInput.model_validate(arguments)
                workspace_id = parsed_edit.workspace_id
                relative_path = parsed_edit.relative_path
                result = await asyncio.to_thread(
                    tools.workspace_edit, principal, parsed_edit
                )
                source_id = result.source_id
            elif params.name == "workspace_source_scan":
                parsed_scan = _WorkspaceSourceScanInput.model_validate(arguments)
                workspace_id = parsed_scan.workspace_id
                result = await asyncio.to_thread(
                    tools.workspace_source_scan, principal, workspace_id
                )
                source_id = result.source_id
            elif params.name == "workspace_capture":
                parsed_capture = WorkspaceCaptureToolInput.model_validate(arguments)
                workspace_id = parsed_capture.workspace_id
                generation = parsed_capture.expected_generation
                result = await asyncio.to_thread(
                    tools.workspace_capture, principal, parsed_capture
                )
                source_id = result.source_id
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
            elif params.name == "workspace_validate":
                parsed_validation = WorkspaceValidateInput.model_validate(arguments)
                workspace_id = parsed_validation.workspace_id
                result = await asyncio.to_thread(
                    tools.workspace_validate, principal, parsed_validation
                )
                source_id = result.source_id
            elif params.name == "workspace_deploy":
                parsed_deploy = WorkspaceDeployInput.model_validate(arguments)
                workspace_id = parsed_deploy.workspace_id
                target_id = parsed_deploy.target_id
                result = await asyncio.to_thread(
                    tools.workspace_deploy, principal, parsed_deploy
                )
                source_id = result.source_id
            elif params.name == "workspace_execute":
                parsed_execution = WorkspaceExecuteInput.model_validate(arguments)
                workspace_id = parsed_execution.workspace_id
                target_id = parsed_execution.target_id
                result = await asyncio.to_thread(
                    tools.workspace_execute, principal, parsed_execution
                )
                source_id = result.source_id
            elif params.name == "workspace_execution_recover":
                parsed_recovery = WorkspaceExecutionRecoverInput.model_validate(
                    arguments
                )
                request_id = parsed_recovery.request_id
                result = await asyncio.to_thread(
                    tools.workspace_execution_recover, principal, parsed_recovery
                )
            elif params.name == "promotion_request":
                parsed_promotion = WorkspaceTargetOperationInput.model_validate(
                    arguments
                )
                workspace_id = parsed_promotion.workspace_id
                target_id = parsed_promotion.target_id
                request_id = parsed_promotion.request_id
                generation = parsed_promotion.expected_generation
                result = await asyncio.to_thread(
                    tools.promotion_request, principal, parsed_promotion
                )
                source_id = result.source_id
                promotion_plan_id = result.promotion_plan_id
            else:
                raise McpAuthorizationError("permission_denied")
        except ValidationError:
            audit_outcome = "invalid_request"
            result_code = "invalid_params"
        except McpAuthorizationError as error:
            result_code = error.code
            result_reason = error.reason
            if (
                params.name == "workspace_capture"
                and error.code == "workspace_capture_rejected"
                and error.reason == "workspace_generation_conflict"
            ):
                result_capture_conflict_observed_generation = (
                    error.capture_conflict_observed_generation
                )
            if params.name == "workspace_execute" and error.code == "execution_failed":
                result_diagnostic = error.diagnostic
                result_addon_exception = error.addon_exception
            audit_outcome = _audit_outcome_for_tool_error(result_code)
            emit_screenshot_stage("mcp_result", "failed", result_code)
        except BaseException:
            _audit_safely(
                principal,
                params.name,
                target_id,
                started,
                audit_outcome,
                result_code=result_code,
                protocol_request_id=_context.request_id,
                source_id=source_id,
                workspace_id=workspace_id,
                request_id=request_id,
                generation=generation,
                promotion_plan_id=promotion_plan_id,
            )
            if diagnostic_context is not None:
                observe_screenshot_diagnostic(
                    lambda: diagnostic_context.__exit__(None, None, None)
                )
            raise
        else:
            try:
                if isinstance(result, ScreenshotToolResult):
                    encoded = base64.b64encode(result.png).decode("ascii")
                    emit_screenshot_observation(
                        "image_payload",
                        "passed",
                        "mcp_base64_encoded",
                        lambda: {
                            "encoded_character_length": len(encoded),
                            "decoded_byte_length": len(result.png),
                            "image_sha256": hashlib.sha256(result.png).hexdigest(),
                            "detected_format": "png",
                            "width": result.metadata.width,
                            "height": result.metadata.height,
                            "color_mode": "rgb",
                        },
                    )
                    if len(encoded) > MCP_MAX_IMAGE_BASE64_BYTES:
                        audit_outcome = "tool_error"
                        result_code = "response_too_large"
                        response = _tool_error_result(result_code)
                    else:
                        content = result.metadata.model_dump(mode="json")
                        response = CallToolResult(
                            content=[
                                ImageContent(data=encoded, mime_type=PNG_MIME_TYPE)
                            ],
                            structured_content=content,
                            is_error=False,
                        )
                else:
                    content = result.model_dump(mode="json")
                    response = CallToolResult(
                        content=[
                            TextContent(
                                text=json.dumps(
                                    content, separators=(",", ":"), sort_keys=True
                                )
                            )
                        ],
                        structured_content=content,
                        is_error=False,
                    )
                if result_code == "internal_error":
                    audit_outcome = "success"
                    result_code = "success"
                if isinstance(result, ScreenshotToolResult):
                    emit_screenshot_stage("mcp_result", "passed", result_code)
            except BaseException:
                _audit_safely(
                    principal,
                    params.name,
                    target_id,
                    started,
                    "internal_error",
                    result_code="internal_error",
                    protocol_request_id=_context.request_id,
                    source_id=source_id,
                    workspace_id=workspace_id,
                    request_id=request_id,
                    generation=generation,
                    promotion_plan_id=promotion_plan_id,
                )
                if diagnostic_context is not None:
                    observe_screenshot_diagnostic(
                        lambda: diagnostic_context.__exit__(None, None, None)
                    )
                raise
            _audit_safely(
                principal,
                params.name,
                target_id,
                started,
                audit_outcome,
                result_code=result_code,
                protocol_request_id=_context.request_id,
                source_id=source_id,
                workspace_id=workspace_id,
                request_id=request_id,
                generation=generation,
                promotion_plan_id=promotion_plan_id,
            )
            if diagnostic_context is not None:
                observe_screenshot_diagnostic(
                    lambda: diagnostic_context.__exit__(None, None, None)
                )
            return response
        response = _tool_error_result(
            result_code,
            tool_name=params.name,
            reason=result_reason,
            diagnostic=result_diagnostic,
            addon_exception=result_addon_exception,
            capture_conflict_workspace_id=workspace_id,
            capture_conflict_supplied_generation=generation,
            capture_conflict_observed_generation=(
                result_capture_conflict_observed_generation
            ),
        )
        _audit_safely(
            principal,
            params.name,
            target_id,
            started,
            audit_outcome,
            result_code=result_code,
            protocol_request_id=_context.request_id,
            source_id=source_id,
            workspace_id=workspace_id,
            request_id=request_id,
            generation=generation,
            promotion_plan_id=promotion_plan_id,
        )
        if diagnostic_context is not None:
            observe_screenshot_diagnostic(
                lambda: diagnostic_context.__exit__(None, None, None)
            )
        return response

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

    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            *additional_allowed_hosts,
        ],
        allowed_origins=[
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "http://localhost",
            "http://localhost:*",
            *additional_allowed_origins,
        ],
    )
    sdk_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MCP_MAX_REQUEST_BODY,
        transport_security=transport_security,
        host="127.0.0.1",
    )
    instance_id = str(uuid4())
    protected = ModernMcpAuthMiddleware(
        sdk_app,
        authentication,
        failed_auth=failed_auth_limiter,
        authenticated=authenticated_limiter,
        concurrency=concurrency_limiter,
        transport_security=transport_security,
        instance_id=instance_id,
    )
    return McpHttpTransport(
        app=protected,
        server=server,
        development_read_limiter=development_limiter,
        development_edit_limiter=edit_limiter,
        instance_id=instance_id,
    )


_SAFE_TOOL_REASONS = {
    "development_source_scan_unsafe": frozenset(
        {"source_identity_changed", "source_invalid", "registered_source_unavailable"}
    ),
    "workspace_capture_rejected": frozenset(
        {
            "workspace_generation_conflict",
            "workspace_private_edits_present",
            "source_identity_changed",
            "source_invalid",
            "registered_source_unavailable",
            "workspace_not_clean_and_ready",
            "capture_reconciliation_required",
        }
    ),
    "execution_unsupported": EXECUTION_DEPENDENCY_REASONS,
}


def _tool_error_result(
    result_code: str,
    *,
    tool_name: str | None = None,
    reason: str | None = None,
    diagnostic: KodiJsonRpcDiagnostic | None = None,
    addon_exception: AddonExceptionDiagnostic | None = None,
    capture_conflict_workspace_id: UUID | None = None,
    capture_conflict_supplied_generation: int | None = None,
    capture_conflict_observed_generation: int | None = None,
) -> CallToolResult:
    # A tool error has no success-shaped structured result. Its text remains
    # available to models without violating the tool's declared outputSchema.
    message = result_code
    if reason is not None and reason in _SAFE_TOOL_REASONS.get(result_code, ()):
        message = f"{result_code}: {reason}"
    if tool_name == "workspace_edit":
        if result_code == "workspace_generation_conflict":
            message += (
                "\nnext_action: Inspect current workspace state with "
                "workspace_status and retry using its currently observed "
                "generation as expected_generation."
            )
        elif result_code == "workspace_patch_preimage_mismatch":
            message += (
                "\nnext_action: Read the current complete file with workspace_read "
                "and set expected_text to that exact content before retrying."
            )
    if (
        result_code == "workspace_capture_rejected"
        and reason == "workspace_generation_conflict"
        and capture_conflict_workspace_id is not None
        and capture_conflict_supplied_generation is not None
        and capture_conflict_observed_generation is not None
    ):
        message += (
            f"\nworkspace_id: {capture_conflict_workspace_id}"
            f"\nsupplied_expected_generation: {capture_conflict_supplied_generation}"
            f"\nobserved_current_generation: {capture_conflict_observed_generation}"
            "\ncapture_occurred: false"
            "\nnext_action: Refresh workspace_status; if capture is still intended "
            "and the workspace is ready, use its current generation as "
            "expected_generation."
        )
    if result_code == "execution_failed" and diagnostic is not None:
        suffix = f"Kodi {diagnostic.code}"
        if diagnostic.message is not None:
            suffix += f": {diagnostic.message}"
        message = f"execution_failed: jsonrpc_error ({suffix})"
        if addon_exception is not None:
            observation = addon_exception.observation_text()
            if observation is not None:
                message += f"\n{observation}"
    return CallToolResult(
        content=[TextContent(text=message)],
        is_error=True,
    )


def _audit_outcome_for_tool_error(result_code: str) -> str:
    if result_code in _MCP_AUDIT_AUTHORIZATION_CODES or result_code.endswith(
        "_not_found_or_not_authorized"
    ):
        return "authorization_denied"
    return "tool_error"


def mcp_audit_request_correlation(
    protocol_request_id: str | int | UUID | None,
) -> dict[str, str | int]:
    """Return bounded non-reversible JSON-RPC correlation metadata."""

    if protocol_request_id is None:
        return {"type": "missing"}
    if isinstance(protocol_request_id, int) and not isinstance(
        protocol_request_id, bool
    ):
        request_type = "integer"
        value = str(protocol_request_id).encode("ascii")
    else:
        request_type = "string"
        value = str(protocol_request_id).encode("utf-8", "surrogatepass")
    digest_input = request_type.encode("ascii") + b":" + value
    return {
        "type": request_type,
        "byte_length": len(value),
        "sha256": hashlib.sha256(digest_input).hexdigest(),
    }


def _bounded_audit_tool(tool_name: str) -> tuple[str, dict[str, str | int] | None]:
    if tool_name in _MCP_AUDIT_SAFE_TOOL_NAMES:
        return tool_name, None
    return "unrecognized", mcp_audit_request_correlation(tool_name)


def _bounded_audit_code(value: str) -> str:
    return value if _MCP_AUDIT_SAFE_CODE.fullmatch(value) else "unclassified"


def _bounded_audit_integer(value: int | None) -> int | dict[str, str | int] | None:
    if value is None or -(2**63) <= value <= 2**63 - 1:
        return value
    return mcp_audit_request_correlation(value)


def _audit_safely(
    principal: McpPrincipal,
    tool_name: str,
    target_id: UUID | None,
    started_at: str,
    outcome: str,
    *,
    result_code: str,
    protocol_request_id: str | int | None = None,
    source_id: UUID | None = None,
    workspace_id: UUID | None = None,
    request_id: UUID | None = None,
    generation: int | None = None,
    promotion_plan_id: UUID | None = None,
) -> None:
    try:
        _audit(
            principal,
            tool_name,
            target_id,
            started_at,
            outcome,
            result_code=result_code,
            protocol_request_id=protocol_request_id,
            source_id=source_id,
            workspace_id=workspace_id,
            request_id=request_id,
            generation=generation,
            promotion_plan_id=promotion_plan_id,
        )
    except Exception:
        # The request result or original exception remains canonical if the
        # operational logging pipeline fails. This constant fallback contains
        # no caller-controlled material and is itself best effort.
        with suppress(Exception):
            logger.error("mcp_audit_emit_failed")


def _audit(
    principal: McpPrincipal,
    tool_name: str,
    target_id: UUID | None,
    started_at: str,
    outcome: str,
    *,
    result_code: str,
    protocol_request_id: str | int | None = None,
    source_id: UUID | None = None,
    workspace_id: UUID | None = None,
    request_id: UUID | None = None,
    generation: int | None = None,
    promotion_plan_id: UUID | None = None,
) -> None:
    safe_tool, tool_correlation = _bounded_audit_tool(tool_name)
    record: dict[str, object] = {
        "schema_version": _MCP_AUDIT_SCHEMA_VERSION,
        "event": "mcp_request",
        "client_id": str(principal.client_id),
        "credential_id": str(principal.credential_id),
        "tool": safe_tool,
        "target_id": None if target_id is None else str(target_id),
        "protocol_request": mcp_audit_request_correlation(protocol_request_id),
        "capability": _tool_capability(safe_tool),
        "source_id": None if source_id is None else str(source_id),
        "workspace_id": None if workspace_id is None else str(workspace_id),
        "request_id": None if request_id is None else str(request_id),
        "generation": _bounded_audit_integer(generation),
        "plan_id": None if promotion_plan_id is None else str(promotion_plan_id),
        "started_at": started_at,
        "completed_at": _timestamp(),
        "outcome": _bounded_audit_code(outcome),
        "result_code": _bounded_audit_code(result_code),
    }
    if tool_correlation is not None:
        record["tool_correlation"] = tool_correlation
    logger.info(
        "mcp_request %s",
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


def _tool_capability(tool_name: str) -> str:
    return {
        "target_list": "target_inspect",
        "target_status": "target_inspect",
        "inspect_ui_state": "target_inspect",
        "addon_list": "addon_inventory",
        "remote_action": "semantic_remote",
        "screenshot_capture": "screenshot",
    }.get(tool_name, "not_applicable")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

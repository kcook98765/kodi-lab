"""Bounded modern MCP stdio proxy to Kodi Lab's loopback HTTP authority."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from urllib.parse import SplitResult, urlsplit

import anyio
import httpx2
from mcp_types import (
    CONNECTION_CLOSED,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    JSONRPCNotification,
    JSONRPCRequest,
    RequestId,
    jsonrpc_message_adapter,
)
from pydantic import ValidationError

from kodi_lab.mcp.transport import (
    MCP_MAX_IMAGE_BASE64_BYTES,
    MCP_MAX_REQUEST_BODY,
    MCP_PROTOCOL_VERSION,
)

TOKEN_ENVIRONMENT_VARIABLE = "KODI_LAB_MCP_TOKEN"
ENDPOINT_ENVIRONMENT_VARIABLE = "KODI_LAB_MCP_URL"
DEFAULT_MCP_ENDPOINT = "http://127.0.0.1:8100/mcp"
STDIO_MAX_IN_FLIGHT = 4
STDIO_QUEUE_CAPACITY = 8
MAX_HTTP_RESPONSE_BODY = MCP_MAX_IMAGE_BASE64_BYTES + (64 * 1024)

_MISSING_TOKEN = "Kodi Lab MCP credential is missing or malformed."
_INVALID_ENDPOINT = "Kodi Lab MCP endpoint configuration is invalid."
_AUTHORITY_UNAVAILABLE = "Kodi Lab MCP authority is unavailable."
_AUTHORITY_REJECTED = "Kodi Lab MCP authority rejected the request."


class _AsyncBinaryReader(Protocol):
    async def readline(self, size: int = -1) -> bytes: ...


class _AsyncBinaryWriter(Protocol):
    async def write(self, data: bytes) -> int: ...

    async def flush(self) -> None: ...


class _ThreadedBinaryReader:
    """Bounded reads from the process pipe without event-loop blocking."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    async def readline(self, size: int = -1) -> bytes:
        return await anyio.to_thread.run_sync(self._stream.readline, size)


@dataclass(frozen=True, slots=True)
class StdioProxyConfiguration:
    token: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class _InboundRequest:
    body: bytes
    request_id: RequestId
    method: str
    tool_name: str | None


class StdioProxyConfigurationError(ValueError):
    pass


def load_configuration(
    environment: dict[str, str] | os._Environ[str] = os.environ,
) -> StdioProxyConfiguration:
    token = environment.get(TOKEN_ENVIRONMENT_VARIABLE, "")
    if (
        not token
        or len(token) > 512
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise StdioProxyConfigurationError(_MISSING_TOKEN)
    endpoint = environment.get(ENDPOINT_ENVIRONMENT_VARIABLE, DEFAULT_MCP_ENDPOINT)
    return StdioProxyConfiguration(token=token, endpoint=validate_endpoint(endpoint))


def validate_endpoint(value: str) -> str:
    if not value or len(value) > 256:
        raise StdioProxyConfigurationError(_INVALID_ENDPOINT)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise StdioProxyConfigurationError(_INVALID_ENDPOINT) from error
    if not _is_allowed_endpoint(parsed, port):
        raise StdioProxyConfigurationError(_INVALID_ENDPOINT)
    return parsed.geturl()


def _is_allowed_endpoint(parsed: SplitResult, port: int | None) -> bool:
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/mcp"
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


async def serve_stdio_proxy(
    stdin: _AsyncBinaryReader,
    stdout: _AsyncBinaryWriter,
    *,
    configuration: StdioProxyConfiguration,
    http_client: httpx2.AsyncClient,
) -> None:
    """Proxy bounded newline-delimited stdio requests until stdin reaches EOF."""

    send_stream, receive_stream = anyio.create_memory_object_stream[_InboundRequest](
        STDIO_QUEUE_CAPACITY
    )
    write_lock = anyio.Lock()

    async def write_response(payload: bytes) -> None:
        async with write_lock:
            await stdout.write(payload)
            await stdout.write(b"\n")
            await stdout.flush()

    async def read_requests(task_group: anyio.abc.TaskGroup) -> None:
        async with send_stream:
            while True:
                line = await stdin.readline(MCP_MAX_REQUEST_BODY + 2)
                if not line:
                    task_group.cancel_scope.cancel()
                    return
                if not line.endswith(b"\n") and len(line) > MCP_MAX_REQUEST_BODY:
                    await _drain_oversized_frame(stdin)
                    await write_response(
                        _error_response(None, INVALID_REQUEST, "Request too large")
                    )
                    continue
                body = line[:-1] if line.endswith(b"\n") else line
                if len(body) > MCP_MAX_REQUEST_BODY:
                    await write_response(
                        _error_response(None, INVALID_REQUEST, "Request too large")
                    )
                    continue
                request = _parse_request(body)
                if isinstance(request, bytes):
                    await write_response(request)
                elif request is not None:
                    await send_stream.send(request)

    async def worker(
        worker_stream: anyio.abc.ObjectReceiveStream[_InboundRequest],
    ) -> None:
        async with worker_stream:
            async for request in worker_stream:
                response = await _proxy_request(
                    request,
                    configuration=configuration,
                    http_client=http_client,
                )
                await write_response(response)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(read_requests, task_group)
        for _index in range(STDIO_MAX_IN_FLIGHT):
            task_group.start_soon(worker, receive_stream.clone())
        await receive_stream.aclose()


async def _drain_oversized_frame(stdin: _AsyncBinaryReader) -> None:
    while True:
        remainder = await stdin.readline(MCP_MAX_REQUEST_BODY + 2)
        if not remainder or remainder.endswith(b"\n"):
            return


def _parse_request(body: bytes) -> _InboundRequest | bytes | None:
    try:
        message = jsonrpc_message_adapter.validate_json(body, by_name=False)
    except ValidationError:
        return _error_response(None, PARSE_ERROR, "Invalid JSON-RPC request")
    if isinstance(message, JSONRPCNotification):
        # Modern MCP has no client-to-server application notifications. In
        # particular, initialized is never translated into session state.
        return None
    if not isinstance(message, JSONRPCRequest):
        return _error_response(None, INVALID_REQUEST, "Invalid JSON-RPC request")
    if not _safe_header_value(message.method, maximum=128):
        return _error_response(message.id, INVALID_REQUEST, "Invalid MCP method")
    tool_name: str | None = None
    if message.method == "tools/call" and isinstance(message.params, dict):
        candidate = message.params.get("name")
        if isinstance(candidate, str):
            if not _safe_header_value(candidate, maximum=64):
                return _error_response(message.id, INVALID_REQUEST, "Invalid tool name")
            tool_name = candidate
    return _InboundRequest(
        body=body,
        request_id=message.id,
        method=message.method,
        tool_name=tool_name,
    )


def _safe_header_value(value: str, *, maximum: int) -> bool:
    return (
        bool(value)
        and len(value) <= maximum
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


async def _proxy_request(
    request: _InboundRequest,
    *,
    configuration: StdioProxyConfiguration,
    http_client: httpx2.AsyncClient,
) -> bytes:
    headers = {
        "Authorization": f"Bearer {configuration.token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": request.method,
    }
    if request.tool_name is not None:
        headers["Mcp-Name"] = request.tool_name
    try:
        async with http_client.stream(
            "POST",
            configuration.endpoint,
            headers=headers,
            content=request.body,
        ) as response:
            if response.headers.get("mcp-session-id") is not None:
                return _error_response(
                    request.request_id, INTERNAL_ERROR, _AUTHORITY_REJECTED
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_HTTP_RESPONSE_BODY:
                    return _error_response(
                        request.request_id, INTERNAL_ERROR, _AUTHORITY_REJECTED
                    )
                body.extend(chunk)
    except httpx2.HTTPError:
        return _error_response(
            request.request_id, CONNECTION_CLOSED, _AUTHORITY_UNAVAILABLE
        )
    content = bytes(body)
    try:
        reply = jsonrpc_message_adapter.validate_json(content, by_name=False)
    except ValidationError:
        return _error_response(request.request_id, INTERNAL_ERROR, _AUTHORITY_REJECTED)
    if getattr(reply, "id", object()) != request.request_id:
        return _error_response(request.request_id, INTERNAL_ERROR, _AUTHORITY_REJECTED)
    return content


def _error_response(request_id: RequestId | None, code: int, message: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        },
        separators=(",", ":"),
    ).encode("utf-8")


async def _run(configuration: StdioProxyConfiguration) -> None:
    stdin = _ThreadedBinaryReader(sys.stdin.buffer)
    stdout = anyio.wrap_file(sys.stdout.buffer)
    timeout = httpx2.Timeout(30.0)
    async with httpx2.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        await serve_stdio_proxy(
            stdin,
            stdout,
            configuration=configuration,
            http_client=client,
        )


def main() -> int:
    try:
        configuration = load_configuration()
    except StdioProxyConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    try:
        anyio.run(_run, configuration)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

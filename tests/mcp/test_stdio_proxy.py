from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from kodi_lab.mcp.stdio_proxy import (
    DEFAULT_MCP_ENDPOINT,
    ENDPOINT_ENVIRONMENT_VARIABLE,
    MAX_HTTP_RESPONSE_BODY,
    STDIO_MAX_IN_FLIGHT,
    STDIO_QUEUE_CAPACITY,
    TOKEN_ENVIRONMENT_VARIABLE,
    StdioProxyConfiguration,
    StdioProxyConfigurationError,
    _parse_request,
    _proxy_request,
    load_configuration,
    validate_endpoint,
)

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _request(method: str, params: dict[str, object] | None = None) -> bytes:
    materialized = {} if params is None else dict(params)
    materialized["_meta"] = META
    return json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": method, "params": materialized},
        separators=(",", ":"),
    ).encode()


def test_configuration_requires_environment_token_and_loopback_endpoint() -> None:
    with pytest.raises(StdioProxyConfigurationError, match="missing or malformed"):
        load_configuration({})
    with pytest.raises(StdioProxyConfigurationError, match="missing or malformed"):
        load_configuration({TOKEN_ENVIRONMENT_VARIABLE: "x\nsecret"})
    configured = load_configuration({TOKEN_ENVIRONMENT_VARIABLE: "opaque-token"})
    assert configured.endpoint == DEFAULT_MCP_ENDPOINT
    assert configured.token == "opaque-token"

    for rejected in (
        "https://127.0.0.1:8100/mcp",
        "http://192.0.2.1:8100/mcp",
        "http://user@127.0.0.1:8100/mcp",
        "http://127.0.0.1:8100/other",
        "http://127.0.0.1:8100/mcp?token=value",
    ):
        with pytest.raises(StdioProxyConfigurationError, match="endpoint"):
            validate_endpoint(rejected)
    assert (
        load_configuration(
            {
                TOKEN_ENVIRONMENT_VARIABLE: "opaque-token",
                ENDPOINT_ENVIRONMENT_VARIABLE: "http://localhost:9000/mcp",
            }
        ).endpoint
        == "http://localhost:9000/mcp"
    )


def test_proxy_preserves_modern_request_and_adds_bearer_to_every_request() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
        )

    async def exercise() -> bytes:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler)
        ) as client:
            parsed = _parse_request(_request("tools/list"))
            assert not isinstance(parsed, bytes) and parsed is not None
            return await _proxy_request(
                parsed,
                configuration=StdioProxyConfiguration(
                    token="secret-token", endpoint=DEFAULT_MCP_ENDPOINT
                ),
                http_client=client,
            )

    response = json.loads(asyncio.run(exercise()))
    assert response == {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer secret-token"
    assert seen[0].headers["mcp-protocol-version"] == "2026-07-28"
    assert seen[0].headers["mcp-method"] == "tools/list"
    assert seen[0].content == _request("tools/list")


def test_stdio_rejects_session_notifications_and_bounds_adapter_failures() -> None:
    initialized = json.dumps(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ).encode()
    assert _parse_request(initialized) is None
    assert STDIO_MAX_IN_FLIGHT == 4
    assert STDIO_QUEUE_CAPACITY == 8
    assert MAX_HTTP_RESPONSE_BODY == 128 * 1024

    def unavailable(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("sensitive loopback details")

    async def exercise() -> bytes:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(unavailable)
        ) as client:
            parsed = _parse_request(_request("server/discover"))
            assert not isinstance(parsed, bytes) and parsed is not None
            return await _proxy_request(
                parsed,
                configuration=StdioProxyConfiguration(
                    token="never-reflect-this", endpoint=DEFAULT_MCP_ENDPOINT
                ),
                http_client=client,
            )

    response = asyncio.run(exercise())
    assert b"Kodi Lab MCP authority is unavailable" in response
    assert b"never-reflect-this" not in response
    assert b"sensitive loopback details" not in response

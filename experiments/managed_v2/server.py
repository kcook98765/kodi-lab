"""Loopback-only, bearer-protected MCP endpoint for the Omega experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextvars import ContextVar
from pathlib import Path

import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ListToolsResult, TextContent, Tool
from starlette.responses import Response

from .broker import BrokerClient
from .protocol import FIELDS, READ, Refusal, validate


def application(broker):
    authorization = ContextVar("managed_v2_authorization", default=("", False))

    async def list_tools(_context, _params):
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description="Omega experiment " + name.replace("_", " "),
                    input_schema={
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in params},
                        "required": list(params),
                        "additionalProperties": False,
                    },
                )
                for name, params in FIELDS.items()
                if authorization.get()[1] or name in READ
            ]
        )

    async def call(_context, params):
        args = params.arguments or {}
        try:
            validate(params.name, args)
            result = await asyncio.to_thread(
                broker.request, authorization.get()[0], params.name, args
            )
            failed = isinstance(result["result"], dict) and (
                result["result"].get("state") in {"failed", "unconfirmed"}
                or bool(result["result"].get("error"))
            )
        except Refusal as error:
            result, failed = {"error": str(error)}, True
        except Exception:
            logging.getLogger(__name__).exception(
                "Managed tool %s outcome unconfirmed", params.name
            )
            result, failed = {"error": "operation_outcome_unconfirmed"}, True
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))], is_error=failed
        )

    server = Server(
        name="kodi-lab-managed-v2",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call,
    )
    sdk = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
        max_request_body_size=800_000,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*"],
            allowed_origins=["http://127.0.0.1:*"],
        ),
    )

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await sdk(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        try:
            if not supplied.startswith(b"Bearer "):
                raise Refusal("broker_access_denied")
            token = supplied[7:].decode("ascii")
            access = await asyncio.to_thread(broker.request, token, "authorize", {})
        except UnicodeError:
            await Response(status_code=401)(scope, receive, send)
            return
        except Refusal as exc:
            status = 401 if str(exc) == "broker_access_denied" else 503
            await Response(status_code=status)(scope, receive, send)
            return
        context = authorization.set((token, access["write"]))
        try:
            await sdk(scope, receive, send)
        finally:
            authorization.reset(context)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("socket", type=Path)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    uvicorn.run(
        application(BrokerClient(args.socket)), host="127.0.0.1", port=args.port
    )


if __name__ == "__main__":
    main()

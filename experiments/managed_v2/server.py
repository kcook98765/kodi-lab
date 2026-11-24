"""Loopback-only, bearer-protected MCP endpoint for the Omega experiment."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import secrets
from contextvars import ContextVar
from pathlib import Path

import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ListToolsResult, TextContent, Tool
from starlette.responses import Response

from .controller import Controller
from .runtime import Refusal, atomic

READ = {"managed_status", "snapshot_list", "addon_inventory"}
FIELDS = {
    "managed_prepare": (),
    "managed_start": (),
    "managed_stop": (),
    "managed_reset": (),
    "managed_status": (),
    "snapshot_list": (),
    "snapshot_save": ("name",),
    "snapshot_load": ("name",),
    "snapshot_delete": ("name",),
    "workspace_package": ("addon",),
    "workspace_deploy": ("addon",),
    "addon_install": ("addon", "version", "digest"),
    "package_import": ("encoded", "digest"),
    "addon_launch": ("addon",),
    "addon_setting": ("addon", "key", "value"),
    "addon_inventory": (),
    "managed_skin": ("addon",),
}


def application(controller, owner_token, observer_token):
    owner = ContextVar("managed_v2_owner", default=False)

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
                if owner.get() or name in READ
            ]
        )

    async def call(_context, params):
        args = params.arguments or {}
        try:
            if (
                params.name not in FIELDS
                or set(args) != set(FIELDS[params.name])
                or any(not isinstance(value, str) for value in args.values())
            ):
                raise Refusal("invalid_arguments")
            result = await asyncio.to_thread(
                controller.perform, params.name, args, write=owner.get()
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
        write = hmac.compare_digest(supplied, b"Bearer " + owner_token)
        if not write and not hmac.compare_digest(supplied, b"Bearer " + observer_token):
            await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, send
            )
            return
        context = owner.set(write)
        try:
            await sdk(scope, receive, send)
        finally:
            owner.reset(context)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    state = Controller(args.root)
    tokens = [secrets.token_urlsafe(32).encode() for _ in range(2)]
    for key, credential in zip(("owner", "observer"), tokens, strict=True):
        atomic(state.runtime.root / (key + ".token"), credential)
    try:
        uvicorn.run(application(state, *tokens), host="127.0.0.1", port=args.port)
    finally:
        for key in ("owner", "observer"):
            (state.runtime.root / (key + ".token")).unlink(missing_ok=True)


if __name__ == "__main__":
    main()

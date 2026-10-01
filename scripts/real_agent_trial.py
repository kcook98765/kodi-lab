#!/usr/bin/env python3
"""Run one bounded, credential-free-recorded Kodi Lab MCP agent trial.

Use a short-lived, narrowly granted MCP client. The credential file must be
owned by the current user and readable only by that user. This utility never
creates Kodi Lab authority or sends its MCP credential to the model endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TARGET_FIELDS = (
    "target_id",
    "display_name",
    "target_class",
    "kodi_version",
    "provision_state",
    "runtime_state",
    "process_state",
    "jsonrpc_state",
    "usability_state",
    "readiness_state",
    "capabilities",
)


def endpoint_identity(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint_must_have_no_credentials_or_query")
    return parsed.geturl().rstrip("/")


def credential_from_file(path: Path) -> str:
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("credential_file_permissions_invalid")
    payload = json.loads(path.read_text())
    token = payload.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("credential_file_invalid")
    return token


def safe_tool_result(name: str, payload: Any) -> dict[str, Any]:
    """Persist bounded public facts, never raw MCP content or metadata."""
    if not isinstance(payload, dict):
        return {"classification": "unstructured_result"}
    if name == "target_list":
        targets = payload.get("targets", [])
        if not isinstance(targets, list):
            return {"classification": "invalid_target_list"}
        return {
            "targets": [
                {key: item[key] for key in _TARGET_FIELDS if key in item}
                for item in targets[:100]
                if isinstance(item, dict)
            ],
            "has_next_cursor": payload.get("next_cursor") is not None,
        }
    if name == "target_status":
        item = payload.get("target", {})
        return {
            "target": {key: item[key] for key in _TARGET_FIELDS if key in item}
            if isinstance(item, dict)
            else {},
        }
    if name == "addon_list":
        addons = payload.get("addons", [])
        if not isinstance(addons, list):
            return {"classification": "invalid_addon_list"}
        bridge = next(
            (
                item
                for item in addons
                if isinstance(item, dict)
                and item.get("addon_id") == "service.kodi_lab_bridge"
            ),
            None,
        )
        types = Counter(
            str(item.get("addon_type", "unknown"))[:128]
            for item in addons
            if isinstance(item, dict)
        )
        return {
            "target_id": payload.get("target_id"),
            "total_count": payload.get("total_count"),
            "page_count": len(addons),
            "types_on_page": dict(sorted(types.items())),
            "bridge_on_page": (
                {key: bridge.get(key) for key in ("addon_id", "version", "enabled")}
                if bridge
                else None
            ),
            "has_next_cursor": payload.get("next_cursor") is not None,
        }
    return {"classification": "unsupported_result"}


def safe_mcp_error(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {"code": "unknown_mcp_error"}
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return {"code": "unknown_mcp_error"}
    result = {}
    for key in ("code", "reason"):
        value = error.get(key)
        if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
            result[key] = value
    return result or {"code": "unknown_mcp_error"}


def safe_arguments(name: str, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool_arguments_not_object")
    allowed = {
        "target_list": {"cursor", "limit"},
        "target_status": {"target_id"},
        "addon_list": {"target_id", "cursor", "limit"},
    }.get(name)
    if allowed is None or not set(arguments) <= allowed:
        raise ValueError("tool_arguments_out_of_scope")
    for key, value in arguments.items():
        if key == "limit":
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("tool_arguments_invalid")
        elif not isinstance(value, str) or len(value) > 128:
            raise ValueError("tool_arguments_invalid")
    return arguments


def answer_is_complete(choice: dict[str, Any]) -> bool:
    message = choice.get("message", {})
    return (
        choice.get("finish_reason") != "length"
        and isinstance(message.get("content"), str)
        and bool(message["content"].strip())
    )


def bounded_model_tool_payload(payload: Any, *, max_chars: int = 64000) -> str:
    encoded = json.dumps(payload, ensure_ascii=True)
    if len(encoded) > max_chars:
        return '{"error":{"code":"tool_result_too_large"}}'
    return encoded


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-endpoint", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--mcp-endpoint", required=True)
    p.add_argument("--credential-file", type=Path, required=True)
    p.add_argument("--client-id", required=True)
    p.add_argument("--preset-id", required=True)
    p.add_argument("--target-id", required=True)
    p.add_argument("--target-role", required=True)
    p.add_argument("--tools", required=True, help="comma-separated exposed tools")
    p.add_argument("--task", required=True)
    p.add_argument("--expected", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--max-turns", type=int, default=8)
    p.add_argument("--timeout", type=int, default=120)
    return p


async def run(args: argparse.Namespace, record: dict[str, Any], token: str) -> None:
    exposed = set(record["tools_exposed"])
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as http:
        streams = streamable_http_client(
            args.mcp_endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="auto", cache=None) as mcp:
            listed = await mcp.list_tools()
            available = {tool.name: tool for tool in listed.tools}
            if set(available) != exposed:
                raise ValueError("discovered_tool_surface_mismatch")
            definitions = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": available[name].description or name,
                        "parameters": available[name].input_schema,
                    },
                }
                for name in sorted(exposed)
            ]
            messages: list[dict[str, Any]] = [
                {
                    "role": "system",
                    "content": (
                        "Use only the provided Kodi Lab tools and their results. "
                        "A target list may contain unprobed live state; report fresh "
                        "health only when observed. Do not claim an action occurred "
                        "without an appropriate successful tool result."
                    ),
                },
                {"role": "user", "content": args.task},
            ]
            async with httpx.AsyncClient(timeout=60) as model_http:
                for turn in range(1, args.max_turns + 1):
                    response = await model_http.post(
                        args.model_endpoint + "/chat/completions",
                        json={
                            "model": args.model,
                            "messages": messages,
                            "tools": definitions,
                            "tool_choice": "auto",
                            "temperature": 0,
                            "max_tokens": 4096,
                        },
                    )
                    response.raise_for_status()
                    body = response.json()
                    choice = body["choices"][0]
                    message = choice["message"]
                    calls = message.get("tool_calls") or []
                    record["turn_count"] = turn
                    record["events"].append(
                        {
                            "kind": "model",
                            "turn": turn,
                            "tool_names": [
                                call.get("function", {}).get("name") for call in calls
                            ],
                            "finish_reason": choice.get("finish_reason"),
                        }
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content"),
                            "tool_calls": calls,
                        }
                        if calls
                        else {
                            "role": "assistant",
                            "content": message.get("content") or "",
                        }
                    )
                    if not calls:
                        content = message.get("content") or ""
                        if not answer_is_complete(choice):
                            record["retries"] += 1
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "Continue this task. Finish the inspection "
                                        "and give a factual final answer."
                                    ),
                                }
                            )
                            continue
                        record["final_answer"] = str(content)[:6000]
                        record["completion_status"] = "answered"
                        return
                    for call in calls:
                        name = call.get("function", {}).get("name")
                        if name not in exposed:
                            result_for_model = {"error": {"code": "tool_not_exposed"}}
                            event = {
                                "kind": "tool",
                                "turn": turn,
                                "name": name,
                                "error": {"code": "tool_not_exposed"},
                            }
                        else:
                            try:
                                arguments = safe_arguments(
                                    name, json.loads(call["function"]["arguments"])
                                )
                                actual = await mcp.call_tool(name, arguments)
                                structured = actual.structured_content
                                if actual.is_error:
                                    result_for_model = {
                                        "error": safe_mcp_error(structured)
                                    }
                                    event = {
                                        "kind": "tool",
                                        "turn": turn,
                                        "name": name,
                                        "arguments": arguments,
                                        "error": result_for_model["error"],
                                    }
                                else:
                                    result_for_model = structured
                                    event = {
                                        "kind": "tool",
                                        "turn": turn,
                                        "name": name,
                                        "arguments": arguments,
                                        "result": safe_tool_result(name, structured),
                                    }
                            except (ValueError, KeyError, TypeError):
                                result_for_model = {
                                    "error": {"code": "invalid_tool_arguments"}
                                }
                                event = {
                                    "kind": "tool",
                                    "turn": turn,
                                    "name": name,
                                    "error": {"code": "invalid_tool_arguments"},
                                }
                        record["events"].append(event)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": bounded_model_tool_payload(result_for_model),
                            }
                        )
            record["completion_status"] = "turn_limit"


def main() -> int:
    args = parser().parse_args()
    args.model_endpoint = endpoint_identity(args.model_endpoint)
    args.mcp_endpoint = endpoint_identity(args.mcp_endpoint)
    if not 1 <= args.max_turns <= 16 or not 10 <= args.timeout <= 600:
        raise ValueError("trial_bounds_invalid")
    tools = args.tools.split(",")
    if len(tools) != len(set(tools)) or not tools:
        raise ValueError("tool_allowlist_invalid")
    token = credential_from_file(args.credential_file)
    record: dict[str, Any] = {
        "schema": "kodi-lab-real-agent-trial-v1",
        "run_id": str(uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "model_endpoint": args.model_endpoint,
        "model_id": args.model,
        "mcp_endpoint": args.mcp_endpoint,
        "target_id": args.target_id,
        "target_role": args.target_role,
        "client_id": args.client_id,
        "preset_id": args.preset_id,
        "tools_exposed": tools,
        "task": args.task,
        "expected_behavior": args.expected,
        "events": [],
        "retries": 0,
        "final_answer": None,
        "completion_status": "started",
        "turn_count": 0,
        "human_intervention": False,
        "evaluator_result": "ungraded",
        "failure_class": None,
    }
    started = time.monotonic()
    try:
        asyncio.run(asyncio.wait_for(run(args, record, token), timeout=args.timeout))
    except TimeoutError:
        record["completion_status"] = "timeout"
        record["failure_class"] = "environment"
    except Exception as error:
        record["completion_status"] = "runner_error"
        record["failure_class"] = "harness"
        record["error_type"] = type(error).__name__
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, ensure_ascii=True, indent=2)
            output.write("\n")
    print(
        json.dumps(
            {
                "run_id": record["run_id"],
                "status": record["completion_status"],
                "turns": record["turn_count"],
                "seconds": record["elapsed_seconds"],
                "output": str(args.output),
            }
        )
    )
    return 0 if record["completion_status"] == "answered" else 1


if __name__ == "__main__":
    raise SystemExit(main())

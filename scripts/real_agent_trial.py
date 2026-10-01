#!/usr/bin/env python3
"""Run one bounded, credential-free-recorded Kodi Lab MCP agent trial.

Use a short-lived, narrowly granted MCP client. The credential file must be
owned by the current user and readable only by that user. This utility never
creates Kodi Lab authority or sends its MCP credential to the model endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import struct
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ImageContent

from kodi_lab.kodi.remote import BrowserRemoteAction

_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
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
    if name == "remote_action":
        return {
            key: payload[key]
            for key in (
                "request_id",
                "invocation_id",
                "target_id",
                "action",
                "outcome",
                "completed_at",
            )
            if key in payload
        }
    if name == "screenshot_capture":
        return {
            key: payload[key]
            for key in (
                "target_id",
                "capture_id",
                "captured_at",
                "width",
                "height",
                "mime_type",
                "byte_size",
            )
            if key in payload
        }
    return {"classification": "unsupported_result"}


def safe_mcp_error(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {"code": "unknown_mcp_error"}
    error = payload.get("error", payload)
    if isinstance(error, str) and _SAFE_CODE.fullmatch(error):
        result = {"code": error}
        reason = payload.get("reason")
        if isinstance(reason, str) and _SAFE_CODE.fullmatch(reason):
            result["reason"] = reason
        return result
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
    if name == "remote_action":
        if set(arguments) != {"request_id", "target_id", "action"}:
            raise ValueError("tool_arguments_out_of_scope")
        for key in ("request_id", "target_id"):
            value = arguments[key]
            if not isinstance(value, str):
                raise ValueError("tool_arguments_invalid")
            try:
                if str(UUID(value)) != value:
                    raise ValueError("noncanonical_uuid")
            except ValueError as error:
                raise ValueError("tool_arguments_invalid") from error
        try:
            BrowserRemoteAction(arguments["action"])
        except (ValueError, TypeError) as error:
            raise ValueError("tool_arguments_invalid") from error
        return arguments
    if name == "screenshot_capture":
        if not {"target_id"} <= set(arguments) <= {"target_id", "request_id"}:
            raise ValueError("tool_arguments_out_of_scope")
        for key in arguments:
            value = arguments[key]
            if not isinstance(value, str):
                raise ValueError("tool_arguments_invalid")
            try:
                if str(UUID(value)) != value:
                    raise ValueError("noncanonical_uuid")
            except ValueError as error:
                raise ValueError("tool_arguments_invalid") from error
        return arguments
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


def remote_action_allowed(
    arguments: dict[str, Any],
    *,
    trial_target_id: str,
    allowed_actions: set[str],
    dispatched_count: int,
    max_dispatches: int,
) -> bool:
    return (
        arguments["target_id"] == trial_target_id
        and arguments["action"] in allowed_actions
        and dispatched_count < max_dispatches
    )


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


def visual_tool_payload(
    content: list[Any], metadata: Any, artifact_path: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate and retain exactly one PNG; deliver its pixels to the model."""
    images = [item for item in content if isinstance(item, ImageContent)]
    if len(images) != 1 or images[0].mime_type != "image/png":
        raise ValueError("screenshot_invalid")
    try:
        raw = base64.b64decode(images[0].data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("screenshot_invalid") from error
    if (
        raw[:8] != _PNG_SIGNATURE
        or not 45 <= len(raw) <= _MAX_SCREENSHOT_BYTES
        or raw[12:16] != b"IHDR"
        or not isinstance(metadata, dict)
    ):
        raise ValueError("screenshot_invalid")
    width, height = struct.unpack(">II", raw[16:24])
    if (
        not 1 <= width <= 4096
        or not 1 <= height <= 2160
        or metadata.get("width") != width
        or metadata.get("height") != height
        or metadata.get("byte_size") != len(raw)
        or metadata.get("mime_type") != "image/png"
    ):
        raise ValueError("screenshot_invalid")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(artifact_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
    evidence = {
        "artifact": artifact_path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_size": len(raw),
        "width": width,
        "height": height,
        "capture_id": metadata.get("capture_id"),
    }
    model_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": bounded_model_tool_payload(
                safe_tool_result("screenshot_capture", metadata)
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + images[0].data,
                "detail": "high",
            },
        },
    ]
    return model_content, evidence


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
    p.add_argument("--allowed-remote-actions", default="")
    p.add_argument("--max-remote-actions", type=int, default=0)
    p.add_argument("--max-screenshots", type=int, default=0)
    return p


async def run(args: argparse.Namespace, record: dict[str, Any], token: str) -> None:
    exposed = set(record["tools_exposed"])
    allowed_remote_actions = set(filter(None, args.allowed_remote_actions.split(",")))
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=30
    ) as http:
        streams = streamable_http_client(
            args.mcp_endpoint, http_client=http, terminate_on_close=False
        )
        async with Client(streams, mode="auto", cache=None) as mcp:
            listed = await mcp.list_tools()
            available = {tool.name: tool for tool in listed.tools}
            if not exposed <= set(available):
                raise ValueError("discovered_tool_surface_mismatch")
            record["server_discovered_tools"] = sorted(available)
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
            async with httpx.AsyncClient(timeout=90) as model_http:
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
                                        "Continue this task and give a factual "
                                        "final answer."
                                    ),
                                }
                            )
                            continue
                        record["final_answer"] = str(content)[:6000]
                        record["completion_status"] = "answered"
                        return
                    for call in calls:
                        name = call.get("function", {}).get("name")
                        tool_content: Any = None
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
                                if (
                                    name == "remote_action"
                                    and not remote_action_allowed(
                                        arguments,
                                        trial_target_id=args.target_id,
                                        allowed_actions=allowed_remote_actions,
                                        dispatched_count=record["remote_dispatches"],
                                        max_dispatches=args.max_remote_actions,
                                    )
                                ):
                                    result_for_model = {
                                        "error": {"code": "trial_action_not_allowed"}
                                    }
                                    event = {
                                        "kind": "tool",
                                        "turn": turn,
                                        "name": name,
                                        "arguments": arguments,
                                        "dispatched": False,
                                        "error": result_for_model["error"],
                                    }
                                    record["events"].append(event)
                                    messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": call["id"],
                                            "content": bounded_model_tool_payload(
                                                result_for_model
                                            ),
                                        }
                                    )
                                    continue
                                if name == "remote_action":
                                    record["remote_dispatches"] += 1
                                if name == "screenshot_capture":
                                    if (
                                        arguments["target_id"] != args.target_id
                                        or record["screenshot_dispatches"]
                                        >= args.max_screenshots
                                    ):
                                        result_for_model = {
                                            "error": {
                                                "code": "trial_capture_not_allowed"
                                            }
                                        }
                                        event = {
                                            "kind": "tool",
                                            "turn": turn,
                                            "name": name,
                                            "arguments": arguments,
                                            "dispatched": False,
                                            "error": result_for_model["error"],
                                        }
                                        record["events"].append(event)
                                        messages.append(
                                            {
                                                "role": "tool",
                                                "tool_call_id": call["id"],
                                                "content": bounded_model_tool_payload(
                                                    result_for_model
                                                ),
                                            }
                                        )
                                        continue
                                    record["screenshot_dispatches"] += 1
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
                                        "dispatched": name == "remote_action",
                                        "error": result_for_model["error"],
                                    }
                                else:
                                    result_for_model = structured
                                    event = {
                                        "kind": "tool",
                                        "turn": turn,
                                        "name": name,
                                        "arguments": arguments,
                                        "dispatched": name == "remote_action",
                                        "result": safe_tool_result(name, structured),
                                    }
                                    if name == "screenshot_capture":
                                        image_path = args.output.parent / (
                                            f"{args.output.stem}-screenshot-"
                                            f"{record['screenshot_dispatches']}.png"
                                        )
                                        try:
                                            tool_content, evidence = (
                                                visual_tool_payload(
                                                    actual.content,
                                                    structured,
                                                    image_path,
                                                )
                                            )
                                        except ValueError:
                                            result_for_model = {
                                                "error": {"code": "screenshot_invalid"}
                                            }
                                            event["error"] = result_for_model["error"]
                                            event.pop("result", None)
                                        else:
                                            record["screenshot_artifacts"].append(
                                                evidence
                                            )
                                            event["image"] = evidence
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
                                "content": (
                                    tool_content
                                    if tool_content is not None
                                    else bounded_model_tool_payload(result_for_model)
                                ),
                            }
                        )
            record["completion_status"] = "turn_limit"


def main() -> int:
    args = parser().parse_args()
    args.model_endpoint = endpoint_identity(args.model_endpoint)
    args.mcp_endpoint = endpoint_identity(args.mcp_endpoint)
    if not 1 <= args.max_turns <= 16 or not 10 <= args.timeout <= 600:
        raise ValueError("trial_bounds_invalid")
    actions = set(filter(None, args.allowed_remote_actions.split(",")))
    try:
        for action in actions:
            BrowserRemoteAction(action)
    except ValueError as error:
        raise ValueError("remote_action_bounds_invalid") from error
    if not 0 <= args.max_remote_actions <= 2 or (
        actions and args.max_remote_actions == 0
    ):
        raise ValueError("remote_action_bounds_invalid")
    if not 0 <= args.max_screenshots <= 2:
        raise ValueError("screenshot_bounds_invalid")
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
        "allowed_remote_actions": sorted(actions),
        "max_remote_actions": args.max_remote_actions,
        "remote_dispatches": 0,
        "max_screenshots": args.max_screenshots,
        "screenshot_dispatches": 0,
        "screenshot_artifacts": [],
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

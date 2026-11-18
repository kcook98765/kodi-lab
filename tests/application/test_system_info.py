"""Real adapters/authorization with bounded native JSON-RPC transports."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from kodi_lab.kodi.system_info import INFO_BOOLEANS, INFO_LABELS
from kodi_lab.mcp.authorization import McpAuthorizationError
from tests.application.test_mcp_external_targets import (
    TARGET_ID,
)
from tests.application.test_mcp_external_targets import (
    external_authority as external_authority,
)
from tests.application.test_production_inspection import (
    _Environment,
)
from tests.application.test_production_inspection import (
    production_inspection as production_inspection,
)


def _native(env: _Environment, *, during: Any = None) -> None:
    original = env.adapters._external_transport
    assert original is not None

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] not in {"XBMC.GetInfoLabels", "XBMC.GetInfoBooleans"}:
            return original.handle_request(request)
        env.calls.append(body)
        if body["method"].endswith("Labels"):
            assert body["params"] == {"labels": list(INFO_LABELS)}
            result: object = {
                "System.BuildVersion": "21.3",
                "System.Memory(total)": "4096MB",
            }
        else:
            assert body["params"] == {"booleans": list(INFO_BOOLEANS)}
            result = {"System.HasNetwork": True}
        if during is not None:
            during(body["method"])
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    env.adapters._external_transport = httpx.MockTransport(handler)


def test_production_native_only_with_emergency_and_mutation_suspension(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    _native(env)
    result = env.tools.system_info(env.principal, target_id=env.target_id)
    assert result.status == "partial"
    assert result.metrics["memory_total"].value == 4096 * 1048576
    assert result.scope == "unknown"
    assert [c["method"] for c in env.calls].count("Application.GetProperties") == 2
    assert not any("Bridge" in str(c) for c in env.calls)


@pytest.mark.parametrize("missing", ["grant", "scope", "disabled", "wrong_target"])
def test_production_denies_before_native_io(
    production_inspection: _Environment, missing: str
) -> None:
    env = production_inspection
    _native(env)
    target_id = env.target_id
    if missing == "grant":
        env.administration.remove_target_grant(env.client_id, target_id)
    elif missing == "scope":
        env.administration.revoke_production_inspection_scope(env.scope_id)
    elif missing == "disabled":
        env.targets.set_enabled(target_id, enabled=False)
    else:
        target_id = uuid4()
    with pytest.raises(McpAuthorizationError):
        env.tools.system_info(env.principal, target_id=target_id)
    assert env.calls == []


@pytest.mark.parametrize("fault", ["scope_revoked", "identity_drift"])
def test_production_changes_during_collection_suppress_the_snapshot(
    production_inspection: _Environment, fault: str
) -> None:
    env = production_inspection

    def during(method: str) -> None:
        if method.endswith("Booleans"):
            if fault == "scope_revoked":
                env.administration.revoke_production_inspection_scope(env.scope_id)
            else:
                env.live_name[0] = "Different Kodi"

    _native(env, during=during)
    with pytest.raises(McpAuthorizationError):
        env.tools.system_info(env.principal, target_id=env.target_id)


def test_external_development_needs_no_bridge(external_authority: Any) -> None:
    _, _, adapters = external_authority
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        results = {
            "JSONRPC.Ping": "pong",
            "Application.GetProperties": {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            },
            "XBMC.GetInfoLabels": {"System.BuildVersion": "21.3"},
            "XBMC.GetInfoBooleans": {"System.HasNetwork": True},
        }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": results[method]}
        )

    adapters._external_transport = httpx.MockTransport(handler)
    result = adapters.system_info(TARGET_ID)
    assert result.metrics["kodi_version"].value == "21.3"
    assert calls.count("Application.GetProperties") == 2


def test_system_info_production_rate_is_bounded(
    production_inspection: _Environment,
) -> None:
    env = production_inspection
    _native(env)
    env.tools.system_info(env.principal, target_id=env.target_id)
    calls = list(env.calls)
    with pytest.raises(McpAuthorizationError, match="rate_limited"):
        env.tools.system_info(env.principal, target_id=env.target_id)
    assert env.calls == calls
    env.now[0] += 5
    assert (
        env.tools.system_info(env.principal, target_id=env.target_id).status
        == "partial"
    )

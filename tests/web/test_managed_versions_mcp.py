"""Real authenticated MCP transport exposes only explicitly granted version slots."""

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.runtime.recipes import load_reviewed_recipe, recipe_target_id
from kodi_lab.storage.models import ManagedInstanceRow, ManagedVersionOperationRow
from kodi_lab.storage.repositories import TargetRepository
from tests.application.test_managed_start import NativeReady
from tests.managed_versions_support import Broker
from tests.runtime.test_recipe_installation import _ready
from tests.storage.test_repositories import make_target
from tests.web.test_mcp import (
    _administration,
    _assert_tool_error,
    _post,
    _strict_tool_result,
)


@pytest.fixture
def broker(
    database: sessionmaker[Session], data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> Broker:
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    generate_master_key(data_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.setattr(
        "tests.web.conftest.coordinator_factory", lambda *_: NativeReady()
    )
    value = Broker(database, data_root.parent / "fixture-profiles")
    value.calls = []
    value.remote_error = None
    value.playback_active = False
    value.snapshot_data_root = data_root
    return value


def authority(
    client: TestClient,
    database: sessionmaker[Session],
    *,
    permissions: frozenset[str] = frozenset({"managed.versions"}),
    grant: bool = True,
) -> tuple[str, UUID, UUID]:
    from dataclasses import replace

    admin = _administration(client)
    _ready(database, generation="omega")
    reviewed = load_reviewed_recipe("omega")
    target = recipe_target_id(reviewed)
    with database.begin() as session:
        TargetRepository(session).create(
            replace(make_target("Omega Lab"), id=target, kodi_version="21.3")
        )
    preset = admin.create_custom_preset(
        display_name="Version wire fixture",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=permissions,
    )
    actor = admin.create_client(display_name="Wire version actor", preset_id=preset.id)
    admin.assign_target_preset(target, preset.id)
    if grant:
        admin.grant_target(actor.id, target)
    cast(
        FastAPI, client.app
    ).state.kodi_lab.managed_versions._rate = BoundedIntervalRateLimiter(0)
    return admin.issue_credential(actor.id).token, target, actor.id


def call(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = _post(
        client, token, "tools/call", params={"name": name, "arguments": arguments}
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["result"])


def test_wire_status_start_stop_use_version_without_raw_runtime_identity(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    token, target, _ = authority(client, database)
    inventory = _post(client, token, "tools/list").json()["result"]["tools"]
    tools = {tool["name"]: tool for tool in inventory}
    assert set(tools) == {"managed_status", "managed_start", "managed_stop"}
    assert set(tools["managed_start"]["inputSchema"]["properties"]) == {
        "version",
        "request_id",
    }
    assert tools["managed_start"]["inputSchema"]["additionalProperties"] is False
    status = call(client, token, "managed_status", {})
    _strict_tool_result(status, tools["managed_status"]["outputSchema"])
    omega = next(
        v for v in status["structuredContent"]["versions"] if v["version"] == "omega"
    )
    assert omega["state"] == "empty" and omega["authorized"]
    assert all(
        not v["authorized"]
        for v in status["structuredContent"]["versions"]
        if v["version"] != "omega"
    )
    request = str(uuid4())
    started = call(
        client, token, "managed_start", {"version": "omega", "request_id": request}
    )
    _strict_tool_result(started, tools["managed_start"]["outputSchema"])
    assert started["structuredContent"]["receipt_state"] == "complete", started
    context = cast(FastAPI, client.app).state.kodi_lab
    with context.readiness._lock:
        future = context.readiness._entries[target].future
    assert future is not None
    future.result(timeout=5)
    stopped = call(
        client, token, "managed_stop", {"version": "omega", "request_id": str(uuid4())}
    )
    _strict_tool_result(stopped, tools["managed_stop"]["outputSchema"])
    assert stopped["structuredContent"]["stop_confirmed"], stopped
    replay = call(
        client, token, "managed_start", {"version": "omega", "request_id": request}
    )
    assert replay["structuredContent"]["replayed"]
    assert replay["structuredContent"]["status"]["state"] == "stopped"
    assert broker.provisions == broker.starts == broker.stops == 1


@pytest.mark.parametrize(
    "scope",
    ["legacy_permission", "target_grant", "target_policy", "revoked_credential"],
)
def test_exact_opt_in_and_current_target_authority_are_required(
    client: TestClient, database: sessionmaker[Session], broker: Broker, scope: str
) -> None:
    token, target, actor = authority(
        client,
        database,
        permissions=frozenset({"managed.lifecycle"})
        if scope == "legacy_permission"
        else frozenset({"managed.versions"}),
        grant=scope != "target_grant",
    )
    admin = _administration(client)
    if scope == "target_policy":
        admin.assign_target_preset(target, FULL_LAB_CONTROL_PRESET_ID)
    if scope == "revoked_credential":
        credential = admin.get_client(actor).credentials[0]
        admin.revoke_credential(actor, credential.id)
        assert _post(client, token, "tools/list").status_code == 401
    else:
        result = call(
            client,
            token,
            "managed_start",
            {"version": "omega", "request_id": str(uuid4())},
        )
        _assert_tool_error(
            result,
            "target_not_found_or_not_authorized"
            if scope == "target_grant"
            else "permission_denied",
        )
    with database() as session:
        assert session.get(ManagedInstanceRow, str(target)) is None
        assert session.scalar(select(ManagedVersionOperationRow)) is None
    assert broker.provisions == broker.starts == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"target_id": str(uuid4())},
        {"container_id": "f" * 64},
        {"runtime_revision_id": str(uuid4())},
        {"reset": True},
    ],
)
def test_version_surface_rejects_raw_identity_and_reset_parameters(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    extra: dict[str, Any],
) -> None:
    token, _, _ = authority(client, database)
    result = call(
        client,
        token,
        "managed_start",
        {"version": "omega", "request_id": str(uuid4()), **extra},
    )
    _assert_tool_error(result, "invalid_params")
    assert broker.provisions == broker.starts == 0

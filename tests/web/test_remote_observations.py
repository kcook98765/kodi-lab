"""Managed action observation/reuse keeps replay and visual authority separate."""

import base64
import json
from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import remote_observations
from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.kodi.ui_state import UiObservation, WindowState
from kodi_lab.storage.models import ManagedInstanceRow
from tests.web.test_mcp import (
    _administration,
    _make_target_ready,
    _post,
    _strict_tool_result,
)

from .conftest import INSTANCE_ID, FakeBroker


def _agent(
    client: TestClient, *, permissions: frozenset[str] | None = None
) -> tuple[UUID, str]:
    administration = _administration(client)
    preset_id = FULL_LAB_CONTROL_PRESET_ID
    if permissions is not None:
        preset_id = administration.create_custom_preset(
            display_name="Observation permissions " + str(uuid4()),
            template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
            permissions=permissions,
        ).id
    agent = administration.create_client(
        display_name="Observer " + str(uuid4()), preset_id=preset_id
    )
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(agent.id, INSTANCE_ID)
    return agent.id, administration.issue_credential(agent.id).token


def _call(
    client: TestClient, token: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    response = _post(
        client, token, "tools/call", params={"name": name, "arguments": arguments}
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["result"])


def _action(
    client: TestClient,
    token: str,
    *,
    observe: bool = True,
    request_id: UUID | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    arguments = {
        "target_id": str(INSTANCE_ID),
        "request_id": str(request_id or uuid4()),
        "action": "left",
        "observe_ui": observe,
    }
    result = _call(client, token, "remote_action", arguments)
    assert not result.get("isError"), result
    return arguments, result["structuredContent"]


def _retrieve(
    client: TestClient, token: str, action: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    return _call(
        client,
        token,
        "screenshot_retrieve",
        {
            "target_id": action["target_id"],
            "request_id": action["request_id"],
            "capture_id": result["screenshot_refresh"]["capture_id"],
        },
    )


def _ui(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    def collect(_self: object, _target: UUID) -> UiObservation:
        calls.append(1)
        return UiObservation(
            window=WindowState(id=10025, label="Videos"), summary="Videos"
        )

    monkeypatch.setattr(KodiTargetAdapterService, "inspect_ui_state", collect)
    return calls


def test_action_returns_ui_and_reuses_one_capture_without_dispatch_or_capture_replay(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client)
    calls = _ui(monkeypatch)
    arguments, result = _action(client, token)
    observation = result["post_action_observation"]
    assert observation["status"] == "succeeded"
    assert observation["observed_at"] >= result["completed_at"]
    assert observation["ui_state"]["window"]["id"] == 10025
    assert observation["runtime_state_revision"] is not None
    reused = _retrieve(client, token, arguments, result)
    assert not reused.get("isError"), reused
    assert (
        reused["structuredContent"]["capture_id"]
        == result["screenshot_refresh"]["capture_id"]
    )
    assert reused["structuredContent"]["request_id"] == arguments["request_id"]
    assert reused["structuredContent"]["age_seconds"] <= 120
    assert len(reused["content"]) == 2
    assert reused["content"][1]["type"] == "text"
    assert json.loads(reused["content"][1]["text"]) == reused["structuredContent"]
    assert base64.b64decode(reused["content"][0]["data"]).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    catalog = {
        item["name"]: item
        for item in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    _strict_tool_result(reused, catalog["screenshot_retrieve"]["outputSchema"])
    replay = _call(client, token, "remote_action", arguments)
    assert replay["structuredContent"] == result
    assert len(broker.remote_calls) == broker.screenshot_calls == len(calls) == 1


def test_observation_failure_preserves_action_success_and_never_retries_collection(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client)
    calls: list[int] = []

    def fail(_self: object, _target: UUID) -> UiObservation:
        calls.append(1)
        raise RuntimeError("fixture transport failure")

    monkeypatch.setattr(KodiTargetAdapterService, "inspect_ui_state", fail)
    arguments, result = _action(client, token)
    assert result["outcome"] == "succeeded"
    assert result["post_action_observation"]["status"] == "failed"
    assert result["post_action_observation"]["error_code"] == "observation_unavailable"
    assert (
        _call(client, token, "remote_action", arguments)["structuredContent"] == result
    )
    assert len(calls) == len(broker.remote_calls) == broker.screenshot_calls == 1
    assert not _retrieve(client, token, arguments, result).get("isError")


def test_control_permission_does_not_imply_inspection_or_visual_access(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client, permissions=frozenset({"target.control"}))
    calls = _ui(monkeypatch)
    arguments, result = _action(client, token)
    assert result["outcome"] == "succeeded"
    assert result["post_action_observation"]["error_code"] == "observation_denied"
    assert result["post_action_observation"]["ui_state"] is None
    denied = _retrieve(client, token, arguments, result)
    assert denied["isError"] and denied["content"][0]["text"] == "permission_denied"
    assert calls == []
    assert broker.screenshot_calls == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        ("expire", "capture_expired"),
        ("generation", "capture_runtime_changed"),
        ("replacement", "capture_replaced"),
        ("client", "capture_not_retained"),
    ],
)
def test_retrieval_rejects_expired_other_client_replaced_or_changed_runtime_frames(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    reason: str,
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client)
    _ui(monkeypatch)
    arguments, result = _action(client, token)
    captures = broker.screenshot_calls
    if change == "expire":
        monkeypatch.setattr(
            remote_observations, "CAPTURE_REUSE_RETENTION", timedelta(0)
        )
    elif change == "generation":
        with database.begin() as session:
            session.execute(
                update(ManagedInstanceRow)
                .where(ManagedInstanceRow.target_id == str(INSTANCE_ID))
                .values(
                    runtime_state_revision=ManagedInstanceRow.runtime_state_revision + 1
                )
            )
    elif change == "replacement":
        context = cast(FastAPI, client.app).state.kodi_lab
        context.screenshots.capture(INSTANCE_ID)
        captures += 1
    else:
        _other_id, token = _agent(client)
    rejected = _retrieve(client, token, arguments, result)
    assert rejected["isError"]
    assert reason in rejected["content"][0]["text"]
    assert broker.screenshot_calls == captures
    assert len(broker.remote_calls) == 1


def test_later_opt_in_does_not_attach_new_ui_or_pixels_to_an_old_action(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client)
    calls = _ui(monkeypatch)
    arguments, result = _action(client, token, observe=False)
    assert result["post_action_observation"] is None
    arguments["observe_ui"] = True
    replay = _call(client, token, "remote_action", arguments)["structuredContent"]
    assert replay["post_action_observation"]["status"] == "not_retained"
    assert calls == []
    assert len(broker.remote_calls) == broker.screenshot_calls == 1


def test_current_grant_is_rechecked_before_returning_retained_pixels(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_target_ready(client)
    agent_id, token = _agent(client)
    _ui(monkeypatch)
    arguments, result = _action(client, token)
    _administration(client).remove_target_grant(agent_id, INSTANCE_ID)
    denied = _retrieve(client, token, arguments, result)
    assert denied["isError"]
    assert denied["content"][0]["text"] == "target_not_found_or_not_authorized"
    assert broker.screenshot_calls == 1


def test_capture_expiring_during_file_read_is_not_returned(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_target_ready(client)
    _id, token = _agent(client)
    _ui(monkeypatch)
    arguments, result = _action(client, token)
    context = cast(FastAPI, client.app).state.kodi_lab
    original = context.screenshots.read_capture

    def slow_read(*args: Any, **kwargs: Any) -> Any:
        observed = original(*args, **kwargs)
        monkeypatch.setattr(
            remote_observations, "CAPTURE_REUSE_RETENTION", timedelta(0)
        )
        return observed

    monkeypatch.setattr(context.screenshots, "read_capture", slow_read)
    rejected = _retrieve(client, token, arguments, result)
    assert rejected["isError"] and "capture_expired" in rejected["content"][0]["text"]
    assert broker.screenshot_calls == 1 and len(broker.remote_calls) == 1

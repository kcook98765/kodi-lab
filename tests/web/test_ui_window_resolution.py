"""Default UI inspection exposes the same enriched v2 observation on each API."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

import jsonschema  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.kodi.ui_state import DialogState, UiObservation, WindowState
from kodi_lab.kodi.ui_window_resolution import resolve_ui_observation
from kodi_lab.kodi.window_resolver import WindowResolution
from tests.web.conftest import (
    INSTANCE_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    guard_targeted_status_isolation,
    login,
)
from tests.web.test_mcp import _authority, _post

CASES = (
    "core_window",
    "reported_name",
    "core_dialog",
    "unknown_window",
    "ambiguous_window",
    "unknown_dialog",
    "ambiguous_dialog",
    "unverified_window",
    "unavailable",
)
RESOLUTION_FIELDS = {"name", "role", "source", "status", "skin_id", "skin_version"}


def _observation(case: str) -> UiObservation:
    if case == "unavailable":
        value = UiObservation()
    elif case == "unverified_window":
        value = resolve_ui_observation(
            UiObservation(window=WindowState(id=19123)), kodi_version="21.3"
        )
        return value.model_copy(
            update={
                "window": value.window.model_copy(
                    update={
                        "resolution": WindowResolution(
                            source="skin_mapping",
                            status="unverified",
                            skin_id="skin.test",
                            skin_version="1.0.0",
                        )
                    }
                )
            }
        )
    elif case == "reported_name":
        value = UiObservation(window=WindowState(id=10025, label="Films"))
    elif case == "core_dialog":
        value = UiObservation(
            window=WindowState(id=10000), dialog=DialogState(id=10100, active=True)
        )
    elif case.endswith("dialog"):
        value = UiObservation(
            dialog=DialogState(
                id=98765 if case == "unknown_dialog" else 12998, active=True
            )
        )
    else:
        value = UiObservation(
            window=WindowState(
                id={
                    "core_window": 10000,
                    "unknown_window": 98765,
                    "ambiguous_window": 12998,
                }[case]
            )
        )
    return resolve_ui_observation(value, kodi_version="21.3")


def _inject(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> tuple[UiObservation, list[UUID]]:
    value = _observation(case)
    calls: list[UUID] = []

    def inspect(_self: ManagedUiObservationService, target: UUID) -> UiObservation:
        calls.append(target)
        return value

    monkeypatch.setattr(ManagedUiObservationService, "inspect", inspect)
    return value, calls


def _tool(client: TestClient, token: str) -> dict[str, Any]:
    tools = _post(client, token, "tools/list").json()["result"]["tools"]
    return cast(
        dict[str, Any],
        next(tool for tool in tools if tool["name"] == "inspect_ui_state"),
    )


def _observe(client: TestClient, token: str, target: UUID) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _post(
            client,
            token,
            "tools/call",
            params={
                "name": "inspect_ui_state",
                "arguments": {"target_id": str(target)},
            },
        ).json()["result"],
    )


def _assert_semantics(content: dict[str, Any], case: str) -> None:
    assert content["schema_version"] == 2
    assert set(content["window"]) == {"id", "label", "resolution"}
    assert set(content["dialog"]) == {"id", "label", "active", "resolution"}
    for state in (content["window"], content["dialog"]):
        assert set(state["resolution"]) == RESOLUTION_FIELDS
        context = case == "unverified_window" and state is content["window"]
        assert state["resolution"]["skin_id"] == ("skin.test" if context else None)
        assert state["resolution"]["skin_version"] == ("1.0.0" if context else None)
    active = content["dialog"] if case.endswith("dialog") else content["window"]
    resolution = active["resolution"]
    if case in {"unavailable", "unverified_window"}:
        assert active["id"] == (19123 if case == "unverified_window" else None)
        assert active["label"] is None
        assert resolution["name"] is None and resolution["role"] == "unknown"
        assert resolution["status"] == (
            "unverified" if case == "unverified_window" else "unknown"
        )
        assert content["summary"] == (
            "Unknown window 19123." if case == "unverified_window" else "Kodi."
        )
    elif case.startswith("unknown") or case.startswith("ambiguous"):
        assert active["id"] == (98765 if case.startswith("unknown") else 12998)
        assert active["label"] is None
        assert resolution["name"] is None
        assert resolution["role"] == "unknown"
        assert resolution["status"] == (
            "unknown" if case.startswith("unknown") else "ambiguous"
        )
        prefix = "Unknown" if case.startswith("unknown") else "Ambiguous"
        kind = "dialog" if case.endswith("dialog") else "window"
        suffix = " open." if case.endswith("dialog") else "."
        assert content["summary"] == f"{prefix} {kind} {active['id']}{suffix}"
    elif case == "reported_name":
        assert active["id"] == 10025 and active["label"] == "Films"
        assert resolution["name"] == "Films" and resolution["source"] == "reported"
        assert resolution["status"] == "reported"
        assert content["summary"] == "Films."
    else:
        assert active["id"] == (10100 if case == "core_dialog" else 10000)
        assert active["label"] is None
        assert resolution["name"] == (
            "Yes / No dialog" if case == "core_dialog" else "Home"
        )
        assert resolution["source"] == "core_mapping"
        assert resolution["status"] == "verified"
        assert content["summary"] == (
            "Yes / No dialog open." if case == "core_dialog" else "Home."
        )
        if case == "core_dialog":
            assert content["window"]["id"] == 10000
            assert content["window"]["resolution"]["name"] == "Home"


def _assert_http_schema(
    client: TestClient, mcp_schema: dict[str, Any], content: dict[str, Any]
) -> None:
    specification = cast(FastAPI, client.app).openapi()
    operation = specification["paths"]["/api/instances/{instance_id}/ui-state"]["get"]
    assert [parameter["name"] for parameter in operation["parameters"]] == [
        "instance_id"
    ]
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    components = specification["components"]["schemas"]
    root_name = response["$ref"].rsplit("/", 1)[1]
    advertised = {"UiObservation": mcp_schema, **mcp_schema["$defs"]}

    def without_null_defaults(value: Any) -> Any:
        # FastAPI omits default:null annotations from its OpenAPI serialization;
        # they do not change which response values the shared schemas accept.
        if isinstance(value, dict):
            return {
                key: without_null_defaults(item)
                for key, item in value.items()
                if not (key == "default" and item is None)
            }
        if isinstance(value, list):
            return [without_null_defaults(item) for item in value]
        return value

    for name, schema in advertised.items():
        http_schema = components[root_name if name == "UiObservation" else name]
        normalized = json.loads(
            json.dumps(http_schema).replace("#/components/schemas/", "#/$defs/")
        )
        assert normalized == without_null_defaults(
            {key: value for key, value in schema.items() if key != "$defs"}
        )
    jsonschema.validate(
        content, {"$ref": response["$ref"], "components": specification["components"]}
    )


@pytest.mark.parametrize("case", CASES)
def test_mcp_and_authenticated_http_default_to_the_same_enriched_observation(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    internal, calls = _inject(monkeypatch, case)
    original = internal.model_dump_json()
    url = f"/api/instances/{INSTANCE_ID}/ui-state"
    assert client.get(url).status_code == 401
    assert calls == []
    login(client)
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    _, token = _authority(client)
    tool = _tool(client, token)
    assert tool["inputSchema"]["required"] == ["target_id"]
    assert set(tool["inputSchema"]["properties"]) == {"target_id"}
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["outputSchema"]["properties"]["schema_version"]["const"] == 2
    response = _observe(client, token, INSTANCE_ID)
    assert not response.get("isError")
    content = response["structuredContent"]
    jsonschema.validate(content, tool["outputSchema"])
    assert json.loads(response["content"][0]["text"]) == content
    _assert_semantics(content, case)
    assert UiObservation.model_validate(content) == internal
    http = client.get(url)
    assert http.status_code == 200
    assert http.json() == content
    if case == "core_window":
        _assert_http_schema(client, tool["outputSchema"], http.json())
    assert UiObservation.model_validate(http.json()) == internal
    assert calls == [INSTANCE_ID, INSTANCE_ID]
    assert internal.model_dump_json() == original


def test_default_enrichment_retains_existing_mcp_target_authorization(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    _, calls = _inject(monkeypatch, "core_window")
    _, token = _authority(client)
    denied = _observe(client, token, UNRELATED_INSTANCE_ID)
    assert denied["isError"]
    assert denied["content"][0]["text"] == "target_not_found_or_not_authorized"
    assert "structuredContent" not in denied
    assert calls == []

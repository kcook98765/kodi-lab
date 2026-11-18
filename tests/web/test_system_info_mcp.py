from __future__ import annotations

import json
from typing import Any

import jsonschema  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.ui_state import ManagedUiObservationService
from tests.web.conftest import (
    INSTANCE_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    guard_targeted_status_isolation,
)
from tests.web.test_mcp import _authority, _post


@pytest.mark.parametrize("legacy", [False, True])
def test_managed_native_only_schema_identity_permissions_and_no_hot_path_polling(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    from tests.web.test_mcp import _legacy_post

    post = _legacy_post if legacy else _post
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    calls: list[str] = []

    class Transport:
        def post(
            self, *, host: str, port: int, path: str, body: bytes, timeout: float
        ) -> bytes:
            request = json.loads(body)
            calls.append(request["method"])
            result: object
            if request["method"] == "Application.GetProperties":
                result = {
                    "name": "Kodi",
                    "version": {"major": 21, "minor": 3, "tag": "stable"},
                }
            elif request["method"] == "XBMC.GetInfoLabels":
                result = {
                    "System.BuildVersion": "21.3",
                    "System.Memory(total)": "8192MB",
                    "System.CpuUsage": "#0: 4.2% #1: 2.0%",
                }
            else:
                assert request["method"] == "XBMC.GetInfoBooleans"
                result = {"System.HasNetwork": True}
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()

    original = ManagedUiObservationService.system_info

    def read(self: ManagedUiObservationService, target_id: Any) -> Any:
        self._transport = Transport()
        self._bridges = None
        return original(self, target_id)

    monkeypatch.setattr(ManagedUiObservationService, "system_info", read)
    _, token = _authority(client)
    listed = post(client, token, "tools/list").json()["result"]["tools"]
    tool = next(t for t in listed if t["name"] == "system_info")
    assert tool["annotations"]["readOnlyHint"]
    assert set(tool["inputSchema"]["properties"]) == {"target_id"}
    assert calls == []
    for arguments in [
        {"target_id": str(UNRELATED_INSTANCE_ID)},
        {"target_id": str(INSTANCE_ID), "labels": ["System.ProfileName"]},
    ]:
        result = post(
            client,
            token,
            "tools/call",
            params={"name": "system_info", "arguments": arguments},
        ).json()["result"]
        assert result["isError"] and not calls
    response = post(
        client,
        token,
        "tools/call",
        params={"name": "system_info", "arguments": {"target_id": str(INSTANCE_ID)}},
    ).json()["result"]
    assert not response.get("isError"), response
    value = response["structuredContent"]
    jsonschema.validate(value, tool["outputSchema"])
    assert value["scope"] == "container_view"
    assert value["metrics"]["cpu_usage"]["value"] == {"0": 4.2, "1": 2.0}
    assert calls == [
        "Application.GetProperties",
        "XBMC.GetInfoLabels",
        "XBMC.GetInfoBooleans",
    ]

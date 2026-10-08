from __future__ import annotations

import json
from typing import Any, cast

import jsonschema  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.broker.docker_inventory import ContainerHealthState, KodiProcessState
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.kodi.ui_state import UiCollection, UiObservation
from kodi_lab.kodi.ui_state_native import UiObservationError
from tests.kodi.test_ui_state import movie_collection, native_fixture
from tests.web.conftest import (
    INSTANCE_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    guard_targeted_status_isolation,
)
from tests.web.test_mcp import _authority, _post


class FixtureBridge:
    def __init__(self, *, bridge: bool = True, labels: bool = True) -> None:
        self.bridge, self.labels = bridge, labels

    def collect(self, *args: Any) -> UiCollection | None:
        return (
            movie_collection().model_copy(update={"labels_available": self.labels})
            if self.bridge
            else None
        )


class FixtureTransport:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        request = json.loads(body)
        method, params = request["method"], request.get("params")
        self.methods.append(method)
        if method == "Application.GetProperties":
            result: Any = {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
        else:
            try:
                result = native_fixture(method, params)
            except UiObservationError:
                return json.dumps(
                    {"id": request["id"], "error": {"code": -32602}}
                ).encode()
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()


@pytest.mark.parametrize("bridge,labels", [(True, True), (False, True), (True, False)])
def test_actual_managed_routing_schema_authorization_and_fallback(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    bridge: bool,
    labels: bool,
    database: sessionmaker[Session],
) -> None:
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    transport = FixtureTransport()
    original = ManagedUiObservationService.inspect

    def inspect(self: ManagedUiObservationService, target_id: Any) -> UiObservation:
        self._transport = transport
        self._bridges = FixtureBridge(bridge=bridge, labels=labels)  # type: ignore[assignment]
        return original(self, target_id)

    monkeypatch.setattr(ManagedUiObservationService, "inspect", inspect)
    _, token = _authority(client)
    tools = _post(client, token, "tools/list").json()["result"]["tools"]
    tool = next(item for item in tools if item["name"] == "inspect_ui_state")
    assert tool["annotations"]["readOnlyHint"]
    assert tool["inputSchema"]["required"] == ["target_id"]
    response = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "inspect_ui_state",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
    ).json()["result"]
    assert not response.get("isError")
    value = response["structuredContent"]
    jsonschema.validate(value, tool["outputSchema"])
    assert "Addons.ExecuteAddon" not in transport.methods
    assert value["window"]["id"] == 10025
    assert value["selected_item"]["associated"] is (bridge and labels)
    if bridge and not labels:
        assert value["focus"]["control_id"] == 50
        assert value["focus"]["is_container"] is True
        assert value["focus"]["label"] is None
        assert value["selected_item"]["label"] is None
        assert "GUI.GetProperties" not in transport.methods
    methods = list(transport.methods)
    denied = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "inspect_ui_state",
            "arguments": {"target_id": str(UNRELATED_INSTANCE_ID)},
        },
    ).json()["result"]
    assert denied["isError"] and transport.methods == methods


def _inject_transport(monkeypatch: pytest.MonkeyPatch) -> FixtureTransport:
    transport = FixtureTransport()
    original = ManagedUiObservationService.inspect

    def inspect(self: ManagedUiObservationService, target_id: Any) -> UiObservation:
        self._transport = transport
        self._bridges = FixtureBridge()  # type: ignore[assignment]
        return original(self, target_id)

    monkeypatch.setattr(ManagedUiObservationService, "inspect", inspect)
    return transport


def _observe(client: TestClient, token: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        _post(
            client,
            token,
            "tools/call",
            params={
                "name": "inspect_ui_state",
                "arguments": {"target_id": str(INSTANCE_ID)},
            },
        ).json()["result"],
    )


def test_fresh_admission_inputs_reused_only_within_each_observation(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    database: sessionmaker[Session],
) -> None:
    observed = guard_targeted_status_isolation(database, broker, client, monkeypatch)
    inventory = broker.managed_container_inventory
    inventories: list[object] = []

    def fresh_inventory(**kwargs: Any) -> ManagedContainerInventoryResult:
        result = inventory(**kwargs)
        inventories.append(result)
        return result

    monkeypatch.setattr(broker, "managed_container_inventory", fresh_inventory)
    transport = _inject_transport(monkeypatch)
    _, token = _authority(client)
    for count in (1, 2):
        result = _observe(client, token)
        assert not result.get("isError")
        assert result["structuredContent"]["selected_item"]["associated"]
        # One fresh pre-collection inventory/runtime and a separate post-check.
        assert len(inventories) == count
        assert len(observed) == count * 2
        assert transport.methods.count("Application.GetProperties") == count
    assert inventories[0] is not inventories[1]


@pytest.mark.parametrize("fault", ["unhealthy", "process_stopped", "ambiguous"])
def test_fresh_runtime_admission_still_refuses_unsafe_targets_before_collection(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    database: sessionmaker[Session],
    fault: str,
) -> None:
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    transport = _inject_transport(monkeypatch)
    _, token = _authority(client)
    assert broker.container is not None
    if fault == "unhealthy":
        broker.container = broker.container.model_copy(
            update={"health_state": ContainerHealthState.UNHEALTHY}
        )
    elif fault == "process_stopped":
        broker.process_state = KodiProcessState.NOT_RUNNING
    else:
        container = broker.container
        monkeypatch.setattr(
            broker,
            "managed_container_inventory",
            lambda **kwargs: ManagedContainerInventoryResult(
                containers=[container, container]
            ),
        )
    assert _observe(client, token)["isError"]
    assert transport.methods == []


@pytest.mark.parametrize(
    "change",
    [
        {"ipv4_address": "10.250.0.99"},
        {"jsonrpc_port": 8081},
        {"container_id": "b" * 64},
        {"runtime_revision_id": "00000000-0000-4000-8000-000000000099"},
        {"process_state": KodiProcessState.NOT_RUNNING},
    ],
)
def test_final_identity_recheck_remains_fresh_and_gate_releases_after_failure(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    database: sessionmaker[Session],
    change: dict[str, object],
) -> None:
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    _inject_transport(monkeypatch)
    _, token = _authority(client)
    original = broker.observe_managed_kodi_runtime
    calls = 0

    def observe(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(**kwargs)
        return result.model_copy(update=change) if calls == 2 else result

    monkeypatch.setattr(broker, "observe_managed_kodi_runtime", observe)
    failed = _observe(client, token)
    assert failed["isError"] and "identity_mismatch" in json.dumps(failed)
    assert not _observe(client, token).get("isError")
    assert calls == 4

"""Authenticated destructive Reset uses only strict version-oriented inputs."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.managed_versions_support import Broker
from tests.web.test_managed_versions_mcp import authority, call
from tests.web.test_managed_versions_mcp import broker as broker
from tests.web.test_mcp import _assert_tool_error, _post, _strict_tool_result


def test_reset_is_opted_in_strict_replayable_and_stopped_by_default(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    token, _, _ = authority(
        client, database, permissions=frozenset({"managed.versions", "managed.reset"})
    )
    tools = {
        t["name"]: t
        for t in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert set(tools) == {
        "managed_status",
        "managed_start",
        "managed_stop",
        "managed_reset",
    }
    assert tools["managed_reset"]["annotations"]["destructiveHint"] is True
    schema = tools["managed_reset"]["inputSchema"]
    assert set(schema["properties"]) == {"version", "request_id", "restart"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["restart"]["default"] is False
    request = str(uuid4())
    first = call(
        client, token, "managed_reset", {"version": "omega", "request_id": request}
    )
    _strict_tool_result(first, tools["managed_reset"]["outputSchema"])
    result = first["structuredContent"]
    assert result["phase"] == "complete" and result["replacement"] == "confirmed", first
    assert result["retirement"] == "not_required" and result["start"] == "not_requested"
    assert (
        result["status"]["state"] == "stopped" and not result["status"]["native_ready"]
    )
    record = broker.records[0]
    profile = broker.root / str(record.managed_instance_id) / "settings.xml"
    profile.write_text("private old Kodi settings\n")
    request = str(uuid4())
    replaced = call(
        client, token, "managed_reset", {"version": "omega", "request_id": request}
    )
    assert replaced["structuredContent"]["retirement"] == "confirmed", replaced
    replay = call(
        client, token, "managed_reset", {"version": "omega", "request_id": request}
    )
    _strict_tool_result(replay, tools["managed_reset"]["outputSchema"])
    assert replay["structuredContent"]["replayed"]
    assert (
        replay["structuredContent"]["operation_id"]
        == replaced["structuredContent"]["operation_id"]
    )
    assert broker.records[0].container_id != record.container_id
    assert broker.removals == 1 and broker.provisions == 2 and broker.starts == 0
    assert profile.read_text() == "clean fixture Kodi profile\n"


@pytest.mark.parametrize("permissions", [{"managed.versions"}, {"managed.reset"}])
def test_reset_requires_both_explicit_permissions(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    permissions: set[str],
) -> None:
    token, _, _ = authority(client, database, permissions=frozenset(permissions))
    names = {
        t["name"] for t in _post(client, token, "tools/list").json()["result"]["tools"]
    }
    assert "managed_reset" not in names
    _assert_tool_error(
        call(
            client,
            token,
            "managed_reset",
            {"version": "omega", "request_id": str(uuid4())},
        ),
        "permission_denied",
    )
    assert broker.removals == broker.provisions == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"target_id": str(uuid4())},
        {"container_id": "a" * 64},
        {"path": "/tmp/profile"},
        {"restart": "false"},
    ],
)
def test_reset_rejects_raw_identities_paths_and_nonboolean_restart(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    extra: dict[str, str],
) -> None:
    token, _, _ = authority(
        client, database, permissions=frozenset({"managed.versions", "managed.reset"})
    )
    _assert_tool_error(
        call(
            client,
            token,
            "managed_reset",
            {"version": "omega", "request_id": str(uuid4()), **extra},
        ),
        "invalid_params",
    )
    assert broker.removals == broker.provisions == 0

"""Stopped diagnostic metadata is authenticated, target-bound and observation-only."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.stopped_diagnostics import StoppedRuntimeDiagnostics
from tests.web.conftest import (
    IMAGE_ID,
    INSTANCE_ID,
    REVISION_ID,
    FakeBroker,
    inventory_entry,
    login,
)


def test_stopped_exit_metadata_route_requires_login_and_never_wakes_kodi(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = f"/api/instances/{INSTANCE_ID}/stopped-diagnostics"
    assert client.get(path).status_code == 401
    login(client)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    expected = broker.container

    def inspect(**kwargs: Any) -> StoppedRuntimeDiagnostics:
        assert kwargs == {
            "managed_instance_id": str(INSTANCE_ID),
            "expected_runtime_revision_id": str(REVISION_ID),
            "expected_container_id": expected.container_id,
        }
        return StoppedRuntimeDiagnostics(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected.container_id,
            image_id=IMAGE_ID,
            container_status="exited",
            exit_code=139,
            oom_killed=False,
            started_at=None,
            finished_at=None,
            observed_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        broker, "read_stopped_managed_diagnostics", inspect, raising=False
    )
    response = client.get(path)
    assert response.status_code == 200 and response.json()["exit_code"] == 139
    assert response.json()["cause"] == "unknown"
    assert broker.container == expected
    assert not any(
        call[0] in {"start", "stop", "provision", "deprovision"}
        for call in broker.calls
    )
    assert path in client.get(f"/instances/{INSTANCE_ID}").text
    broker.container = None
    assert client.get(path).status_code == 409

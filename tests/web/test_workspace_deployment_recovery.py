"""Deployment recovery stays on the authenticated Web administrator plane."""

import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.workspace_deployment_recovery import (
    WorkspaceDeploymentRecoveryResult,
    WorkspaceDeploymentRecoveryService,
)
from tests.web.conftest import INSTANCE_ID, ORIGIN, login


@pytest.mark.parametrize("eligible", [False, True])
def test_history_offers_explicit_recovery_only_for_projected_candidate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, eligible: bool
) -> None:
    from datetime import UTC, datetime

    from kodi_lab.application.activity import (
        ManagedInstanceActivityItem,
        ManagedInstanceActivityKind,
        ManagedInstanceActivityService,
    )

    operation_id = uuid4()
    item = ManagedInstanceActivityItem(
        kind=ManagedInstanceActivityKind.MCP_WORKSPACE_DEPLOYMENT,
        operation_uuid=operation_id,
        original_managed_instance_id=INSTANCE_ID,
        requested_at=datetime.now(UTC),
        started_at=None,
        completed_at=None,
        state="failed",
        summary="Retained inventory failure",
        reconciliation_required=False,
        deployment_recovery_available=eligible,
    )
    monkeypatch.setattr(
        ManagedInstanceActivityService,
        "list_for_live_instance",
        lambda _self, _target, **_kwargs: [item],
    )
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}/history")
    assert page.status_code == 200
    assert (f'data-deployment-id="{operation_id}"' in page.text) is eligible
    assert ("Inspect stopped deployment" in page.text) is eligible
    assert "managed-launch-reconcile.js" in page.text


def test_recovery_requires_session_origin_csrf_and_an_empty_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = uuid4()
    calls: list[tuple[UUID, UUID]] = []

    def inspect(
        self: Any, target: UUID, operation_id: UUID
    ) -> WorkspaceDeploymentRecoveryResult:
        calls.append((target, operation_id))
        return WorkspaceDeploymentRecoveryResult(
            deployment_id=operation_id,
            request_id=uuid4(),
            workspace_id=uuid4(),
            target_id=target,
            generation=12,
            outcome="still_requires_admin",
            reason="stop_target_then_recheck",
            next_action="Stop then inspect; never replay deployment",
            failed_phase="sync",
            failure_cause="sync_unconfirmed",
        )

    monkeypatch.setattr(WorkspaceDeploymentRecoveryService, "reconcile", inspect)
    url = f"/api/instances/{INSTANCE_ID}/deployments/{operation}/reconcile"
    assert client.post(url, json={}, headers=ORIGIN).status_code == 401
    csrf = login(client)
    assert client.post(url, json={}, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            url,
            json={},
            headers={"Origin": "http://untrusted.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(url, json={"reset": True}, headers=headers).status_code == 422
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["deployment_succeeded"] is False
    assert calls == [(INSTANCE_ID, operation)]


def test_stopped_status_distinguishes_historical_usable_readiness(
    client: TestClient, broker: Any
) -> None:
    from kodi_lab.broker.docker_inventory import ContainerRuntimeState
    from tests.web.conftest import inventory_entry
    from tests.web.test_mcp import _make_target_ready

    _make_target_ready(client)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    response = client.get(f"/api/instances/{INSTANCE_ID}")
    assert response.status_code == 200
    status = response.json()
    assert status["readiness_state"] == "idle"
    assert status["stored_readiness_state"] == "usable"
    assert status["usability_state"] == "not_usable"


def test_browser_recovery_is_explicit_and_suppresses_duplicate_requests() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("workspace_deployment_recovery_browser.js")),
            str(root / "src/kodi_lab/web/static/managed-launch-reconcile.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

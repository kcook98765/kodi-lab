"""Contextual controls preserve observed state and separate destructive review."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.activity import (
    ManagedInstanceActivityItem,
    ManagedInstanceActivityKind,
)
from kodi_lab.application.instance_history import ManagedInstanceDetailService
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login


def controls(page: str) -> str:
    return page.split("data-instance-controls=", 1)[1].split("</section>", 1)[0]


def test_instance_controls_follow_existing_lifecycle(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert 'data-instance-action="provision">Prepare runtime' in controls(page)
    assert 'data-instance-action="start" hidden disabled' in controls(page)
    assert "data-deprovision-approved" in controls(page)
    assert f'href="/instances/{INSTANCE_ID}/reset"' in controls(page)
    assert f'href="/instances/{INSTANCE_ID}/delete"' in controls(page)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/provision", headers=headers, json={}
        ).status_code
        == 200
    )
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert 'data-instance-action="start">Start Kodi' in controls(page)
    assert 'data-instance-action="provision" hidden disabled' in controls(page)
    assert 'data-instance-action="deprovision">Remove runtime' in controls(page)
    assert "data-instance-destructive hidden" not in controls(page)
    assert not any(action in {"start", "deprovision"} for action, _ in broker.calls)


@pytest.mark.parametrize("busy", [False, True])
def test_unknown_and_busy_controls_fail_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, busy: bool
) -> None:
    login(client)
    service: ManagedInstanceDetailService = cast(
        FastAPI, client.app
    ).state.kodi_lab.instance_detail
    original = service.get(INSTANCE_ID)
    if busy:
        changed = original.model_copy(update={"mutation_active": True})
    else:
        runtime = original.runtime.model_copy(
            update={"allowed_actions": [], "container_state": "unknown"}
        )
        changed = original.model_copy(update={"runtime": runtime})
    monkeypatch.setattr(service, "get", lambda _id: changed)
    panel = controls(client.get(f"/instances/{INSTANCE_ID}").text)
    for action in ["provision", "start", "stop", "deprovision"]:
        assert f'data-instance-action="{action}" hidden disabled' in panel
    assert "data-instance-destructive hidden" in panel


def test_monitoring_keeps_screenshot_controls_and_separate_ui_inspection(
    client: TestClient,
) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    monitoring = re.search(r'<section id="visual-access".*?</section>', page, re.S)
    assert monitoring is not None
    health = re.search(
        r'<section aria-labelledby="instance-health-heading".*?</section>', page, re.S
    )
    assert health is not None
    for label in ["Kodi process", "JSON-RPC", "Readiness"]:
        assert label in health.group()
    assert "Alternative screen tools" in monitoring.group()
    assert "Saved screenshot" in monitoring.group()
    assert (
        '<details class="instance-maintenance"><summary>Alternative viewers, '
        "screenshots and browser remote</summary>" in page
    )
    for hook in [
        "data-ui-state-inspect",
        "data-preview-pause",
        "data-preview-interval",
        "data-screenshot-refresh",
        "data-screenshot-status",
    ]:
        assert hook in monitoring.group()
    assert "UI state has not been inspected" in monitoring.group()
    assert "Refresh pauses while this tab is hidden" in monitoring.group()


def test_instance_controls_browser_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("instance_controls_browser.js")),
            str(root / "src/kodi_lab/web/static/instance-controls.js"),
        ],
        check=True,
    )


def test_monitoring_inspection_browser_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("managed_monitoring_browser.js")),
            str(root / "src/kodi_lab/web/static/managed-screenshot.js"),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "failure",
        "code",
        "reconciliation",
        "busy",
        "activity-recovery",
        "activity-reconciliation",
    ],
)
def test_maintenance_keeps_failure_and_reconciliation_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    login(client)
    service = cast(FastAPI, client.app).state.kodi_lab.instance_detail
    original = service.get(INSTANCE_ID)
    if scenario in {"failure", "code"}:
        original = original.model_copy(
            update={
                "runtime": original.runtime.model_copy(
                    update={
                        "failure_detail": "Runtime needs review"
                        if scenario == "failure"
                        else None,
                        "failure_code": "runtime_revision_mismatch",
                    }
                )
            }
        )
    if scenario == "reconciliation":
        original = original.model_copy(
            update={
                "reconciliation": original.reconciliation.model_copy(
                    update={
                        "required": True,
                        "blocking": True,
                        "safe_reason": "Review the original operation",
                    }
                )
            }
        )
    if scenario == "busy":
        original = original.model_copy(update={"mutation_active": True})
    if scenario.startswith("activity-"):
        item = ManagedInstanceActivityItem(
            kind=ManagedInstanceActivityKind.MCP_WORKSPACE_DEPLOYMENT,
            operation_uuid=uuid4(),
            original_managed_instance_id=INSTANCE_ID,
            requested_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            state="failed",
            summary="Review retained operation",
            reconciliation_required=scenario == "activity-reconciliation",
            deployment_recovery_available=scenario == "activity-recovery",
        )
        original = original.model_copy(update={"recent_activity": [item]})
    monkeypatch.setattr(service, "get", lambda _id: original)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    before_maintenance = page.split('<details class="instance-maintenance"', 1)[0]
    if scenario == "failure":
        assert "Runtime needs review" in before_maintenance
    if scenario == "code":
        assert (
            '<p class="error" role="alert">runtime_revision_mismatch</p>'
            in before_maintenance
        )
    if scenario == "reconciliation":
        assert "Operator reconciliation required" in before_maintenance
        assert (
            '<details class="instance-maintenance" open>'
            "<summary>Activity and recovery history" in page
        )
    if scenario.startswith("activity-"):
        assert (
            '<details class="instance-maintenance" open>'
            "<summary>Activity and recovery history" in page
        )
    if scenario in {"failure", "code", "reconciliation", "busy"}:
        assert (
            '<details class="instance-maintenance" open><summary>Advanced lifecycle'
            in page
        )
    else:
        assert (
            '<details class="instance-maintenance"><summary>Advanced lifecycle' in page
        )
    for label in [
        "Notes and snapshots",
        "Add-ons and ZIP installation records",
        "Source attachments and copy records",
        "Activity and recovery history",
    ]:
        assert f"<summary>{label}</summary>" in page
    if destination := os.getenv("KODI_LAB_CURRENT_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"instance-{scenario}.html").write_text(page)

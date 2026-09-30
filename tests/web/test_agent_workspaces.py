from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.workspace_source_captures import WorkspaceSourceCaptureService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

from .conftest import (
    CONTAINER_ID,
    INSTANCE_ID,
    ORIGIN,
    REVISION_ID,
    FakeBroker,
    inventory_entry,
    login,
)

SOURCE_ID = UUID("71000000-0000-0000-0000-000000000001")
DEPLOYMENT_ID = UUID("71000000-0000-0000-0000-000000000002")


def _seed_source(database: sessionmaker[Session], data_root: Path) -> None:
    root = data_root.parent / "development-root" / "plugin.video.workspace"
    (root / "resources").mkdir(parents=True)
    (root / "addon.xml").write_text(
        '<addon id="plugin.video.workspace" name="Workspace" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (root / "resources" / "main.py").write_text("print('web')\n", encoding="utf-8")
    now = datetime(2026, 9, 18, tzinfo=UTC)
    with database.begin() as session:
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=SOURCE_ID,
                logical_name="Workspace source",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="plugin.video.workspace",
                addon_id="plugin.video.workspace",
                observed_name="Workspace",
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=now,
                validated_at=now,
            )
        )


def _seed_promotion_target(database: sessionmaker[Session]) -> None:
    now = datetime(2026, 9, 18, tzinfo=UTC).isoformat()
    with database.begin() as session:
        session.query(ManagedInstanceRow).filter_by(target_id=str(INSTANCE_ID)).update(
            {"runtime_state": "running"}
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                attached_at=now,
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(DEPLOYMENT_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Workspace source",
                addon_id="plugin.video.workspace",
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=now,
                state="succeeded",
                phase="finalize",
                result_classification="unchanged",
                completed_at=now,
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                addon_id="plugin.video.workspace",
                mode="copy",
                last_successful_operation_id=str(DEPLOYMENT_ID),
                source_manifest_digest="sha256:" + "1" * 64,
                verified_destination_digest="sha256:" + "2" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Workspace",
                observed_source_version="1.0.0",
                verified_at=now,
                confidence="verified_at_last_operation",
            )
        )


def test_workspace_api_is_authenticated_strict_and_safe(
    client: TestClient, database: sessionmaker[Session], data_root: Path
) -> None:
    _seed_source(database, data_root)
    assert client.get("/api/agent-workspaces").status_code == 401
    csrf = login(client)
    assert client.post("/api/agent-workspaces", json={}).status_code == 403
    page = client.get("/agent-workspaces")
    assert page.status_code == 200
    assert "Workspace source" in page.text
    assert str(data_root) not in page.text
    assert (
        client.post(
            "/api/agent-workspaces",
            json={"development_source_id": str(SOURCE_ID), "workspace_path": "/tmp"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )

    accepted = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(SOURCE_ID)},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert accepted.status_code == 202
    body = accepted.json()
    operation_url = body["status_url"]
    workspace_url = body["workspace_url"]
    for _ in range(100):
        operation = client.get(operation_url).json()
        if operation["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert operation["state"] == "succeeded"

    workspace = client.get(workspace_url)
    assert workspace.status_code == 200
    serialized = workspace.text
    assert "/" not in workspace.json()["source_logical_name"]
    assert "baseline" not in serialized
    assert "digest" not in serialized
    assert "data_root" not in serialized

    workspace_id = body["operation"]["workspace_id"]
    tree = client.get(f"/api/agent-workspaces/{workspace_id}/tree")
    assert tree.status_code == 200
    assert [item["name"] for item in tree.json()] == ["addon.xml", "resources"]
    content = client.get(f"/api/agent-workspaces/{workspace_id}/file/resources/main.py")
    assert content.status_code == 200
    assert content.json() == {
        "path": "resources/main.py",
        "encoding": "utf-8",
        "content": "print('web')\n",
    }
    detail = client.get(f"/agent-workspaces/{workspace_id}")
    assert detail.status_code == 200
    assert "plugin.video.workspace" in detail.text
    assert str(data_root) not in detail.text

    abandon_url = f"/api/agent-workspaces/{workspace_id}/abandon"
    assert (
        client.post(
            abandon_url,
            json={"delete": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )
    abandoned = client.post(
        abandon_url,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["state"] == "abandoned"
    assert abandoned.json()["bytes_state"] == "present"
    assert (
        client.post(
            f"/api/agent-workspaces/{workspace_id}/cleanup",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )
    accepted_cleanup = client.post(
        f"/api/agent-workspaces/{workspace_id}/cleanup",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert accepted_cleanup.status_code == 202
    cleanup_url = accepted_cleanup.json()["status_url"]
    for _ in range(100):
        cleanup_status = client.get(cleanup_url).json()
        if cleanup_status["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert cleanup_status["state"] == "succeeded"
    retained = client.get(workspace_url)
    assert retained.json()["state"] == "abandoned"
    assert retained.json()["bytes_state"] == "cleaned"
    assert retained.json()["cleaned_at"] is not None
    blocked = client.get(f"/api/agent-workspaces/{workspace_id}/tree")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "workspace_bytes_cleaned"
    lifecycle = client.get(f"/api/agent-workspaces/{workspace_id}/lifecycle-activity")
    assert [item["operation"] for item in lifecycle.json()] == [
        "abandoned",
        "cleanup_queued",
        "cleanup_succeeded",
    ]
    assert str(data_root) not in lifecycle.text


def test_workspace_routes_do_not_expose_mutation_verbs(client: TestClient) -> None:
    csrf = login(client)
    workspace_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for suffix in ("promote", "delete", "rename", "mkdir", "git", "shell"):
        response = client.post(
            f"/api/agent-workspaces/{workspace_id}/{suffix}", json={}, headers=headers
        )
        assert response.status_code in {404, 405}


def test_source_scan_then_explicit_whole_capture_is_csrf_bound(
    client: TestClient, database: sessionmaker[Session], data_root: Path
) -> None:
    _seed_source(database, data_root)
    workspace_id = _create_workspace_for_capture(client)
    source = data_root.parent / "development-root" / "plugin.video.workspace"
    base = f"/api/agent-workspaces/{workspace_id}"
    client.cookies.clear()
    assert client.get(f"{base}/source-scan").status_code == 401
    csrf = login(client)
    first = client.get(f"{base}/source-scan")
    assert first.status_code == 200
    assert first.json()["source_state"] == "matches_active_capture"
    assert first.json()["source_scan"]["addon_version"] == "1.0.0"
    assert client.get(base).json()["generation"] == 0
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.workspace" name="Workspace" version="1.0.1">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "resources/main.py").write_text("print('v2')\n", encoding="utf-8")
    changed = client.get(f"{base}/source-scan")
    assert changed.status_code == 200
    assert changed.json()["source_state"] == "differs_from_active_capture"
    assert changed.json()["source_scan"]["addon_version"] == "1.0.1"
    assert client.get(base).json()["generation"] == 0
    assert (
        client.post(
            f"{base}/source-capture", json={"expected_generation": 0}
        ).status_code
        == 403
    )
    captured = client.post(
        f"{base}/source-capture",
        json={"expected_generation": 0},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert captured.status_code == 200
    assert captured.json()["changed"] is True
    assert captured.json()["generation"] == 1
    assert (
        captured.json()["tree_digest"] == changed.json()["source_scan"]["tree_digest"]
    )
    assert client.get(base).json()["generation"] == 1
    unchanged = client.post(
        f"{base}/source-capture",
        json={"expected_generation": 1},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["changed"] is False
    assert unchanged.json()["generation"] == 1
    assert (
        client.get(f"{base}/capture-status").json()["source_state"]
        == "not_freshly_scanned"
    )
    assert client.get(f"/agent-workspaces/{workspace_id}").status_code == 200


def _create_workspace_for_capture(client: TestClient) -> str:
    csrf = login(client)
    accepted = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(SOURCE_ID)},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    ).json()
    for _ in range(100):
        status = client.get(accepted["status_url"]).json()
        if status["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert status["state"] == "succeeded"
    return cast(str, accepted["operation"]["workspace_id"])


def test_source_capture_rejects_private_edits_identity_drift_and_unsafe_source(
    client: TestClient, database: sessionmaker[Session], data_root: Path
) -> None:
    _seed_source(database, data_root)
    workspace_id = _create_workspace_for_capture(client)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    base = f"/api/agent-workspaces/{workspace_id}"
    source = data_root.parent / "development-root" / "plugin.video.workspace"
    (source / "resources/main.py").write_text("changed\n", encoding="utf-8")
    assert (
        client.post(
            f"{base}/source-capture", json={"expected_generation": 99}, headers=headers
        ).status_code
        == 409
    )
    edited = client.put(
        f"{base}/files",
        json={
            "expected_generation": 0,
            "path": "resources/main.py",
            "content": "private\n",
        },
        headers=headers,
    )
    assert edited.status_code == 200
    denied = client.post(
        f"{base}/source-capture", json={"expected_generation": 1}, headers=headers
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "workspace_private_edits_present"
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.other" name="Other" version="1.0.1">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    drift = client.get(f"{base}/source-scan")
    assert drift.status_code == 409
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.workspace" name="Workspace" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "unsafe.py").symlink_to("/etc/passwd")
    assert client.get(f"{base}/source-scan").status_code == 422


def test_interrupted_capture_is_visible_and_recoverable_through_web(
    client: TestClient, database: sessionmaker[Session], data_root: Path
) -> None:
    _seed_source(database, data_root)
    workspace_id = _create_workspace_for_capture(client)
    source = data_root.parent / "development-root" / "plugin.video.workspace"
    (source / "resources/main.py").write_text("new source\n", encoding="utf-8")
    captures = WorkspaceSourceCaptureService(
        database,
        data_root=data_root,
        development_root=data_root.parent / "development-root",
    )

    def interrupt(stage: str) -> None:
        if stage == "pending":
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        captures.capture(
            UUID(workspace_id), expected_generation=0, checkpoint=interrupt
        )
    with database() as session:
        operation = session.scalar(select(AgentWorkspaceCaptureOperationRow))
        assert operation is not None
        operation_id = operation.id
    status = client.get(f"/api/agent-workspaces/{workspace_id}/capture-status").json()
    assert status["recovery_state"] == "staging"
    assert status["capture_available"] is False
    assert (
        "Reconcile interrupted capture"
        in client.get(f"/agent-workspaces/{workspace_id}").text
    )
    csrf = login(client)
    assert (
        client.post(
            f"/api/agent-workspace-capture-operations/{operation_id}/recover", json={}
        ).status_code
        == 403
    )
    recovered = client.post(
        f"/api/agent-workspace-capture-operations/{operation_id}/recover",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert recovered.status_code == 200
    assert (
        client.get(f"/api/agent-workspaces/{workspace_id}/capture-status").json()[
            "recovery_state"
        ]
        is None
    )
    assert client.get(f"/api/agent-workspaces/{workspace_id}").json()["generation"] == 0


def test_workspace_mutation_and_changes_api_is_strict_private_and_generation_bound(
    client: TestClient, database: sessionmaker[Session], data_root: Path
) -> None:
    _seed_source(database, data_root)
    csrf = login(client)
    accepted = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(SOURCE_ID)},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    ).json()
    status_url = accepted["status_url"]
    for _ in range(100):
        operation = client.get(status_url).json()
        if operation["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert operation["state"] == "succeeded"
    workspace_id = accepted["operation"]["workspace_id"]
    files_url = f"/api/agent-workspaces/{workspace_id}/files"
    patch_url = f"/api/agent-workspaces/{workspace_id}/patches"
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}

    assert (
        client.post(
            files_url,
            json={
                "expected_generation": 0,
                "path": "resources/new.txt",
                "content": "x",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            files_url,
            json={
                "expected_generation": 0,
                "path": "resources/new.txt",
                "content": "x",
            },
            headers={"X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            files_url,
            json={
                "expected_generation": 0,
                "path": "resources/new.txt",
                "content": "x",
                "host_path": "/tmp/escape",
            },
            headers=headers,
        ).status_code
        == 422
    )

    created = client.post(
        files_url,
        json={
            "expected_generation": 0,
            "path": "resources/new.txt",
            "content": "new \N{GREEK SMALL LETTER ALPHA}\n",
        },
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json() == {
        "workspace_id": workspace_id,
        "path": "resources/new.txt",
        "operation": "created",
        "generation": 1,
    }
    stale = client.put(
        files_url,
        json={
            "expected_generation": 0,
            "path": "resources/main.py",
            "content": "stale",
        },
        headers=headers,
    )
    assert stale.status_code == 409

    patched = client.post(
        patch_url,
        json={
            "expected_generation": 1,
            "path": "resources/main.py",
            "expected_text": "print('web')\n",
            "replacement_text": "print('patched')\n",
        },
        headers=headers,
    )
    assert patched.status_code == 200
    mismatch = client.post(
        patch_url,
        json={
            "expected_generation": 2,
            "path": "resources/main.py",
            "expected_text": "private preimage",
            "replacement_text": "private replacement",
        },
        headers=headers,
    )
    assert mismatch.status_code == 422
    assert "private preimage" not in mismatch.text
    assert "private replacement" not in mismatch.text

    changes = client.get(f"/api/agent-workspaces/{workspace_id}/changes")
    assert changes.status_code == 200
    assert changes.json()["generation"] == 2
    assert changes.json()["identity_status"] == "stable"
    assert [item["path"] for item in changes.json()["changes"]] == [
        "resources/main.py",
        "resources/new.txt",
    ]
    serialized = changes.text
    assert str(data_root) not in serialized
    assert "development-root" not in serialized
    assert "digest" not in serialized
    activity = client.get(f"/api/agent-workspaces/{workspace_id}/activity")
    assert activity.status_code == 200
    assert [item["operation"] for item in activity.json()] == ["patched", "created"]
    assert "content" not in activity.text

    oversized = client.post(
        files_url,
        json={
            "expected_generation": 2,
            "path": "resources/too-large.txt",
            "content": "x" * (2 * 1024 * 1024 + 1),
        },
        headers=headers,
    )
    assert oversized.status_code == 422
    request_oversized = client.post(
        files_url,
        content=b"{}",
        headers={
            **headers,
            "Content-Type": "application/json",
            "Content-Length": str(16 * 1024 * 1024 + 1),
        },
    )
    assert request_oversized.status_code == 413


def test_promotion_review_and_approval_are_strict_complete_and_private(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    broker: FakeBroker,
) -> None:
    _seed_source(database, data_root)
    _seed_promotion_target(database)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.workspace",
                name="Workspace",
                version="1.0.0",
                enabled=True,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    assert (
        client.get(
            "/api/agent-workspaces/ffffffff-ffff-ffff-ffff-ffffffffffff/"
            "promotion-targets"
        ).status_code
        == 401
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    created = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(SOURCE_ID)},
        headers=headers,
    ).json()
    for _ in range(100):
        creation = client.get(created["status_url"]).json()
        if creation["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert creation["state"] == "succeeded"
    workspace_id = created["operation"]["workspace_id"]
    changed = client.put(
        f"/api/agent-workspaces/{workspace_id}/files",
        json={
            "expected_generation": 0,
            "path": "resources/main.py",
            "content": "print('promoted')\n",
        },
        headers=headers,
    )
    assert changed.status_code == 200
    targets = client.get(f"/api/agent-workspaces/{workspace_id}/promotion-targets")
    assert targets.status_code == 200
    assert targets.json() == [
        {"id": str(INSTANCE_ID), "name": "Kodi <script>not markup</script>"}
    ]

    plans_url = (
        f"/api/instances/{INSTANCE_ID}/agent-workspaces/{workspace_id}/promotion-plans"
    )
    assert client.post(plans_url, json={}).status_code == 403
    assert (
        client.post(
            plans_url,
            json={"source_id": str(SOURCE_ID)},
            headers=headers,
        ).status_code
        == 422
    )
    planned = client.post(plans_url, json={}, headers=headers)
    assert planned.status_code == 201
    review = planned.json()
    assert review["state"] == "reviewable"
    assert review["source_logical_name"] == "Workspace source"
    assert review["verification_target_id"] == str(INSTANCE_ID)
    assert review["addon_id"] == "plugin.video.workspace"
    assert review["changes"] == [
        {
            "path": "resources/main.py",
            "change_kind": "modify",
            "before_text": "print('web')\n",
            "after_text": "print('promoted')\n",
            "before_bytes": 13,
            "after_bytes": 18,
            "conflict_reason": None,
        }
    ]
    serialized = planned.text
    for forbidden in (
        str(data_root),
        "development-root",
        "container_id",
        "sha256",
        "source_capture",
        "baseline",
        "preimage",
    ):
        assert forbidden not in serialized
    plan_id = review["plan_id"]
    page = client.get(
        f"/instances/{INSTANCE_ID}/agent-workspaces/{workspace_id}/"
        f"promotion-plans/{plan_id}"
    )
    assert page.status_code == 200
    assert "print(&#39;web&#39;)" in page.text
    assert "print(&#39;promoted&#39;)" in page.text
    assert "Kodi verification occurs in the next bounded stage" in page.text
    assert "Source promotion" in page.text
    assert "Kodi verification" in page.text
    assert (
        "Kodi verification does not change the already-completed source "
        "promotion result." in page.text
    )
    assert "<script>not markup</script>" not in page.text

    approve_url = f"{plans_url}/{plan_id}/approve-and-execute"
    assert client.post(approve_url, json={"confirmed": True}).status_code == 403
    for payload in ({}, {"confirmed": False}, {"confirmed": True, "force": True}):
        assert (
            client.post(approve_url, json=payload, headers=headers).status_code == 422
        )
    before_calls = list(broker.calls)
    approved = client.post(approve_url, json={"confirmed": True}, headers=headers)
    assert approved.status_code == 200
    operation = approved.json()
    assert operation["state"] == "succeeded"
    assert operation["handoff_published"] is True
    assert "container" not in approved.text
    assert "digest" not in approved.text
    assert broker.calls == before_calls
    verification = client.get(
        f"/api/instances/{INSTANCE_ID}/agent-workspaces/{workspace_id}/"
        f"promotion-operations/{operation['operation_id']}/verification"
    )
    assert verification.status_code == 200
    assert verification.json()["state"] == "failed"
    assert verification.json()["source_result_notice"] == (
        "Kodi verification does not change the already-completed source "
        "promotion result."
    )
    source = data_root.parent / "development-root" / "plugin.video.workspace"
    assert source.joinpath("resources/main.py").read_text() == "print('promoted')\n"
    workspace = client.get(f"/api/agent-workspaces/{workspace_id}").json()
    assert workspace["state"] == "promoted"
    repeated = client.post(approve_url, json={"confirmed": True}, headers=headers)
    assert repeated.status_code == 409


def test_promotion_rejection_is_confirmed_web_only_and_durable(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    broker: FakeBroker,
) -> None:
    _seed_source(database, data_root)
    _seed_promotion_target(database)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.workspace",
                name="Workspace",
                version="1.0.0",
                enabled=True,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    context = cast(FastAPI, client.app).state.kodi_lab
    mcp_client = context.mcp_administration.create_client(
        display_name="Cannot approve", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    bearer = context.mcp_administration.issue_credential(mcp_client.id).token
    forbidden_approval = client.post(
        f"/api/instances/{INSTANCE_ID}/agent-workspaces/{UUID(int=1)}/"
        f"promotion-plans/{UUID(int=2)}/approve-and-execute",
        json={"confirmed": True},
        headers={**ORIGIN, "Authorization": f"Bearer {bearer}"},
    )
    assert forbidden_approval.status_code == 401

    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    created = client.post(
        "/api/agent-workspaces",
        json={"development_source_id": str(SOURCE_ID)},
        headers=headers,
    ).json()
    for _ in range(100):
        creation = client.get(created["status_url"]).json()
        if creation["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    workspace_id = created["operation"]["workspace_id"]
    assert (
        client.put(
            f"/api/agent-workspaces/{workspace_id}/files",
            json={
                "expected_generation": 0,
                "path": "resources/main.py",
                "content": "print('reject me')\n",
            },
            headers=headers,
        ).status_code
        == 200
    )
    plans_url = (
        f"/api/instances/{INSTANCE_ID}/agent-workspaces/{workspace_id}/promotion-plans"
    )
    planned = client.post(plans_url, json={}, headers=headers)
    assert planned.status_code == 201, planned.text
    plan_id = planned.json()["plan_id"]
    reject_url = f"{plans_url}/{plan_id}/reject"
    assert client.post(reject_url, json={"confirmed": True}).status_code == 403
    for payload in ({}, {"confirmed": False}, {"confirmed": True, "force": True}):
        assert client.post(reject_url, json=payload, headers=headers).status_code == 422
    rejected = client.post(reject_url, json={"confirmed": True}, headers=headers)
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "invalidated"
    assert (
        client.post(
            f"{plans_url}/{plan_id}/approve-and-execute",
            json={"confirmed": True},
            headers=headers,
        ).status_code
        == 409
    )

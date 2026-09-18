from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage.repositories import DevelopmentSourceRepository

from .conftest import ORIGIN, login

SOURCE_ID = UUID("71000000-0000-0000-0000-000000000001")


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


def test_workspace_routes_do_not_expose_mutation_verbs(client: TestClient) -> None:
    csrf = login(client)
    workspace_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for suffix in ("promote", "delete", "rename", "mkdir", "git", "shell"):
        response = client.post(
            f"/api/agent-workspaces/{workspace_id}/{suffix}", json={}, headers=headers
        )
        assert response.status_code in {404, 405}


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

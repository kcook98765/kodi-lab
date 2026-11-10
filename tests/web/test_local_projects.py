"""Web-only onboarding, exact authority, profile preservation and presentation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.local_projects import LocalProjectSelection
from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.development.projects import LocalProjectResolver
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceRow,
    McpClientTargetGrantRow,
    McpPresetPermissionRow,
    TargetRow,
)
from tests.application.test_local_projects import write_project
from tests.web.conftest import ORIGIN, FakeBroker, inventory_entry, login


def setup_project(client: TestClient) -> tuple[Any, str]:
    context = cast(FastAPI, client.app).state.kodi_lab
    developer = context.mcp_administration.create_client(
        display_name="Local developer", preset_id=ADDON_DEVELOPMENT_PRESET_ID
    )
    write_project(context.settings.development_root, "hub/script.hub", "script.hub")
    return context, str(developer.id)


def test_onboarding_requires_web_admin_csrf_and_closed_inputs(
    client: TestClient,
) -> None:
    context, developer = setup_project(client)
    body = {"project": "hub", "client_id": developer}
    assert (
        client.post(
            "/api/development-projects/onboard", json=body, headers=ORIGIN
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/api/development-projects/{uuid4()}?client_id={developer}"
        ).status_code
        == 401
    )
    csrf = login(client)
    assert (
        client.post(
            "/api/development-projects/onboard", json=body, headers=ORIGIN
        ).status_code
        == 403
    )
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for extra in ({"root": "/home"}, {"target_id": str(uuid4())}, {"grant_all": True}):
        response = client.post(
            "/api/development-projects/onboard", json={**body, **extra}, headers=headers
        )
        assert response.status_code == 422
    assert (
        client.post(
            "/api/development-projects/onboard",
            json=body,
            headers={**headers, "Origin": "https://foreign.invalid"},
        ).status_code
        == 403
    )
    for project in ("../outside", "/tmp/host", "missing"):
        response = client.post(
            "/api/development-projects/onboard",
            json={**body, "project": project},
            headers=headers,
        )
        assert response.status_code == 409
        assert "message" in response.json()
    assert context.development_source_administration.list_sources() == []


def test_form_success_reuses_project_and_keeps_profiles_and_target_authority(
    client: TestClient, database: sessionmaker[Session], broker: FakeBroker
) -> None:
    context, developer = setup_project(client)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    container_before = broker.container
    with database() as db:
        target_before = [
            (r.id, r.target_type, r.name) for r in db.scalars(select(TargetRow))
        ]
        instance_before = [
            (r.target_id, r.runtime_revision_id)
            for r in db.scalars(select(ManagedInstanceRow))
        ]
        grants_before = [
            (r.client_id, r.target_id)
            for r in db.scalars(select(McpClientTargetGrantRow))
        ]
        permissions_before = [
            (r.preset_id, r.permission)
            for r in db.scalars(select(McpPresetPermissionRow))
        ]
    csrf = login(client)
    form = {"project": "hub", "client_id": developer, "csrf_token": csrf}
    assert (
        client.post(
            "/development-session/projects",
            data={**form, "csrf_token": "wrong"},
            headers=ORIGIN,
        ).status_code
        == 403
    )
    response = client.post(
        "/development-session/projects",
        data=form,
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    destination = response.headers["location"]
    for _ in range(100):
        page = client.get(destination)
        if 'data-state="ready"' in page.text:
            break
        time.sleep(0.01)
    assert page.status_code == 200 and 'data-state="ready"' in page.text
    assert "Choose Kodi version" in page.text and "script.hub" in page.text
    assert "Project setup does not establish native readiness" in page.text
    repeated = client.post(
        "/development-session/projects",
        data=form,
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert repeated.headers["location"] == destination
    assert len(context.agent_workspaces.list_workspaces()) == 1
    assert broker.container == container_before
    with database() as db:
        assert [
            (r.id, r.target_type, r.name) for r in db.scalars(select(TargetRow))
        ] == target_before
        assert [
            (r.target_id, r.runtime_revision_id)
            for r in db.scalars(select(ManagedInstanceRow))
        ] == instance_before
        assert [
            (r.client_id, r.target_id)
            for r in db.scalars(select(McpClientTargetGrantRow))
        ] == grants_before
        assert [
            (r.preset_id, r.permission)
            for r in db.scalars(select(McpPresetPermissionRow))
        ] == permissions_before
        assert not db.scalars(select(DevelopmentSessionRow)).all()


def test_actionable_error_and_no_root_setup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, developer = setup_project(client)
    csrf = login(client)
    response = client.post(
        "/development-session/projects",
        data={"project": "missing", "client_id": developer, "csrf_token": csrf},
        headers=ORIGIN,
    )
    assert response.status_code == 422 and 'role="alert"' in response.text
    assert "missing, inaccessible or unsafe" in response.text
    monkeypatch.setattr(context.settings, "development_root", None)
    monkeypatch.setattr(context.local_projects, "_resolver", LocalProjectResolver(None))
    page = client.get("/development-session")
    assert "Set up a local development root" in page.text
    assert "/home/kyle" not in page.text
    setup = client.get("/development-session/project-setup")
    assert (
        setup.status_code == 200
        and "No directory is selected automatically" in setup.text
    )
    assert "KODI_LAB_DEVELOPMENT_ROOT" in setup.text


def test_status_does_not_regrant_revoked_access(client: TestClient) -> None:
    context, developer = setup_project(client)
    csrf = login(client)
    result = client.post(
        "/api/development-projects/onboard",
        json={"project": "hub", "client_id": developer},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 200, result.text
    payload = result.json()

    context.mcp_administration.remove_development_source_grant(
        UUID(developer), UUID(payload["source_id"])
    )
    url = f"/api/development-projects/{payload['workspace_id']}?client_id={developer}"
    assert client.get(url).status_code == 409
    assert (
        context.mcp_administration.get_client(UUID(developer)).development_source_grants
        == ()
    )


def test_export_local_project_browser_fixtures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, developer = setup_project(client)
    login(client)
    pages = {"select": client.get("/development-session").text}
    submitted: list[UUID] = []
    monkeypatch.setattr(
        context.local_projects._creation, "_submit_creation", submitted.append
    )
    selected = context.local_projects.onboard(
        LocalProjectSelection(client_id=UUID(developer), project="hub")
    )
    url = (
        f"/development-session?workspace_id={selected.workspace_id}"
        f"&client_id={developer}"
    )
    pages["preparing"] = client.get(url).text
    assert len(submitted) == 1
    lease = context.agent_workspaces.acquire(selected.creation_operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    ready = context.local_projects.status(selected.client_id, selected.workspace_id)
    pages["ready"] = client.get(url).text
    pages["versions"] = client.get(ready.next_url).text
    csrf = login(client)
    pages["error"] = client.post(
        "/development-session/projects",
        data={"project": "../escape", "client_id": developer, "csrf_token": csrf},
        headers=ORIGIN,
    ).text
    monkeypatch.setattr(context.settings, "development_root", None)
    monkeypatch.setattr(context.local_projects, "_resolver", LocalProjectResolver(None))
    pages["unconfigured"] = client.get("/development-session").text
    if destination := os.getenv("KODI_LAB_PROJECT_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixtures.json").write_text(
            json.dumps(
                {
                    "pages": pages,
                    "preparing": selected.model_dump(mode="json"),
                    "ready": ready.model_dump(mode="json"),
                }
            )
        )

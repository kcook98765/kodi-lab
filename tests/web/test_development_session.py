"""Session Web routes preserve admin authority and never expose disposal."""

import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_session_workflow import DevelopmentSessionWorkflow
from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.storage.models import DevelopmentSessionRow
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, inventory_entry, login


def source_and_workspace(client: TestClient) -> tuple[Any, Any]:
    context = cast(FastAPI, client.app).state.kodi_lab
    source = context.settings.development_root / "plugin.video.session-wire"
    source.mkdir()
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.session-wire" name="Session" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>\n"
    )
    (source / "main.py").write_text("pass\n")
    registered = context.development_source_administration.register_source(
        logical_name="Session source", relative_locator=source.name
    )
    creation = context.agent_workspaces.queue_creation(registered.id)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    return source, creation


def test_session_page_and_actions_require_authentication(client: TestClient) -> None:
    assert client.get("/development-session", follow_redirects=False).status_code == 303
    assert client.get("/api/development-session").status_code == 401
    for action in ("reserve", "prepare", "resume", "stop", "close-absent"):
        assert (
            client.post(
                f"/api/development-session/{action}", json={}, headers=ORIGIN
            ).status_code
            == 401
        )
    login(client)
    page = client.get("/development-session")
    assert (
        page.status_code == 200 and "Resume and Stop keep the same profile" in page.text
    )
    assert 'href="/development-session"' in client.get("/development").text
    assert "No approved cached development runtime" in page.text
    assert "Watch this Kodi" in page.text


def test_reserve_prepare_replay_and_absence_only_close_have_closed_inputs(
    client: TestClient, database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    _source, creation = source_and_workspace(client)
    # This route proof doubles runtime qualification; admission has separate tests.
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_args: None)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    body = {
        "request_id": str(uuid4()),
        "expected_generation": 0,
        "target_id": str(INSTANCE_ID),
        "workspace_id": str(creation.workspace_id),
    }
    assert (
        client.post(
            "/api/development-session/reserve", json=body, headers=ORIGIN
        ).status_code
        == 403
    )
    reserved = client.post(
        "/api/development-session/reserve", json=body, headers=headers
    )
    assert reserved.status_code == 200, reserved.text
    assert reserved.json()["phase"] == "reserved"
    assert (
        client.post(
            "/api/development-session/reserve", json=body, headers=headers
        ).json()
        == reserved.json()
    )
    assert client.get("/api/development-session").json()["current"]["generation"] == 1
    calls = []

    def action(_self: Any, kind: str, generation: int) -> Any:
        calls.append((kind, generation))
        return context.status.get(INSTANCE_ID)

    monkeypatch.setattr(DevelopmentSessionWorkflow, "act", action)
    assert (
        client.post(
            "/api/development-session/prepare",
            json={"expected_generation": 1},
            headers=headers,
        ).status_code
        == 200
    )
    for invalid in (
        {},
        {"expected_generation": 0},
        {"expected_generation": 1, "target_id": str(uuid4())},
        {"expected_generation": 1, "force": True},
    ):
        assert (
            client.post(
                "/api/development-session/prepare", json=invalid, headers=headers
            ).status_code
            == 422
        )
    assert calls == [("prepare", 1)]
    assert (
        client.post(
            "/api/development-session/switch",
            json={"expected_generation": 1},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/development-session/delete",
            json={"expected_generation": 1},
            headers=headers,
        ).status_code
        == 422
    )
    closed = client.post(
        "/api/development-session/close-absent",
        json={"expected_generation": 1},
        headers=headers,
    )
    assert closed.status_code == 200 and closed.json()["phase"] == "closed"
    with database() as session:
        assert len(session.scalars(select(DevelopmentSessionRow)).all()) == 1


def test_existing_stopped_profile_is_preserved(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.docker_inventory import ContainerRuntimeState

    _source, creation = source_and_workspace(client)
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_args: None)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    before = broker.container
    csrf = login(client)
    result = client.post(
        "/api/development-session/reserve",
        json={
            "request_id": str(uuid4()),
            "expected_generation": 0,
            "target_id": str(INSTANCE_ID),
            "workspace_id": str(creation.workspace_id),
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert (
        result.status_code == 409
        and result.json()["error"]["code"] == "development_session_existing_resource"
    )
    assert broker.container == before
    assert not any(
        call[0] in {"start", "stop", "provision", "deprovision"}
        for call in broker.calls
    )


def test_session_browser_request_and_uncertain_mutation_guards() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("development_session_browser.js")),
            str(root / "src/kodi_lab/web/static/development-session.js"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )

"""The selected-session checklist is private Web observation, never a setup job."""

import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.application.session_preparation import SessionPreparationService
from kodi_lab.storage.models import (
    DevelopmentSourceRow,
    McpWorkspaceExecutionOperationRow,
)
from tests.application import test_mcp_workspace_development as workspace_fixtures
from tests.application.test_session_workspace_admission import bind
from tests.web.conftest import INSTANCE_ID, ORIGIN, login
from tests.web.test_development_session import source_and_workspace


def export(client: TestClient, name: str, path: str) -> None:
    if destination := os.getenv("KODI_GUIDANCE_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{name}.html").write_text(client.get(path).text)
        if path == "/development-session":
            (root / f"{name}-status.json").write_text(
                client.get("/api/development-session").text
            )


def reserve(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _source, creation = source_and_workspace(client)
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_args: None)
    csrf = login(client)
    result = client.post(
        "/api/development-session/reserve",
        json={
            "request_id": str(uuid4()),
            "expected_generation": 0,
            "workspace_id": str(creation.workspace_id),
            "target_id": str(INSTANCE_ID),
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 200, result.text
    return cast(dict[str, Any], result.json())


def test_preparation_requires_web_auth_and_empty_session_does_not_prepare(
    client: TestClient,
) -> None:
    assert client.get("/api/development-session/preparation").status_code == 401
    response = client.get("/development-session/preparation", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    login(client)
    result = client.get("/api/development-session/preparation")
    assert result.status_code == 200 and result.json() is None
    assert result.headers["cache-control"] == "no-store"
    page = client.get("/development-session/preparation")
    assert page.status_code == 200
    assert "No Current Managed Kodi development selection is saved." in page.text
    assert page.headers["cache-control"] == "no-store"
    export(client, "preparation-empty", "/development-session/preparation")
    export(client, "project-empty", "/development-session")
    assert (
        'href="/development-session/preparation"'
        in client.get("/development-session").text
    )


def test_private_checklist_is_read_only_unknown_and_escapes_selected_metadata(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = reserve(client, monkeypatch)
    project = client.get("/development-session")
    assert "it does not mean the add-on is deployed, enabled or has run" in project.text
    export(client, "project-selected", "/development-session")
    context = cast(FastAPI, client.app).state.kodi_lab
    with database.begin() as session:
        source = session.get(DevelopmentSourceRow, current["source_id"])
        assert source is not None
        source.logical_name = "<img src=x onerror=alert(1)>"

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("checklist cannot probe, dispatch or read credentials")

    for owner, name in (
        (context.status, "get"),
        (context.status, "get_observation"),
        (context.managed_bridge_trust, "prepare"),
        (context.managed_bridge_trust.broker, "managed_container_inventory"),
    ):
        monkeypatch.setattr(owner, name, forbidden)
    commands: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        command = sql.lstrip().split()[0].upper()
        commands.append(command)
        # Ordinary Web authentication updates its own last-use timestamp.
        # Preserve that existing protection while checking business-state reads.
        assert command in {"SELECT", "PRAGMA", "BEGIN"} or (
            command == "UPDATE"
            and sql.lstrip().upper().startswith("UPDATE WEB_SESSIONS ")
        )

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = client.get(
            "/api/development-session/preparation?expected_generation=1"
        )
        page = client.get("/development-session/preparation?expected_generation=1")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result.status_code == page.status_code == 200
    payload = result.json()
    assert (
        payload["session"]["generation"] == 1
        and payload["session"]["source_id"] == current["source_id"]
    )
    assert payload["assessment_scope"] == "stored_session_preparation"
    assert payload["native_readiness"] == payload["addon_enabled"] == "unknown"
    assert (
        payload["agent_authority"] == "not_assessed"
        and not payload["reservation_created"]
    )
    assert payload["capabilities"]["current_container_binding"] == "not_observed"
    # API and HTML preparation each keep their own coherent snapshot.
    # HTML also reads bounded original-session copy receipts separately.
    assert commands.count("BEGIN") == 3
    assert (
        result.headers["cache-control"] == page.headers["cache-control"] == "no-store"
    )
    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert "Prepare Kodi" in page.text and "current Bridge connection" in page.text
    assert "Agent permissions have not been assessed" in page.text
    assert f"/agent-workspaces/{current['workspace_id']}" in page.text
    assert f"/instances/{current['target_id']}/capability-prerequisites" in page.text
    assert "A selected workspace alone does not establish readiness" in page.text
    assert "Deployed means files were copied to Kodi" in page.text
    assert "Enabled means Kodi allows the add-on to run" in page.text
    assert "evidence that the add-on ran in Kodi" in page.text
    assert "a successful deployment alone does not verify execution" in page.text
    export(client, "preparation-selected", "/development-session/preparation")


@pytest.mark.parametrize("deployed", [False, True])
def test_deployment_never_claims_enabled_or_executed_in_guidance(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployed: bool,
) -> None:
    # Produce real stored preparation facts using an isolated fake runtime.
    env = workspace_fixtures._environment(tmp_path)
    if deployed:
        workspace_fixtures._deploy_workspace(env)
    bind(env)
    assessment = SessionPreparationService(env.sessions).inspect()
    assert assessment is not None
    assert assessment.addon_enabled == "unknown"
    assert assessment.agent_authority == "not_assessed"
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceExecutionOperationRow)) is None
    assert env.smoke.count == 0
    monkeypatch.setattr(
        SessionPreparationService, "inspect", lambda *_args, **_kwargs: assessment
    )
    login(client)
    page = client.get("/development-session/preparation")
    assert page.status_code == 200
    assert "a successful deployment alone does not verify execution" in page.text
    assert "Agent permissions have not been assessed" in page.text
    assert "enabled add-on have not been checked" in page.text
    export(
        client, f"preparation-deployed-{deployed}", "/development-session/preparation"
    )


def test_stale_checklist_generation_refuses_without_mutating_session(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = reserve(client, monkeypatch)
    for prefix in ("/api", ""):
        result = client.get(
            f"{prefix}/development-session/preparation?expected_generation=2"
        )
        assert result.status_code == 409
        assert (
            result.json()["error"]["code"] == "development_session_generation_conflict"
        )
        for value in ("0", "-1", "true", "bogus"):
            assert (
                client.get(
                    f"{prefix}/development-session/preparation?expected_generation={value}"
                ).status_code
                == 422
            )
    status = client.get("/api/development-session").json()
    assert status["current"] == current

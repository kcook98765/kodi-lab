"""The selected-session checklist is private Web observation, never a setup job."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.storage.models import DevelopmentSourceRow
from tests.web.conftest import INSTANCE_ID, ORIGIN, login
from tests.web.test_development_session import source_and_workspace


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
    assert page.status_code == 200 and "No development session is selected" in page.text
    assert page.headers["cache-control"] == "no-store"
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
    assert commands.count("BEGIN") == 2
    assert (
        result.headers["cache-control"] == page.headers["cache-control"] == "no-store"
    )
    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text
    assert "Prepare Kodi" in page.text and "current Bridge connection" in page.text
    assert "Agent permissions have not been assessed" in page.text
    assert f"/agent-workspaces/{current['workspace_id']}" in page.text
    assert f"/instances/{current['target_id']}/capability-prerequisites" in page.text


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

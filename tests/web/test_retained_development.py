"""Actual admin HTTP/HTML routes share the retained session authority."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from tests.application.test_mcp_retained_selection import authorize
from tests.application.test_retained_development import environment
from tests.web.conftest import ORIGIN, FakeBroker, login
from tests.web.test_mcp import _post


@pytest.fixture(autouse=True)
def multiple_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    original = FakeBroker.managed_container_inventory

    def inventory(self: Any, *, managed_instance_id: str | None = None) -> Any:
        if not hasattr(self, "retained_records"):
            return original(self, managed_instance_id=managed_instance_id)
        return ManagedContainerInventoryResult(
            containers=[
                r
                for r in self.retained_records()
                if managed_instance_id is None
                or r.managed_instance_id == managed_instance_id
            ]
        )

    monkeypatch.setattr(FakeBroker, "managed_container_inventory", inventory)


def seed(
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    with database() as session:
        path = Path(str(session.get_bind().engine.url.database))
    s = environment(tmp_path / "case", monkeypatch, database_path=path)
    monkeypatch.setattr(
        broker, "retained_records", lambda: s.inventory().containers, raising=False
    )
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_: None)
    monkeypatch.setattr(
        "kodi_lab.application.mcp_workspace_development.BoundedIntervalRateLimiter.allow",
        lambda *_: True,
    )
    return s


def test_retained_admin_auth_csrf_strict_inputs_and_existing_legacy_default(
    client: TestClient,
) -> None:
    for path in ("policy", "selection-preview"):
        assert client.get("/api/development-session/" + path).status_code == 401
    for path in ("policy", "select"):
        assert (
            client.post(
                "/api/development-session/" + path, json={}, headers=ORIGIN
            ).status_code
            == 401
        )
    csrf = login(client)
    assert client.get("/api/development-session/policy").json()["enabled"] is False
    body = {"enabled": True, "expected_generation": 0}
    assert (
        client.post(
            "/api/development-session/policy", json=body, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/development-session/policy",
            json={**body, "enabled": "true"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )


def test_four_profiles_sequential_selection_over_http_and_without_javascript(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    before = {t: p.read_bytes() for t, p in s.profiles.items()}
    first = None
    for generation in ("omega", "nexus", "matrix", "omega"):
        params = {
            "target_id": str(s.ids[generation]),
            "workspace_id": str(s.env.workspace_id),
        }
        preview = client.get(
            "/api/development-session/selection-preview", params=params
        )
        assert preview.status_code == 200, preview.text
        p = preview.json()
        body = {
            **params,
            "request_id": str(uuid4()),
            "expected_generation": p["latest_generation"],
            "identity": p["identity"],
        }
        selected = client.post(
            "/api/development-session/select", json=body, headers=headers
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["phase"] == "bound"
        if first is None:
            first = body
    replay = client.post("/api/development-session/select", json=first, headers=headers)
    assert replay.json()["phase"] == "closed"
    assert s.service.status().generation == 4
    assert {t: p.read_bytes() for t, p in s.profiles.items()} == before
    page = client.get(
        "/development-session/retained",
        params={
            "target_id": str(s.ids["nexus"]),
            "workspace_id": str(s.env.workspace_id),
        },
    )
    assert page.status_code == 200 and "Select and keep profiles" in page.text
    for tid in s.ids.values():
        assert str(tid) in page.text
    p = client.get(
        "/api/development-session/selection-preview",
        params={
            "target_id": str(s.ids["nexus"]),
            "workspace_id": str(s.env.workspace_id),
        },
    ).json()
    form = {
        "csrf_token": csrf,
        "request_id": str(uuid4()),
        "expected_generation": str(p["latest_generation"]),
        "identity": p["identity"],
        "target_id": str(s.ids["nexus"]),
        "workspace_id": str(s.env.workspace_id),
    }
    assert (
        client.post(
            "/development-session/retained/select",
            data=form,
            headers={"Origin": "http://attacker.invalid"},
            follow_redirects=False,
        ).status_code
        == 403
    )
    posted = client.post(
        "/development-session/retained/select",
        data=form,
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert s.service.status().target_id == s.ids["nexus"]
    assert len(s.records) == 4
    current = client.get("/development-session")
    assert current.status_code == 200
    assert 'href="/development-session/retained"' in current.text


def test_authenticated_mcp_wire_selection_is_strict_and_does_not_start_kodi(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    s.choose("omega")
    authorize(s)
    params = {"target_id": str(s.ids["nexus"]), "workspace_id": str(s.env.workspace_id)}
    preview = _post(
        client,
        s.token,
        "tools/call",
        params={"name": "managed_selection_preview", "arguments": params},
    ).json()["result"]
    assert not preview.get("isError"), preview
    p = preview["structuredContent"]
    body = {
        **params,
        "request_id": str(uuid4()),
        "expected_generation": p["latest_generation"],
        "identity": p["identity"],
    }
    rejected = _post(
        client,
        s.token,
        "tools/call",
        params={
            "name": "managed_select",
            "arguments": {**body, "expected_generation": str(p["latest_generation"])},
        },
    ).json()["result"]
    assert rejected["isError"]
    selected = _post(
        client,
        s.token,
        "tools/call",
        params={"name": "managed_select", "arguments": body},
    ).json()["result"]
    assert not selected.get("isError"), selected
    assert selected["structuredContent"]["target_id"] == str(s.ids["nexus"])
    assert "container_id" not in selected["structuredContent"]
    assert all(r.runtime_state == "stopped" for r in s.records)
    assert not broker.calls

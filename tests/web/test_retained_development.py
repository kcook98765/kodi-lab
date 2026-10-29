"""Actual admin HTTP/HTML routes share the retained session authority."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from tests.application.test_managed_bridge_trust import Client
from tests.application.test_managed_resources import ImmediateReadiness
from tests.application.test_mcp_retained_selection import authorize
from tests.application.test_mcp_workspace_development import SOURCE_ID
from tests.application.test_retained_bridge_lifecycle import Broker
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


@pytest.mark.parametrize("generation", ["0", 0.0, False])
def test_policy_json_generation_is_a_strict_integer(
    client: TestClient, generation: Any
) -> None:
    csrf = login(client)
    result = client.post(
        "/api/development-session/policy",
        json={
            "enabled": True,
            "expected_generation": generation,
            "omega_target_id": str(recipe_target_id(load_installation_recipe("omega"))),
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 422
    assert not client.get("/api/development-session/policy").json()["enabled"]


def test_html_confirmation_identifies_previewed_workspace_and_target(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    s.choose("omega")
    queued = s.env.workspaces.queue_creation(SOURCE_ID)
    lease = s.env.workspaces.acquire(queued.operation_id)
    assert lease is not None
    s.env.workspaces.run(lease)
    login(client)
    page = client.get(
        "/development-session/retained",
        params={
            "target_id": str(s.ids["nexus"]),
            "workspace_id": str(queued.workspace_id),
        },
    )
    assert page.status_code == 200
    assert re.search(
        r'<option value="' + str(queued.workspace_id) + r'"[^>]* selected', page.text
    )
    assert re.search(
        r'<option value="' + str(s.ids["nexus"]) + r'"[^>]* selected', page.text
    )
    assert (
        "Workspace: Slice 4 source · plugin.video.slice4 · " + str(queued.workspace_id)
        in page.text
    )
    assert not re.search(
        r'<option value="' + str(s.env.workspace_id) + r'"[^>]* selected', page.text
    )


@pytest.mark.parametrize("name", ["managed_selection_preview", "managed_select"])
def test_mcp_workspace_admin_refusal_is_audited_as_authorization_denied(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    s.choose("omega")
    authorize(s)
    queued = s.env.workspaces.queue_creation(SOURCE_ID)
    lease = s.env.workspaces.acquire(queued.operation_id)
    assert lease is not None
    s.env.workspaces.run(lease)
    s.admin.bind_workspace(s.env.client_id, queued.workspace_id)
    arguments: dict[str, Any] = {
        "target_id": str(s.ids["nexus"]),
        "workspace_id": str(queued.workspace_id),
    }
    if name == "managed_select":
        arguments.update(
            request_id=str(uuid4()), expected_generation=1, identity="a" * 64
        )
    caplog.set_level(logging.INFO, logger="kodi_lab.mcp")
    result = _post(
        client, s.token, "tools/call", params={"name": name, "arguments": arguments}
    ).json()["result"]
    assert result["isError"]
    records = [
        json.loads(r.getMessage().removeprefix("mcp_request "))
        for r in caplog.records
        if r.getMessage().startswith("mcp_request ")
    ]
    record = next(r for r in records if r["tool"] == name)
    assert record["outcome"] == "authorization_denied"
    assert record["result_code"] == "retained_workspace_requires_administrator"


@pytest.mark.parametrize("web_target", ["omega", "nexus"])
def test_concurrent_authenticated_web_and_mcp_start_requests_share_admission(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    web_target: str,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    s.choose("omega")
    authorize(s)
    csrf = login(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    delegate = Broker(s, Client())
    # Keep HTTP authentication, services, SQLite ownership and broker capacity
    # real. Native/Bridge execution is a separate service-level regression.
    context.lifecycle._broker = delegate
    context.status._broker = delegate
    context.managed_resources._broker = delegate
    context.managed_resources._playback_factory = lambda *_: SimpleNamespace(
        playback_active=lambda: False
    )
    context.lifecycle._coordinator_factory = lambda *_: ImmediateReadiness()
    context.lifecycle._bridge_trust = None
    delegate.start_entered, delegate.start_finish = Event(), Event()
    before = {t: p.read_bytes() for t, p in s.profiles.items()}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                _post,
                client,
                s.token,
                "tools/call",
                params={
                    "name": "managed_ensure_ready",
                    "arguments": {
                        "target_id": str(s.ids["omega"]),
                        "request_id": str(uuid4()),
                    },
                },
            )
            try:
                assert delegate.start_entered.wait(5)
                web = client.post(
                    f"/api/instances/{s.ids[web_target]}/start",
                    json={},
                    headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
                )
                assert web.status_code == 409, web.text
                assert delegate.starts == 1
            finally:
                delegate.start_finish.set()
            result = future.result(timeout=10).json()["result"]
            assert not result.get("isError"), result
        assert sum(r.runtime_state == "running" for r in s.records) == 1
        context.readiness.close()
        released = _post(
            client,
            s.token,
            "tools/call",
            params={
                "name": "managed_release",
                "arguments": {
                    "target_id": str(s.ids["omega"]),
                    "request_id": str(uuid4()),
                },
            },
        ).json()["result"]
        assert not released.get("isError"), released
        assert (
            len(s.records) == 4
            and {t: p.read_bytes() for t, p in s.profiles.items()} == before
        )
    finally:
        delegate.start_finish.set()
        context.readiness.close()


@pytest.mark.parametrize("name", ["managed_selection_preview", "managed_select"])
def test_target_policy_revocation_is_audited_before_selection_replay(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from kodi_lab.application.mcp_administration import ADDON_DEVELOPMENT_PRESET_ID

    s = seed(database, broker, tmp_path, monkeypatch)
    s.choose("omega")
    authorize(s)
    params = {"target_id": str(s.ids["nexus"]), "workspace_id": str(s.env.workspace_id)}
    p = _post(
        client,
        s.token,
        "tools/call",
        params={"name": "managed_selection_preview", "arguments": params},
    ).json()["result"]["structuredContent"]
    body = {
        **params,
        "request_id": str(uuid4()),
        "expected_generation": p["latest_generation"],
        "identity": p["identity"],
    }
    first = _post(
        client,
        s.token,
        "tools/call",
        params={"name": "managed_select", "arguments": body},
    ).json()["result"]
    assert not first.get("isError"), first
    s.admin.assign_target_preset(s.ids["nexus"], ADDON_DEVELOPMENT_PRESET_ID)
    caplog.set_level(logging.INFO, logger="kodi_lab.mcp")
    result = _post(
        client,
        s.token,
        "tools/call",
        params={
            "name": name,
            "arguments": params if name == "managed_selection_preview" else body,
        },
    ).json()["result"]
    assert result["isError"]
    records = [
        json.loads(r.getMessage().removeprefix("mcp_request "))
        for r in caplog.records
        if r.getMessage().startswith("mcp_request ")
    ]
    record = next(r for r in records if r["tool"] == name)
    assert record["outcome"] == "authorization_denied"
    assert record["result_code"] == "permission_denied"


def test_slow_html_selection_does_not_block_other_http_requests(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = seed(database, broker, tmp_path, monkeypatch)
    csrf = login(client)
    params = {"target_id": str(s.ids["omega"]), "workspace_id": str(s.env.workspace_id)}
    p = client.get("/api/development-session/selection-preview", params=params).json()
    body = {
        **params,
        "csrf_token": csrf,
        "request_id": str(uuid4()),
        "expected_generation": str(p["latest_generation"]),
        "identity": p["identity"],
    }
    entered, finish = Event(), Event()
    original = DevelopmentSessionService.select_retained

    def slow(self: Any, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert finish.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DevelopmentSessionService, "select_retained", slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        form = pool.submit(
            client.post,
            "/development-session/retained/select",
            data=body,
            headers=ORIGIN,
            follow_redirects=False,
        )
        try:
            assert entered.wait(5)
            other = pool.submit(client.get, "/api/development-session/policy")
            assert other.result(timeout=1).status_code == 200
        finally:
            finish.set()
        assert form.result(timeout=5).status_code == 303


def test_opt_in_filters_eligible_legacy_omega_and_preserves_unsupported_selection(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from kodi_lab.application.development_sessions import RetainedPolicyRequest
    from kodi_lab.runtime.lab import canonical_id
    from kodi_lab.storage.models import (
        DevelopmentSessionRow,
        ManagedInstanceRow,
        TargetRow,
    )

    s = seed(database, broker, tmp_path, monkeypatch)
    template = s.records[2]
    s.records.clear()
    s.service.configure_retained(
        RetainedPolicyRequest(enabled=False, expected_generation=0)
    )
    legacy = canonical_id("21")
    with database.begin() as session:
        session.add(
            TargetRow(
                id=str(legacy),
                name="Legacy Omega registration",
                target_type="managed",
                kodi_version="21.3",
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(legacy),
                instance_type="disposable",
                runtime_revision_id=str(s.revisions[s.ids["omega"]]),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    csrf = login(client)
    reserved = client.post(
        "/api/development-session/reserve",
        json={
            "request_id": str(uuid4()),
            "expected_generation": 0,
            "target_id": str(legacy),
            "workspace_id": str(s.env.workspace_id),
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert reserved.status_code == 200, reserved.text
    s.records.append(
        replace(template, managed_instance_id=str(legacy), container_id="f" * 64)
    )
    with database.begin() as session:
        row = session.get(DevelopmentSessionRow, 1)
        assert row is not None
        row.phase, row.container_id = "bound", "f" * 64
    page = client.get("/development-session/retained")
    assert page.status_code == 200
    assert str(s.ids["omega"]) in page.text
    assert not re.search(r'<option value="' + str(legacy) + r'"', page.text)
    assert "current selection is outside the reviewed retained versions" in page.text
    assert "disabled>Enable retained versions" in page.text
    assert s.service.status().target_id == legacy
    assert s.records[0].container_id == "f" * 64

"""Standalone pages restrict the existing viewer to their rendered incarnation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.runtime.display_capabilities import OMEGA_DISPLAY_CANDIDATE
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceRow,
    NovncSessionRow,
    RuntimeContainerArtifactRow,
)
from kodi_lab.web.app import create_app
from kodi_lab.web.novnc import (
    ChangeMode,
    PrototypeViewer,
    StartView,
    binding_fingerprint,
)
from tests.web import conftest
from tests.web.conftest import (
    CONTAINER_ID,
    IMAGE_ID,
    INSTANCE_ID,
    ORIGIN,
    REVISION_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    login,
)
from tests.web.test_current_kodi import selected
from tests.web.test_novnc_control import mutation
from tests.web.view_attempt_fixture import view_attempt

PAGE = f"/instances/{INSTANCE_ID}/novnc-phase1"
START = f"/api/instances/{INSTANCE_ID}/novnc-start"


@pytest.fixture
def standalone_client(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    database: sessionmaker[Session],
) -> TestClient:
    with database.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
    state: dict[str, Any] = {
        "pin": ViewBinding(CONTAINER_ID, IMAGE_ID, "original-start", ()),
        "available": True,
    }

    def authority(instance: Any) -> ViewBinding:
        if not state["available"]:
            raise ValueError("private unavailable reason")
        return CatalogViewAuthority(
            database, {INSTANCE_ID: state["pin"]}, capability=OMEGA_DISPLAY_CANDIDATE
        ).resolve(instance)

    def configured(**kwargs: Any) -> FastAPI:
        viewer = PrototypeViewer(
            Path("/tmp/no-standalone-native-socket"),
            authority,
            registry=LiveDisplaySessionRegistry(),
            ownership=HumanViewerOwnership(database),
            enabled_instances=frozenset({INSTANCE_ID}),
        )
        kwargs["novnc_viewer"] = viewer
        app = create_app(**kwargs)
        app.state.standalone_viewer = viewer
        app.state.standalone_runtime = state
        return app

    monkeypatch.setattr(conftest, "create_app", configured)
    return cast(TestClient, request.getfixturevalue("client"))


def service(client: TestClient) -> PrototypeViewer:
    return cast(PrototypeViewer, cast(FastAPI, client.app).state.standalone_viewer)


def runtime(client: TestClient) -> dict[str, Any]:
    return cast(dict[str, Any], cast(FastAPI, client.app).state.standalone_runtime)


def rendered_binding(client: TestClient) -> str:
    response = client.get(PAGE)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = re.search(r'data-view-binding="([0-9a-f]{64})"', response.text)
    assert match is not None, response.text
    assert 'data-view-generation=""' in response.text
    assert "data-view-workspace" not in response.text
    return match[1]


@pytest.mark.parametrize("other_selected", [False, True])
def test_standalone_page_preserves_development_selection_and_opens_no_worker(
    standalone_client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    other_selected: bool,
) -> None:
    client = standalone_client
    csrf = login(client)
    if other_selected:
        selected(client, database, monkeypatch)
        with database.begin() as db:
            row = db.get(DevelopmentSessionRow, 1)
            assert row is not None
            row.target_id = str(UNRELATED_INSTANCE_ID)
    engine = database.kw["bind"]
    writes: list[str] = []

    def record(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        if sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(sql)

    event.listen(engine, "before_cursor_execute", record)
    try:
        fingerprint = rendered_binding(client)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert writes == []
    assert not service(client).attachments
    assert service(client).registry.active_count() == 0
    assert broker.calls == [] and broker.screenshot_calls == 0
    with database() as db:
        rows = list(db.scalars(select(DevelopmentSessionRow)))
        assert len(rows) == int(other_selected)
        if rows:
            assert rows[0].target_id == str(UNRELATED_INSTANCE_ID)
        assert db.get(NovncSessionRow, str(INSTANCE_ID)) is None
    # Entry point is outside the collapsed maintenance/viewer sections.
    detail = client.get(f"/instances/{INSTANCE_ID}").text
    assert detail.index(">Open screen</a>") < detail.index(
        "Advanced lifecycle and maintenance controls"
    )
    attempt = view_attempt()
    response = client.post(
        START,
        json={"attempt": str(attempt), "expected_binding": fingerprint},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 200 and response.json()["mode"] == "view-only"
    viewer = service(client)
    item = viewer.attachments[attempt]
    viewer.stop(item.owner, attempt)
    viewer.end(item)
    with database() as db:
        rows = list(db.scalars(select(DevelopmentSessionRow)))
        assert len(rows) == int(other_selected)
        if rows:
            assert rows[0].target_id == str(UNRELATED_INSTANCE_ID)


@pytest.mark.parametrize(
    "change",
    ["container", "image", "started", "revision", "epoch", "selection", "unsafe"],
)
def test_rendered_standalone_context_cannot_adopt_changed_identity(
    standalone_client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    client = standalone_client
    csrf = login(client)
    fingerprint = rendered_binding(client)
    state = runtime(client)
    if change in {"container", "image", "started"}:
        state["pin"] = replace(
            state["pin"],
            **{
                change: {
                    "container": "c" * 64,
                    "image": "sha256:" + "c" * 64,
                    "started": "replacement-start",
                }[change]
            },
        )
    elif change == "revision":
        state["pin"] = replace(state["pin"], identity=(str(uuid4()), 0, None))
    elif change == "epoch":
        with database.begin() as db:
            instance = db.get(ManagedInstanceRow, str(INSTANCE_ID))
            assert instance is not None
            instance.runtime_state_revision += 1
    elif change == "selection":
        selected(client, database, monkeypatch)
    else:
        mutation(database)
    response = client.post(
        START,
        json={"attempt": str(view_attempt()), "expected_binding": fingerprint},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409
    assert not service(client).attachments
    assert service(client).registry.active_count() == 0
    with database() as db:
        assert db.get(NovncSessionRow, str(INSTANCE_ID)) is None


def test_standalone_admission_is_view_only_owner_bound_one_use_and_cleans_up(
    standalone_client: TestClient, database: sessionmaker[Session]
) -> None:
    client = standalone_client
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    fingerprint = rendered_binding(client)
    attempt = view_attempt()
    body = {"attempt": str(attempt), "expected_binding": fingerprint}
    reply = client.post(START, json=body, headers=headers)
    assert reply.status_code == 200 and reply.json()["mode"] == "view-only"
    viewer = service(client)
    attachment = viewer.attachments[attempt]
    assert attachment.binding.identity == (str(REVISION_ID), 0, None)
    assert viewer.ownership is not None
    status = viewer.ownership.status(INSTANCE_ID)
    assert status is not None and status.mode == "view-only"
    ticket = reply.json()["ticket"]
    for instance, owner in ((uuid4(), attachment.owner), (INSTANCE_ID, "wrong-owner")):
        with pytest.raises(ValueError):
            viewer.consume(instance, owner, ticket)
    assert viewer.consume(INSTANCE_ID, attachment.owner, ticket) is attachment
    with pytest.raises(ValueError):
        viewer.consume(INSTANCE_ID, attachment.owner, ticket)
    assert client.post(START, json=body, headers=headers).status_code == 409
    stopped = client.post(
        f"/api/instances/{INSTANCE_ID}/novnc-stop",
        json={"attempt": str(attempt)},
        headers=headers,
    )
    assert stopped.status_code == 200
    viewer.end(attachment)
    assert not viewer.attachments and viewer.registry.active_count() == 0
    with database() as db:
        assert db.get(NovncSessionRow, str(INSTANCE_ID)) is None
        assert db.get(DevelopmentSessionRow, 1) is None


def test_standalone_start_requires_rendered_context_auth_csrf_and_origin(
    standalone_client: TestClient,
) -> None:
    client = standalone_client
    body = {"attempt": str(view_attempt()), "expected_binding": "a" * 64}
    assert client.get(PAGE).status_code == 401
    assert client.post(START, json=body, headers=ORIGIN).status_code == 401
    csrf = login(client)
    fingerprint = rendered_binding(client)
    body["expected_binding"] = fingerprint
    assert client.post(START, json=body, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            START, json=body, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            START, json=body, headers={**headers, "X-Kodi-Lab-CSRF": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            START, json={"attempt": body["attempt"]}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            START, json={**body, "expected_binding": "invalid"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            START, json={**body, "mode": "interactive"}, headers=headers
        ).status_code
        == 422
    )
    assert not service(client).attachments


def test_unavailable_standalone_page_explains_recovery_without_enabling_viewing(
    standalone_client: TestClient,
    broker: FakeBroker,
) -> None:
    client = standalone_client
    login(client)
    runtime(client)["available"] = False
    page = client.get(PAGE)
    assert page.status_code == 200
    assert "screen is unavailable" in page.text
    assert "If it is stopped, use Start Kodi there" in page.text
    assert "private unavailable reason" not in page.text
    assert f"/instances/{INSTANCE_ID}#instance-controls-heading" in page.text
    assert "data-novnc-viewer" not in page.text and "novnc-phase1.js" not in page.text
    assert broker.calls == [] and not service(client).attachments


def test_standalone_page_respects_existing_read_capacity(
    standalone_client: TestClient,
) -> None:
    client = standalone_client
    login(client)
    viewer = service(client)
    assert viewer.checks.acquire(blocking=False)
    assert viewer.checks.acquire(blocking=False)
    try:
        page = client.get(PAGE)
        assert "screen is unavailable" in page.text
        assert "data-novnc-viewer" not in page.text
    finally:
        viewer.checks.release()
        viewer.checks.release()
    rendered_binding(client)


@pytest.mark.parametrize("change", ["container", "started", "identity"])
def test_standalone_control_handoff_cannot_follow_a_replacement(
    standalone_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    client = standalone_client
    login(client)
    viewer = service(client)
    binding = viewer.binding(INSTANCE_ID, "view-only")
    item = viewer.start(
        INSTANCE_ID,
        "c" * 64,
        StartView(
            attempt=view_attempt(),
            expected_binding=binding_fingerprint(INSTANCE_ID, binding),
        ),
    )
    assert viewer.ownership is not None
    original = viewer.ownership.wait_released

    def released(instance: Any) -> None:
        original(instance)
        if change == "container":
            successor = replace(binding, container="e" * 64)
        elif change == "started":
            successor = replace(binding, started="successor")
        else:
            successor = replace(binding, identity=(str(REVISION_ID), 1, None))
        monkeypatch.setattr(viewer, "authority", lambda _: successor)

    monkeypatch.setattr(viewer.ownership, "wait_released", released)
    with pytest.raises(ValueError, match="runtime changed"):
        viewer.handoff(
            INSTANCE_ID,
            "c" * 64,
            ChangeMode(attempt=item.attempt, next_attempt=view_attempt()),
            "interactive",
        )
    assert not viewer.attachments and viewer.registry.active_count() == 0


def test_export_standalone_browser_fixtures(standalone_client: TestClient) -> None:
    client = standalone_client
    login(client)
    ready = client.get(PAGE).text
    detail = client.get(f"/instances/{INSTANCE_ID}").text
    runtime(client)["available"] = False
    unavailable = client.get(PAGE).text
    if destination := os.getenv("KODI_LAB_STANDALONE_VIEWER_EVIDENCE"):
        Path(destination).joinpath("pages.json").write_text(
            json.dumps(
                {
                    "ready": ready,
                    "unavailable": unavailable,
                    "detail": detail,
                }
            )
        )

"""Private explicit baseline-copy workflow with durable original request status."""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceDevelopmentSourceRow,
    SessionSourceCopyAdmissionRow,
)
from tests.web.conftest import CONTAINER_ID, ORIGIN, FakeBroker, inventory_entry, login
from tests.web.test_session_preparation import reserve


def prepared(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    *,
    attached: bool = True,
) -> tuple[dict[str, Any], str, list[UUID]]:
    current = reserve(client, monkeypatch)
    with database.begin() as session:
        row = session.get(DevelopmentSessionRow, current["generation"])
        assert row is not None
        row.phase = "bound"
        row.container_id = CONTAINER_ID
        if attached:
            session.add(
                ManagedInstanceDevelopmentSourceRow(
                    managed_instance_id=current["target_id"],
                    development_source_id=current["source_id"],
                    attached_at="2026-10-25T10:00:00.000000Z",
                )
            )
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    submitted: list[UUID] = []
    context = cast(FastAPI, client.app).state.kodi_lab
    monkeypatch.setattr(
        context.development_source_deployment_executor, "submit", submitted.append
    )
    return current, login(client), submitted


def test_copy_routes_require_auth_and_same_origin_csrf_with_closed_input(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = str(uuid4())
    path = "/api/development-session/source-copy"
    assert client.post(path, json={}).status_code == 401
    assert client.get(f"{path}/{request}").status_code == 401
    assert (
        client.get(
            f"/development-session/source-copy/{request}", follow_redirects=False
        ).status_code
        == 303
    )
    current, csrf, submitted = prepared(client, database, broker, monkeypatch)
    body = {"request_id": request, "expected_generation": current["generation"]}
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path,
            json=body,
            headers={"Origin": "http://attacker.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    for extra in (
        {"target_id": current["target_id"]},
        {"destination": "/tmp/secret"},
        {"operation_id": str(uuid4())},
    ):
        assert (
            client.post(
                path,
                json={**body, **extra},
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            ).status_code
            == 422
        )
    for generation in (True, 0, -1, "1"):
        assert (
            client.post(
                path,
                json={**body, "expected_generation": generation},
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            ).status_code
            == 422
        )
    assert not submitted
    with database() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(SessionSourceCopyAdmissionRow)
            )
            == 0
        )


def test_api_admits_exact_receipt_and_gets_never_submit_or_probe(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, csrf, submitted = prepared(client, database, broker, monkeypatch)
    request = str(uuid4())
    path = "/api/development-session/source-copy"
    body = {"request_id": request, "expected_generation": current["generation"]}
    result = client.post(path, json=body, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf})
    assert result.status_code == 202, result.text
    assert result.headers["cache-control"] == "no-store"
    receipt = result.json()["receipt"]
    assert (
        receipt["source_id"] == current["source_id"]
        and receipt["container_id"] == CONTAINER_ID
    )
    assert submitted == [UUID(receipt["operation_id"])]

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("GET cannot probe runtime or submit a copy")

    monkeypatch.setattr(broker, "managed_container_inventory", forbidden)
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        command = sql.lstrip().split()[0].upper()
        statements.append(command)
        assert command in {"SELECT", "BEGIN", "PRAGMA"} or (
            command == "UPDATE"
            and sql.lstrip().upper().startswith("UPDATE WEB_SESSIONS ")
        )

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        status = client.get(f"{path}/{request}")
        page = client.get(f"/development-session/source-copy/{request}")
        checklist = client.get("/development-session/preparation")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert status.status_code == page.status_code == checklist.status_code == 200
    assert status.json() == receipt and statements
    assert (
        status.headers["cache-control"] == page.headers["cache-control"] == "no-store"
    )
    assert 'name="request_id" value="' + request + '"' in page.text
    assert (
        "Continue this queued copy" in page.text
        and "This page does not check Kodi" in page.text
    )
    assert f'href="/development-session/source-copy/{request}"' in checklist.text
    assert "Review the existing copy request" in checklist.text
    assert '<form action="/development-session/source-copy"' not in checklist.text
    assert "private workspace edits" in checklist.text
    assert len(submitted) == 1
    assert client.get(f"{path}/{uuid4()}").status_code == 404


def test_html_form_uses_same_nonce_and_generation_and_checks_origin_csrf(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, csrf, submitted = prepared(client, database, broker, monkeypatch)
    form = {
        "csrf_token": csrf,
        "request_id": str(uuid4()),
        "expected_generation": str(current["generation"]),
    }
    path = "/development-session/source-copy"
    assert client.post(path, data=form, follow_redirects=False).status_code == 403
    assert (
        client.post(
            path, data={**form, "csrf_token": "wrong"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            path, data={**form, "target_id": current["target_id"]}, headers=ORIGIN
        ).status_code
        == 422
    )
    first = client.post(path, data=form, headers=ORIGIN, follow_redirects=False)
    assert first.status_code == 303, first.text
    assert first.headers["location"] == f"{path}/{form['request_id']}"
    assert first.headers["cache-control"] == "no-store"
    replay = client.post(path, data=form, headers=ORIGIN, follow_redirects=False)
    assert replay.status_code == 303 and submitted[0] == submitted[1]
    assert len(submitted) == 2
    with database() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(SessionSourceCopyAdmissionRow)
            )
            == 1
        )


def test_missing_attachment_shows_setup_and_never_silently_attaches(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, csrf, submitted = prepared(
        client, database, broker, monkeypatch, attached=False
    )
    page = client.get("/development-session/preparation")
    assert (
        page.status_code == 200
        and "Attach this registered source through Kodi setup" in page.text
    )
    assert '<form action="/development-session/source-copy"' not in page.text
    result = client.post(
        "/api/development-session/source-copy",
        json={"request_id": str(uuid4()), "expected_generation": 1},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert result.status_code == 409 and not submitted
    assert result.json()["error"]["code"] == "development_source_deployment_unsupported"
    with database() as session:
        assert (
            session.get(
                ManagedInstanceDevelopmentSourceRow,
                (current["target_id"], current["source_id"]),
            )
            is None
        )
        assert (
            session.scalar(
                select(func.count()).select_from(SessionSourceCopyAdmissionRow)
            )
            == 0
        )


def test_stale_form_refuses_before_any_new_work(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _current, csrf, submitted = prepared(client, database, broker, monkeypatch)
    result = client.post(
        "/api/development-session/source-copy",
        json={"request_id": str(uuid4()), "expected_generation": 2},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert (
        result.status_code == 409
        and result.json()["error"]["code"] == "development_session_generation_conflict"
    )
    assert not submitted


@pytest.mark.parametrize(
    "outcome", ["running", "failed", "reconciliation_required", "not_retained"]
)
def test_status_and_replay_do_not_restart_nonqueued_or_pruned_work(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from kodi_lab.storage.repositories import (
        DevelopmentSourceDeploymentOperationRepository,
    )

    current, csrf, submitted = prepared(client, database, broker, monkeypatch)
    context = cast(FastAPI, client.app).state.kodi_lab
    request = str(uuid4())
    body = {"request_id": request, "expected_generation": 1}
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    path = "/api/development-session/source-copy"
    admitted = client.post(path, json=body, headers=headers)
    assert admitted.status_code == 202
    operation_id = UUID(admitted.json()["receipt"]["operation_id"])
    coordinator = context.development_source_deployment_operations
    lease = coordinator.acquire(operation_id)
    assert lease is not None
    if outcome != "running":
        coordinator.finish_failure(
            lease,
            reason="fixture failure",
            reconciliation_required=outcome == "reconciliation_required",
        )
    if outcome == "not_retained":
        with database.begin() as session:
            assert (
                DevelopmentSourceDeploymentOperationRepository(session).prune_terminal(
                    UUID(current["target_id"]), retain=0
                )
                == 1
            )
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 202 and replay.json()["submission"] == "not_requested"
    assert submitted == [operation_id]
    page = client.get(f"/development-session/source-copy/{request}")
    assert page.status_code == 200 and "Continue this queued copy" not in page.text
    if outcome == "not_retained":
        assert "copy outcome is unknown here" in page.text
        assert replay.json()["receipt"]["detail"] == "not_retained"
    checklist = client.get("/development-session/preparation")
    assert f'href="/development-session/source-copy/{request}"' in checklist.text
    if outcome in {"running", "reconciliation_required"}:
        assert '<form action="/development-session/source-copy"' not in checklist.text

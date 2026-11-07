"""Authenticated read-only component preview/API and browser refresh behavior."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_kodi_prerequisites import DataStorageObservation
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.storage.models import (
    ManagedResourcePolicyRow,
    RuntimeRecipeInstallationRow,
)
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_runtime_installations import Flow, seed_installation

API = "/api/create-kodi/prerequisites"
PREVIEW = "/preview/create-kodi-prerequisites"


def test_higher_recorded_limit_is_supported_without_changing_admin_policy(
    client: TestClient, broker: FakeBroker, database: sessionmaker[Session]
) -> None:
    Flow().attach(broker)
    csrf = login(client)
    with database.begin() as session:
        session.add(ManagedResourcePolicyRow(id=1, max_running=4, idle_minutes=None))
    for _ in range(2):
        checks = {item["key"]: item for item in client.get(API).json()["checks"]}
        assert checks["running_policy"]["status"] == "passed"
        assert checks["running_policy"]["detail_url"] == "/managed-resources"
        assert (
            "Up to 1 managed Kodi instances may run at once."
            in checks["running_policy"]["message"]
        )
    assert "Managed Kodi running limit" in client.get(PREVIEW).text
    with database() as session:
        row = session.get(ManagedResourcePolicyRow, 1)
        assert row and (row.max_running, row.idle_minutes) == (4, None)
    assert (
        client.post(
            "/api/managed-resources/policy",
            json={"max_running": 1, "idle_minutes": None},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 200
    )
    checks = {item["key"]: item for item in client.get(API).json()["checks"]}
    assert checks["running_policy"]["status"] == "passed"
    assert not any(name in {"start", "stop", "provision"} for name, _ in broker.calls)


def test_prerequisites_require_authentication_and_accept_only_reads(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    assert client.get(API).status_code == 401
    assert client.get(PREVIEW, follow_redirects=False).status_code == 401
    Flow().attach(broker)
    token = login(client)
    assert client.get(API).status_code == 200  # GET requires no CSRF token.
    for headers in ({}, ORIGIN, {**ORIGIN, "X-Kodi-Lab-CSRF": token}):
        assert client.post(API, headers=headers).status_code == 405
        assert client.post(PREVIEW, headers=headers).status_code == 405
    assert client.get(API, headers={"Host": "evil.invalid"}).status_code == 400
    for generation in ("matrix", "nexus", "omega", "piers"):
        assert client.get(API, params={"generation": generation}).status_code == 200
        assert client.get(PREVIEW, params={"generation": generation}).status_code == 200
    for generation in ("unknown", "../../etc/passwd"):
        assert client.get(API, params={"generation": generation}).status_code == 422
        assert client.get(PREVIEW, params={"generation": generation}).status_code == 422


def test_preview_is_embeddable_without_permanent_navigation_or_operation_controls(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    Flow().attach(broker)
    login(client)
    response = client.get(PREVIEW)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'data-kodi-prerequisites data-generation="omega"' in response.text
    assert "Check again" in response.text
    assert 'role="status" aria-live="polite"' in response.text
    assert 'data-prerequisite-error role="alert" hidden' in response.text
    assert "informational snapshots" in response.text
    assert "kodi-prerequisites.js" in response.text
    assert 'href="/preview/create-kodi-prerequisites' not in response.text
    component = response.text.split("data-kodi-prerequisites", 1)[1].split(
        "</section>", 1
    )[0]
    assert "<form" not in component
    assert "data-install-action" not in component
    assert "Current Kodi" not in component
    assert "data-instance-action" not in component


def test_refresh_observes_active_and_existing_setup_without_reconciliation(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = Flow()
    flow.attach(broker)
    login(client)
    operation = seed_installation(database, "omega")
    with database.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None
        row.state = RuntimeRecipeInstallationState.PENDING
        row.failure_code = None
        row.completed_at = None
    flow.operation_id, flow.generation = str(operation), "omega"
    monkeypatch.setattr(
        "kodi_lab.application.runtime_installations.recipe_target_id",
        lambda _recipe: INSTANCE_ID,
    )
    for name in ("_reconcile", "list", "get", "start"):
        monkeypatch.setattr(
            RuntimeInstallationService,
            name,
            lambda *_args, **_kwargs: pytest.fail("stateful installer called"),
        )
    for name in ("get", "get_observation", "list_statuses"):
        monkeypatch.setattr(
            ManagedRuntimeStatusService,
            name,
            lambda *_args, **_kwargs: pytest.fail("stateful runtime status called"),
        )
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            response = client.get(API)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            payload = response.json()
            observed = {item["key"]: item for item in payload["checks"]}
            assert observed["runtime"]["status"] == "existing"
            assert observed["activity"]["status"] == "existing"
            assert observed["instance"]["detail_url"] == f"/instances/{INSTANCE_ID}"
            assert all("action" not in item for item in payload["checks"])
        flow.operation_id, flow.generation = None, None
        assert (
            next(
                item
                for item in client.get(API).json()["checks"]
                if item["key"] == "runtime"
            )["status"]
            == "attention"
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not [
        sql
        for sql in statements
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", sql, re.I)
    ]
    with database() as session:
        saved = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert (
            saved is not None and saved.state is RuntimeRecipeInstallationState.PENDING
        )
    assert flow.launches == [] and broker.calls == [] and broker.screenshot_calls == 0


@pytest.mark.parametrize("measurement", ["small", "unknown", "read-only"])
def test_snapshot_reports_measurements_without_asserting_host_readiness(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    measurement: str,
) -> None:
    Flow().attach(broker)
    login(client)

    def storage(_root: Path) -> DataStorageObservation:
        if measurement == "unknown":
            raise OSError("/private/disk/path")
        return DataStorageObservation(1, measurement == "read-only")

    monkeypatch.setattr(
        "kodi_lab.application.managed_kodi_prerequisites.read_data_storage", storage
    )
    response = client.get(API, params={"generation": "nexus"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["generation"] == "nexus" and payload["version"] == "20.5"
    observed = {item["key"]: item for item in payload["checks"]}
    assert (
        observed["data_storage"]["status"]
        == {"small": "info", "unknown": "unknown", "read-only": "attention"}[
            measurement
        ]
    )
    for key in ("docker_storage", "host_storage", "memory"):
        assert observed[key]["status"] == "unknown"
    assert "/private/disk/path" not in response.text
    assert "can_install" not in response.text


def test_component_refresh_browser_behavior() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("kodi_prerequisites_browser.cjs")),
            str(root / "src/kodi_lab/web/static/kodi-prerequisites.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_export_isolated_browser_fixtures(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optionally retain mock-only HTML/JSON for a focused Chromium review."""
    Flow().attach(broker)
    login(client)
    monkeypatch.setattr(
        "kodi_lab.application.managed_kodi_prerequisites.read_data_storage",
        lambda _root: DataStorageObservation(12 * 1024**3, False),
    )
    html = client.get(PREVIEW).text
    payload = client.get(API).json()
    assert 'data-status="unknown"' in html
    destination = os.environ.get("KODI_LAB_PREREQUISITE_BROWSER_FIXTURES")
    if destination:
        Path(destination).write_text(json.dumps({"html": html, "payload": payload}))

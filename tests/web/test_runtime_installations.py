"""Authenticated three-generation runtime installation page and HTTP contract."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    REQUIRED_OPERATIONS,
    RuntimeInstallationService,
)
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

from .conftest import IMAGE_ID, INSTANCE_ID, ORIGIN, REVISION_ID, FakeBroker, login


class Flow:
    def __init__(self) -> None:
        self.operation_id: str | None = None
        self.generation: str | None = None
        self.present = True
        self.launches: list[tuple[str, str]] = []

    def attach(self, broker: FakeBroker) -> None:
        dynamic: Any = broker
        dynamic.capabilities = lambda: SimpleNamespace(
            operations=list(REQUIRED_OPERATIONS)
        )
        dynamic.active_reviewed_runtime_installation = lambda: SimpleNamespace(
            operation_id=self.operation_id, generation=self.generation, lock_held=False
        )
        dynamic.observe_reviewed_runtime_installation = lambda _id: SimpleNamespace(
            state="running", lock_held=False
        )
        dynamic.reviewed_runtime_image_present = lambda _id: SimpleNamespace(
            present=self.present
        )

        def launch(*, generation: str, operation_id: str) -> SimpleNamespace:
            self.operation_id = operation_id
            self.generation = generation
            self.launches.append((generation, operation_id))
            return SimpleNamespace(operation_id=operation_id, newly_accepted=True)

        dynamic.launch_reviewed_runtime_installation = launch


def seed_installation(
    factory: sessionmaker[Session], generation: str, *, ready: bool = False
) -> UUID:
    reviewed = load_reviewed_recipe(generation)
    operation_id = uuid4()
    now = datetime.now(UTC)
    with factory.begin() as session:
        row = RuntimeRecipeInstallationRepository(session).create(
            operation_id=operation_id,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation=generation,
            now=now,
        )
        if ready:
            row.state = RuntimeRecipeInstallationState.READY
            row.image_id = IMAGE_ID
            row.runtime_revision_id = str(REVISION_ID)
            row.attestation_json = "{}"
            row.validation_json = "{}"
            row.completed_at = row.updated_at
            session.flush()
        else:
            RuntimeRecipeInstallationRepository(session).fail(
                operation_id, code="installation_failed", now=now
            )
    return operation_id


def test_remote_only_page_has_three_unavailable_choices(
    client: TestClient,
) -> None:
    assert client.get("/api/runtime-installations").status_code == 401
    login(client)
    response = client.get("/runtimes")
    primary = response.text.split('class="runtime-matrix-details"')[0]
    for title, version in (
        ("Kodi 19 — Matrix", "19.4"),
        ("Kodi 20 — Nexus", "20.5"),
        ("Kodi 21 — Omega", "21.3"),
    ):
        assert title in primary and version in primary
    assert primary.count("Installation unavailable") == 3
    assert "data-install-action" not in primary
    assert "Piers" not in primary
    assert "several GB of Docker storage" in primary
    assert "Each step requires a separate action" in primary
    assert 'data-instance-action="start"' not in primary


def test_install_api_auth_origin_csrf_body_and_polling(
    client: TestClient,
    broker: FakeBroker,
) -> None:
    flow = Flow()
    flow.attach(broker)
    url = "/api/runtime-installations/matrix"
    assert client.post(url).status_code == 401
    token = login(client)
    initial = client.get("/runtimes").text.split('class="runtime-matrix-details"')[0]
    assert initial.count('data-install-action="install"') == 3
    assert "Piers" not in initial
    listed = client.get("/api/runtime-installations").json()["installations"]
    assert [(item["generation"], item["version"]) for item in listed] == [
        ("matrix", "19.4"),
        ("nexus", "20.5"),
        ("omega", "21.3"),
    ]
    assert client.post(url, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    assert (
        client.post(
            url, headers={**headers, "Origin": "http://evil.invalid"}
        ).status_code
        == 403
    )
    assert client.post(url, headers=headers, content=b"{}").status_code == 400
    assert client.post(url, headers=headers, content=b" ").status_code == 400
    assert (
        client.post("/api/runtime-installations/piers", headers=headers).status_code
        == 404
    )
    accepted = client.post(url, headers=headers)
    assert accepted.status_code == 202
    operation_id = accepted.json()["operation_id"]
    assert accepted.json()["status_url"] == f"/api/runtime-installations/{operation_id}"
    assert accepted.json()["newly_accepted"] is True
    assert (
        client.get(f"/api/runtime-installations/{operation_id}").json()["phase"]
        == "Preparing"
    )
    assert client.get("/runtimes").text.count("Preparing") >= 1
    duplicate = client.post(url, headers=headers)
    assert duplicate.status_code == 202
    assert duplicate.json()["operation_id"] == operation_id
    assert duplicate.json()["newly_accepted"] is False
    assert (
        client.post("/api/runtime-installations/nexus", headers=headers).status_code
        == 409
    )
    assert len(flow.launches) == 1
    assert not any(name == "start_managed_container" for name, _ in broker.calls)


def test_failed_retry_missing_reacquire_and_managed_link(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = Flow()
    flow.attach(broker)
    monkeypatch.setattr(
        RuntimeInstallationService, "_eligible_ready", lambda _self, _row: True
    )
    token = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    failed_id = seed_installation(database, "matrix")
    failed = client.get(f"/api/runtime-installations/{failed_id}").json()
    assert failed["status"] == "failed"
    assert failed["action"] == "retry"
    assert "Check network access" in failed["failure_message"]
    retried = client.post(
        "/api/runtime-installations/matrix?action=retry", headers=headers
    )
    assert retried.status_code == 202
    flow.operation_id = None
    flow.generation = None
    ready_id = seed_installation(database, "nexus", ready=True)
    incomplete = client.get(f"/api/runtime-installations/{ready_id}").json()
    assert incomplete["status"] == "setup_incomplete"
    assert incomplete["action"] == "none"
    assert "rerun the reviewed runtime installer" in incomplete["note"]
    repair = client.post(
        "/api/runtime-installations/nexus?action=retry", headers=headers
    )
    assert repair.status_code == 409
    flow.operation_id = None
    flow.generation = None
    flow.present = False
    missing = client.get(f"/api/runtime-installations/{ready_id}").json()
    assert missing["status"] == "missing" and missing["action"] == "reacquire"
    reacquired = client.post(
        "/api/runtime-installations/nexus?action=reacquire", headers=headers
    )
    assert reacquired.status_code == 202
    with database() as session:
        row = RuntimeRecipeInstallationRepository(session).get(ready_id)
        assert row is not None and row.runtime_revision_id == str(REVISION_ID)
    flow.operation_id = None
    flow.generation = None
    flow.present = True
    # Existing canonical managed-instance identity is resolved to a page link.
    monkeypatch.setattr(
        "kodi_lab.application.runtime_installations.recipe_target_id",
        lambda _reviewed: INSTANCE_ID,
    )
    linked = client.get(f"/api/runtime-installations/{ready_id}").json()
    assert linked["action"] == "open"
    assert linked["managed_instance_url"] == f"/instances/{INSTANCE_ID}"
    assert "Open managed instance" in client.get("/runtimes").text


def test_unverified_ready_row_never_becomes_a_ready_install_action(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    Flow().attach(broker)
    login(client)
    operation_id = seed_installation(database, "omega", ready=True)
    status = client.get(f"/api/runtime-installations/{operation_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "unavailable"
    assert status.json()["action"] == "none"

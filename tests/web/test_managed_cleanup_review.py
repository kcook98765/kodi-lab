"""Cleanup review uses managed metadata and retains existing explicit approvals."""

from __future__ import annotations

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mutations import ManagedInstanceMutationCoordinator
from kodi_lab.domain import ManagedInstanceMutationKind
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login


def review(page: str) -> str:
    return page.split('<section id="cleanup-review"', 1)[1].split("</section>", 1)[0]


def test_cleanup_review_retains_data_consequences_and_confirmation_routes(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/provision", headers=headers, json={}
        ).status_code
        == 200
    )
    before = list(broker.calls)
    response = client.get("/managed-resources")
    assert response.status_code == 200
    panel = review(response.text)
    assert f'href="/instances/{INSTANCE_ID}/delete"' in panel
    assert f'href="/instances/{INSTANCE_ID}#instance-controls-heading"' in panel
    assert "0 snapshot(s), 0 known archive bytes" in panel
    assert "cannot be restored in v1" in panel
    assert "writable container data" in panel
    assert "durable history" in panel
    assert 'href="/agent-workspaces"' in panel
    assert 'href="/snapshots/preserved"' in panel
    assert "disk usage are unavailable" in panel
    assert "<button" not in panel and "<form" not in panel
    assert not [
        call
        for call in broker.calls[len(before) :]
        if call[0] in {"stop", "start", "deprovision", "provision"}
    ]


def test_unknown_cleanup_state_cannot_offer_removal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    service = cast(FastAPI, client.app).state.kodi_lab.status
    unknown = service.list_statuses()[0].model_copy(
        update={"container_state": "unknown", "allowed_actions": []}
    )
    monkeypatch.setattr(service, "list_statuses", lambda: [unknown])
    panel = review(client.get("/managed-resources").text)
    assert "resolve unknown or active operations" in panel
    assert f'href="/instances/{INSTANCE_ID}/delete"' not in panel
    assert "Review runtime removal" not in panel


def test_active_operation_blocks_cleanup_review(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    login(client)
    coordinator = ManagedInstanceMutationCoordinator(database)
    lease = coordinator.acquire(INSTANCE_ID, ManagedInstanceMutationKind.LIFECYCLE)
    try:
        panel = review(client.get("/managed-resources").text)
        assert "Operation in progress — cleanup blocked" in panel
        assert "Review runtime removal" not in panel
        assert f'href="/instances/{INSTANCE_ID}/delete"' not in panel
    finally:
        coordinator.release(lease)

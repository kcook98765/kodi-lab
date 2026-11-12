"""Real presentation fixtures use disposable lifecycle services and a fake broker."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote_coordination import (
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import ManagedInstanceUseRow
from tests.application.test_managed_start import NativeReady
from tests.managed_versions_support import Broker
from tests.web.test_managed_slots import act, setup, slot, wait
from tests.web.test_managed_versions_mcp import broker as broker


def test_export_managed_card_review_states(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = setup(client, database)
    cast(FastAPI, client.app).state.managed_slots.screen_supported = lambda _target: (
        True
    )
    cases: dict[str, object] = {}

    def capture(name: str) -> None:
        response = client.get("/managed-kodi")
        assert response.status_code == 200
        status = client.get("/api/managed-kodi/slots").json()
        assert "Ready to view:" in response.text
        assert "grant Trusted Managed Lab once" in response.text
        assert "agent GUI input is blocked" in response.text
        for item in status["slots"]:
            card = response.text.split(f'data-slot="{item["version"]}"', 1)[1]
            screen = card.split("data-slot-screen", 1)[1].split(">", 1)[0]
            assert (" hidden" not in screen) == bool(item["screen_url"])
            if item["screen_url"]:
                assert f'href="{item["screen_url"]}"' in screen
        receipts = {}
        for item in status["slots"]:
            if item["pending_request"]:
                path = "/api/managed-kodi/requests/" + item["pending_request"]
                receipt = client.get(path)
                assert receipt.status_code == 200
                receipts[path] = receipt.json()
        cases[name] = {
            "html": response.text,
            "status": status,
            "receipts": receipts,
            "overview": client.get("/kodi").text,
        }

    capture("installation-and-preparation")
    readiness = Event()

    def finish_readiness(*_args: object, **_kwargs: object) -> object:
        assert readiness.wait(10), "Synthetic readiness was not released"
        return NativeReady().wait_until_usable()

    monkeypatch.setattr(
        cast(FastAPI, client.app).state.kodi_lab.managed_versions._lifecycle,
        "_coordinator_factory",
        lambda *_: SimpleNamespace(wait_until_usable=finish_readiness),
    )
    try:
        assert act(client, csrf, "start").json()["phase"] == "complete"
        assert slot(client)["state"] == "starting"
        capture("starting")
    finally:
        readiness.set()
    wait(client)
    assert slot(client)["screen_url"]
    capture("running")
    cast(FastAPI, client.app).state.managed_slots.screen_supported = lambda _: False
    assert slot(client)["native"] == "Ready" and not slot(client)["screen_url"]
    capture("running-without-viewer")
    cast(FastAPI, client.app).state.managed_slots.screen_supported = lambda _: True
    handle = acquire_browser_remote(
        database, UUID(broker.records[0].managed_instance_id)
    )
    try:
        assert not slot(client)["stop"]
        capture("protected-running")
    finally:
        release_browser_remote(handle)
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    assert slot(client)["state"] == "stopped"
    capture("retained-stopped")
    context = cast(FastAPI, client.app).state.kodi_lab
    actor = f"mcp:{uuid4()}"
    target = broker.records[0].managed_instance_id
    with database.begin() as session:
        session.add(
            ManagedInstanceUseRow(
                managed_instance_id=target,
                actor_key=actor,
                last_activity_at=timestamp(context.managed_versions._clock()),
                hold_until=timestamp(
                    context.managed_versions._clock() + timedelta(minutes=15)
                ),
            )
        )
    historical = slot(client)
    assert historical["start"] and not historical["reset"]
    assert not historical["needs_review"]
    capture("retained-stopped-history")
    with database.begin() as session:
        use = session.get(ManagedInstanceUseRow, (target, actor))
        assert use is not None
        session.delete(use)
    assert act(client, csrf, "reset").json()["phase"] == "complete"
    assert slot(client)["reset_note"]
    capture("reset-completed")
    broker.fault = "lost_remove_response"
    result = act(client, csrf, "reset")
    assert result.status_code == 200 and result.json()["phase"] == "uncertain"
    assert broker.removals == 2
    capture("uncertain-reset")
    # Capturing/polling the original operation never repeats disposal or creation.
    assert broker.removals == 2 and broker.provisions == 2
    if destination := os.getenv("KODI_MANAGED_CONTROLS_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "cases.json").write_text(json.dumps(cases))


def test_plain_guidance_keeps_ordinary_setup_and_development_separate(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    missing, installed = slot(client, "matrix"), slot(client)
    assert missing["state_label"] == "Not set up"
    assert "Create Managed Kodi" in missing["message"]
    assert installed["state"] == "stopped"
    assert "saved settings and add-ons" in installed["message"]
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert slot(client)["message"] == "Kodi is running."
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    retained = slot(client)
    assert retained["state"] == "stopped"
    assert "saved settings and add-ons" in retained["message"]
    assert "Reset" not in retained["message"]
    page = client.get("/managed-kodi").text
    assert 'href="/development-session">Legacy Current Managed Kodi' in page
    assert "The agent manages its projects and tests installed versions" in page

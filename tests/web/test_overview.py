"""Overview reads recorded state; operations belong to the managed page."""

import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import __version__
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgeReachabilityState,
    ExternalObservationState,
    ExternalVerificationState,
    HealthStatus,
    RuntimeRecipeInstallationState,
)
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)
from kodi_lab.web.overview import OverviewSummary
from tests.managed_versions_support import Broker
from tests.web.conftest import EXTERNAL_TARGET_ID, ORIGIN, login, seed_external_target
from tests.web.test_kodi_views import household as household
from tests.web.test_managed_slots import act, setup, wait
from tests.web.test_managed_versions_mcp import broker as broker


def summary(client: TestClient) -> OverviewSummary:
    response = client.get("/kodi")
    assert response.status_code == 200
    return cast(OverviewSummary, cast(Any, response).context["overview"])


def export(client: TestClient, name: str) -> None:
    if destination := os.getenv("KODI_OVERVIEW_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{name}.html").write_text(client.get("/kodi").text)


def test_empty_overview_and_managed_pages_have_distinct_ownership(
    client: TestClient,
) -> None:
    login(client)
    export(client, "empty")
    overview = client.get("/kodi").text
    main = overview.split("<main ", 1)[1]
    assert "<h1>Overview</h1>" in main and __version__ in main
    assert "Kodi Lab Web is available" in main
    assert "recorded information" in main
    assert "Versions prepared: <strong>0 of 4</strong>" in main
    assert "Last recorded running: <strong>None</strong>" in main
    assert "Versions needing attention: <strong>0</strong>" in main
    assert "Production devices: <strong>0</strong>" in main
    assert "Development devices: <strong>0</strong>" in main
    assert "Registered clients: <strong>0</strong>" in main
    assert "isolated Kodi installation for testing" in main
    assert "explicit approval for each device" in main
    assert main.count('class="kodi-card"') == 3
    for label, destination in (("Managed Kodi", "/managed-kodi"),):
        assert main.count(f'href="{destination}">Set up {label}</a>') == 1
        assert f">Open {label}</a>" not in main.split("<noscript>", 1)[0]
    assert 'href="/external-kodi">Open External Kodi</a>' in main
    for forbidden in (
        "<form",
        "<button",
        "data-slot-action",
        "managed-slots.js",
        "/add-kodi",
        "Current Managed Kodi",
    ):
        assert forbidden not in main
    managed = client.get("/managed-kodi").text
    assert "<h1>Managed Kodi</h1>" in managed
    assert managed.count('class="managed-slot"') == 4
    assert managed.count("managed-slots.js") == 1
    assert "data-slot-confirm" in managed
    for forbidden in (
        'id="household-kodi-heading"',
        "/add-kodi",
        "data-target-id=",
        "data-create-client",
    ):
        assert forbidden not in managed.split("<main ", 1)[1]


def test_recorded_managed_counts_follow_existing_start_stop_without_probes(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = setup(client, database)
    assert summary(client).prepared == 1
    assert ">Open Managed Kodi</a>" in client.get("/kodi").text
    assert ">Set up Managed Kodi</a>" not in client.get("/kodi").text
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert summary(client).running == ("Omega",)
    context = cast(FastAPI, client.app).state.kodi_lab

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Overview tried to observe live readiness")

    with monkeypatch.context() as patch:
        patch.setattr(context.managed_versions, "status", forbidden)
        patch.setattr(context.status, "get_observation", forbidden)
        patch.setattr(
            "kodi_lab.web.current_kodi.CurrentKodiPresentation.identity", forbidden
        )
        before = list(broker.calls)
        assert summary(client).running == ("Omega",)
        assert broker.calls == before
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    assert summary(client).running == ()
    assert summary(client).managed_attention == 0
    # Recorded failure counts a version once, even with an unresolved reset.
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    omega = recipe_target_id(load_installation_recipe("omega"))
    with database.begin() as session:
        target = session.get(TargetRow, str(omega))
        assert target is not None
        target.health = HealthStatus.WARNING
    assert summary(client).managed_attention == 1
    assert (
        "Some Kodi versions or connections need attention" in client.get("/kodi").text
    )
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    broker.fault = "lost_remove_response"
    assert act(client, csrf, "reset").json()["phase"] == "uncertain"
    assert summary(client).managed_attention == 1


@pytest.mark.parametrize(
    ("observation", "verification", "admin", "bucket"),
    [
        ("reachable", "verified", "enabled", "connected"),
        ("unreachable", "verified", "enabled", "disconnected"),
        (None, "verified", "enabled", "unknown"),
        ("authentication_failed", "verified", "enabled", "attention"),
        ("reachable", "identity_mismatch", "enabled", "attention"),
        ("reachable", "verified", "disabled", "disconnected"),
        ("reachable", "identity_mismatch", "disabled", "attention"),
        ("reachable", "unverified", "disabled", "attention"),
    ],
)
def test_external_counts_stay_separate_and_recorded(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    observation: str | None,
    verification: str,
    admin: str,
    bucket: str,
) -> None:
    seed_external_target(database)
    with database.begin() as session:
        dev = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
        assert dev is not None
        dev.observation_state = (
            ExternalObservationState(observation) if observation else None
        )
        dev.verification_state = ExternalVerificationState(verification)
        dev.administrative_state = ExternalAdministrativeState(admin)
    login(client)
    value = summary(client)
    assert value.production.total == 1 and value.production.connected == 1
    assert value.development.total == 1
    export(client, "development-" + bucket)
    assert getattr(value.development, bucket) == 1
    overview = client.get("/kodi").text
    managed = client.get("/managed-kodi").text
    for html in (overview, managed):
        assert "Living room" not in html
        assert str(household) not in html
        assert str(EXTERNAL_TARGET_ID) not in html
    assert 'href="/external-kodi"' in overview
    assert 'data-slot="omega"' not in overview
    external = client.get("/external-kodi").text
    assert 'href="/production-targets"' in external
    assert 'href="/external-targets"' in external
    for label in ("External Production Kodi", "External Development Kodi"):
        assert f">Open {label}</a>" in external
        assert f">Set up {label}</a>" not in external


def test_agent_counts_and_production_attention(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
) -> None:
    csrf = login(client)
    presets = client.get("/api/mcp/presets").json()

    created = client.post(
        "/api/mcp/clients",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={"display_name": "Dashboard fixture", "preset_id": presets[0]["id"]},
    )
    assert created.status_code == 201
    with database.begin() as session:
        bridge = session.get(ExternalBridgePairingRow, str(household))
        assert bridge is not None

        bridge.reachability_state = ExternalBridgeReachabilityState.IDENTITY_MISMATCH
    value = summary(client)
    assert value.clients == value.enabled_clients == 1
    assert value.production.attention == 1 and value.production.connected == 0
    export(client, "production-attention")
    cast(FastAPI, client.app).state.kodi_lab.mcp_administration.set_client_enabled(
        UUID(created.json()["id"]), False
    )
    assert summary(client).clients == 1 and summary(client).enabled_clients == 0


@pytest.mark.parametrize("reachability", ["unreachable", "unknown"])
def test_production_disconnected_unknown_and_retired_counts(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    reachability: str,
) -> None:
    login(client)
    with database.begin() as session:
        bridge = session.get(ExternalBridgePairingRow, str(household))
        assert bridge is not None
        bridge.reachability_state = ExternalBridgeReachabilityState(reachability)
    value = summary(client)
    assert value.production.total == 1
    assert (
        getattr(
            value.production,
            "disconnected" if reachability == "unreachable" else "unknown",
        )
        == 1
    )
    export(client, "production-" + reachability)
    context = cast(FastAPI, client.app).state.kodi_lab
    context.production_targets.deactivate(household)
    assert summary(client).production.total == 0
    seed_external_target(database)
    context.external_targets.deactivate(EXTERNAL_TARGET_ID)
    assert summary(client).development.total == 0


def test_four_prepared_versions_and_failed_installation_recovery(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.web.test_runtime_installations import seed_installation

    # Stored installation fixtures only; no preview acquisition or validation.
    monkeypatch.setattr(
        "tests.web.test_runtime_installations.load_reviewed_recipe",
        load_installation_recipe,
    )
    login(client)
    for generation in ("matrix", "nexus", "omega", "piers"):
        seed_installation(database, generation, ready=True)
    assert summary(client).prepared == 4
    seed_installation(database, "matrix", ready=False)
    assert summary(client).prepared == 3
    assert summary(client).managed_attention == 1
    export(client, "installation-attention")
    seed_installation(database, "matrix", ready=True)
    assert summary(client).prepared == 4
    assert summary(client).managed_attention == 0


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_incomplete_managed_installation_keeps_recorded_summary(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from tests.web.test_runtime_installations import seed_installation

    monkeypatch.setattr(
        "tests.web.test_runtime_installations.load_reviewed_recipe",
        load_installation_recipe,
    )
    operation = seed_installation(database, "omega")
    with database.begin() as session:
        installation = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert installation is not None
        installation.state = RuntimeRecipeInstallationState(state)
        if state == "pending":
            installation.completed_at = installation.failure_code = None
    login(client)
    assert summary(client).managed_setup_started
    page = client.get("/kodi").text
    assert ">Open Managed Kodi</a>" in page
    assert ">Set up Managed Kodi</a>" not in page
    assert "Versions prepared: <strong>0 of 4</strong>" in page
    export(client, "managed-" + state)

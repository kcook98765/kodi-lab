"""Managed setup reviews canonical identity and retains separate safe stages."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE
from tests.web.conftest import FakeBroker, login
from tests.web.test_runtime_installations import Flow, seed_installation


def test_guided_setup_uses_recipe_names_without_mutation(
    client: TestClient, broker: FakeBroker
) -> None:
    flow = Flow()
    flow.attach(broker)
    login(client)
    page = client.get("/runtimes").text
    assert '<h2 id="managed-setup-heading">Install Kodi files</h2>' in page
    assert "Guided managed Kodi setup" not in page
    assert "Names for this installation are fixed by the reviewed recipe" in page
    assert 'href="/managed-kodi">Managed Kodi</a>' in page
    assert "everyday Start, Stop and View Screen controls" in page
    assert 'href="/development">Development Tools</a>' in page
    assert 'href="/development-session">Current Managed Kodi</a>' in page
    assert "stopped managed profile available" in page
    assert "It does not start Kodi or authorize an agent" in page
    assert 'href="/mcp-clients">Agents</a>' in page
    assert "grant Trusted Managed Lab" in page
    assert "without Web workspace selection" in page
    assert "Each step requires a separate action" in page
    assert "data-install-review" in page and "data-install-approved" in page
    assert "No operation recorded" in page
    listed = client.get("/api/runtime-installations").json()["installations"]
    for item in listed:
        if item["generation"] == "piers":
            expected = "Kodi 22 Piers RC2"
        else:
            reviewed = load_reviewed_recipe(item["generation"])
            expected = f"{reviewed.recipe.display_generation} Lab"
        assert item["managed_instance_name"] == expected
        assert f'data-instance-name="{expected}"' in page
    assert not flow.launches
    assert "Kodi 22 Piers RC2" in page
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    preview = client.get("/runtimes").text
    assert 'data-instance-name="Kodi 22 Piers RC2"' in preview
    assert "data-preview-acknowledgement" in preview
    assert not flow.launches
    add_kodi = client.get("/add-kodi").text
    assert 'href="/create-kodi"' in add_kodi
    assert "Create Managed Kodi" in add_kodi


def test_failed_setup_exposes_original_operation_and_explicit_review(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    flow = Flow()
    flow.attach(broker)
    operation = seed_installation(database, "omega")
    login(client)
    page = client.get("/runtimes").text
    assert str(operation) in page
    assert 'data-install-action="retry"' in page
    assert "no mutation is retried automatically" in page
    assert "inspect the original operation" in page
    assert not flow.launches


def test_guided_setup_browser_review_and_failure_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("managed_setup_browser.js")),
            str(root / "src/kodi_lab/web/static/runtime-installations.js"),
        ],
        check=True,
    )

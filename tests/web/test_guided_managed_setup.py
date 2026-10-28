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
    assert "Guided managed Kodi setup" in page
    assert "Names for this guided setup are fixed by the reviewed recipe" in page
    assert "Prepare Kodi" in page and "Start Kodi" in page
    assert "Each step requires a separate action" in page
    assert "data-install-review" in page and "data-install-approved" in page
    assert "No operation recorded" in page
    listed = client.get("/api/runtime-installations").json()["installations"]
    for item in listed:
        expected = (
            f"{load_reviewed_recipe(item['generation']).recipe.display_generation} Lab"
        )
        assert item["managed_instance_name"] == expected
        assert f'data-instance-name="{expected}"' in page
    assert not flow.launches
    assert "Kodi 22 Piers RC1" not in page
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    preview = client.get("/runtimes").text
    assert 'data-instance-name="Kodi 22 Piers RC1"' in preview
    assert "data-preview-acknowledgement" in preview
    assert not flow.launches
    assert 'href="/runtimes#managed-setup-heading"' in client.get("/add-kodi").text


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

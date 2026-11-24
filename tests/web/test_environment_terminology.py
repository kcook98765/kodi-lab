"""Environment wording and existing onboarding routes retain their authority."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.web.conftest import EXTERNAL_TARGET_ID, login, seed_external_target
from tests.web.test_environment_identity import IdentityLabels
from tests.web.test_kodi_views import household as household


@pytest.mark.parametrize("view", ["standard", "development"])
@pytest.mark.parametrize("mode", ["home", "development"])
def test_onboarding_wording_preserves_modes_and_existing_mechanisms(
    client: TestClient, view: str, mode: str
) -> None:
    login(client)
    client.cookies.set("kodi_lab_admin_view", view)
    page = client.get(f"/add-kodi?mode={mode}")
    assert page.status_code == 200
    assert "<h1>Connect External Kodi</h1>" in page.text
    assert f'data-initial-mode="{mode}"' in page.text
    assert f'name="kodi_use" value="{mode}" checked' in page.text
    assert "External Production Kodi" in page.text
    assert "External Development Kodi" in page.text
    assert 'data-home-label="External Production Kodi"' in page.text
    assert 'data-development-label="External Development Kodi"' in page.text
    assert 'id="managed-kodi-choice-heading"' in page.text
    assert "data-standard-view" not in page.text
    assert "data-consent" in page.text
    assert "data-pair-gate disabled" in page.text
    assert "data-js-gate disabled" in page.text
    assert 'method="post" action="/add-kodi" data-connect-form' in page.text
    assert client.get("/add-kodi?mode=production").status_code == 422


def test_grouped_identity_is_not_repeated_on_every_card(
    client: TestClient, database: sessionmaker[Session], household: UUID
) -> None:
    seed_external_target(database)
    login(client)
    client.cookies.set("kodi_lab_admin_view", "development")
    overview = client.get("/kodi").text
    assert IdentityLabels(overview).types == ["managed"]
    assert 'id="external-summary-heading">External Kodi' in overview
    for path, kind in [
        ("/production-targets", "external_production"),
        ("/external-targets", "external_development"),
    ]:
        assert IdentityLabels(client.get(path).text).types == [kind]
    assert IdentityLabels(client.get(f"/kodi/{household}").text).types == [
        "external_production"
    ]
    for target, kind in [
        (household, "external_production"),
        (EXTERNAL_TARGET_ID, "external_development"),
    ]:
        page = client.get(f"/reconnect-kodi/{target}")
        assert page.status_code == 200
        assert IdentityLabels(page.text).types == [kind]
        assert "outside Kodi Lab" in page.text
        assert "Verified relationship" not in page.text


@pytest.mark.parametrize(
    ("route", "category"),
    [
        ("/external-targets", "External Development Kodi"),
        ("/production-targets", "External Production Kodi"),
    ],
)
def test_external_empty_states_name_the_environment(
    client: TestClient, route: str, category: str
) -> None:
    login(client)
    page = client.get(route)
    assert page.status_code == 200
    assert f"No {category} connected yet." in page.text
    assert "outside Kodi Lab" in page.text

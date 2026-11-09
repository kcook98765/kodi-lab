"""Stored environment identity stays separate from connection and trust state."""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalObservationState,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.storage.models import ExternalKodiTargetRow
from kodi_lab.web.environment_identity import environment_identity
from tests.web.conftest import EXTERNAL_TARGET_ID, login, seed_external_target
from tests.web.test_kodi_views import household as household


class IdentityLabels(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.types: list[str] = []
        self.decorative_icons = 0
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if kind := attributes.get("data-environment-type"):
            self.types.append(kind)
        if tag == "svg":
            assert attributes.get("aria-hidden") == "true"
            assert attributes.get("focusable") == "false"
            self.decorative_icons += 1


@pytest.mark.parametrize(
    ("kind", "name", "ownership"),
    [
        (TargetType.MANAGED, "Managed Kodi", "Kodi runs inside Kodi Lab."),
        (
            TargetType.EXTERNAL_PRODUCTION,
            "External Production Kodi",
            "Your household Kodi runs on another device, outside Kodi Lab.",
        ),
        (
            TargetType.EXTERNAL_DEVELOPMENT,
            "External Development Kodi",
            "Your test or development Kodi runs on another device, outside Kodi Lab.",
        ),
    ],
)
def test_stored_type_has_explicit_identity(
    kind: TargetType, name: str, ownership: str
) -> None:
    identity = environment_identity(kind)
    assert environment_identity(kind.value) == identity
    assert identity.display_name == name
    assert ownership in identity.ownership


def test_unknown_type_is_never_defaulted_to_production() -> None:
    with pytest.raises(ValueError):
        environment_identity("connected")


@pytest.mark.parametrize("invalidated", [False, True])
def test_pending_or_invalidated_production_keeps_category(
    client: TestClient, invalidated: bool
) -> None:
    login(client)
    service = cast(FastAPI, client.app).state.kodi_lab.production_targets
    target = service.register(
        logical_name="Household connection awaiting review",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.42",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    if invalidated:
        target = service.edit_endpoint(
            target.target_id,
            protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
            host="192.168.50.43",
            port=8080,
        )
    assert target.production_policy.designation_state == (
        "invalidated" if invalidated else "pending_native_identity"
    )
    for path in ("/kodi", f"/kodi/{target.target_id}", "/production-targets"):
        response = client.get(path)
        assert response.status_code == 200
        assert "External Production Kodi" in response.text
        assert "external_production" in IdentityLabels(response.text).types
        assert 'data-status="connected"' not in response.text
    assert service.get(target.target_id).target_class == "external_production"


@pytest.mark.parametrize("observation", ["reachable", "unreachable"])
def test_development_identity_does_not_assert_production_trust(
    client: TestClient, database: sessionmaker[Session], observation: str
) -> None:
    seed_external_target(database)
    with database.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
        assert row is not None
        row.observation_state = ExternalObservationState(observation)
    login(client)
    response = client.get("/external-targets")
    assert response.status_code == 200
    assert set(IdentityLabels(response.text).types) == {"external_development"}
    assert "External Development Kodi" in response.text
    assert "outside Kodi Lab" in response.text
    assert "Verified relationship" not in response.text
    assert 'data-status="connected"' not in response.text
    assert client.get(f"/kodi/{EXTERNAL_TARGET_ID}").status_code == 404
    assert str(EXTERNAL_TARGET_ID) not in client.get("/kodi").text


def test_identity_pages_keep_links_and_accessible_labels(
    client: TestClient, database: sessionmaker[Session], household: UUID
) -> None:
    seed_external_target(database)
    paths = ("/kodi", f"/kodi/{household}", "/production-targets", "/external-targets")
    for path in paths:
        assert client.get(path, follow_redirects=False).status_code in (401, 303)
    login(client)
    client.cookies.set("kodi_lab_admin_view", "development")
    pages = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        labels = IdentityLabels(response.text)
        assert labels.types and labels.decorative_icons == len(labels.types)
        for kind in labels.types:
            assert environment_identity(kind).display_name in response.text
        pages[path] = response.text
    overview = pages["/kodi"]
    assert set(IdentityLabels(overview).types) == {kind.value for kind in TargetType}
    for destination in (
        "/managed-kodi",
        "/production-targets",
        "/external-targets",
        "/mcp-clients",
    ):
        assert f'href="{destination}"' in overview
    assert 'data-status="connected"' in pages[f"/kodi/{household}"]
    assert 'data-slot="omega"' not in overview
    managed = client.get("/managed-kodi").text
    assert set(IdentityLabels(managed).types) == {"managed"}
    assert 'data-slot="omega"' in managed
    assert 'data-slot-action="start"' in managed
    assert str(household) not in managed
    assert "Living room" not in managed
    # Optional controller-rendered fixtures for an entirely intercepted browser.
    if evidence := os.getenv("KODI_ENVIRONMENT_IDENTITY_EVIDENCE"):
        output = Path(evidence)
        output.mkdir(parents=True, exist_ok=True)
        (output / "pages.json").write_text(json.dumps(pages))

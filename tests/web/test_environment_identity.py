"""Stored environment identity stays separate from connection and trust state."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import cast

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
        if path == "/kodi":
            assert 'id="external-summary-heading">External Kodi' in response.text
            assert "Production devices:" in response.text
        else:
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

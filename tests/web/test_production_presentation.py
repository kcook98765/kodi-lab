"""External Production presentation uses stored identity and performs no probes."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_policy import (
    advance_production_policy,
    bind_external_production_transition,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgeReachabilityState,
    ProductionDesignationState,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ExternalScreenshotPreferenceRow,
    HouseholdOnboardingRow,
)
from tests.web.conftest import login
from tests.web.test_kodi_views import household as household


@pytest.mark.parametrize(
    "case",
    [
        "connected",
        "unavailable",
        "identity-mismatch",
        "invalidated-trust",
        "disabled",
        "disabled-trust-failure",
        "recovery",
        "screenshots-off",
        "retired",
    ],
)
def test_export_production_review_states(
    client: TestClient, database: sessionmaker[Session], household: UUID, case: str
) -> None:
    with database.begin() as session:
        target = session.get(ExternalKodiTargetRow, str(household))
        bridge = session.get(ExternalBridgePairingRow, str(household))
        policy = session.get(ExternalProductionPolicyRow, str(household))
        assert target and bridge and policy
        if case == "unavailable":
            bridge.reachability_state = ExternalBridgeReachabilityState.UNREACHABLE
        elif case == "identity-mismatch":
            bridge.reachability_state = (
                ExternalBridgeReachabilityState.IDENTITY_MISMATCH
            )
        elif case in {
            "disabled",
            "retired",
            "invalidated-trust",
            "disabled-trust-failure",
        }:
            if case == "invalidated-trust":
                policy.designation_state = ProductionDesignationState.INVALIDATED
                advance_production_policy(policy, now=datetime.now(UTC))
            else:
                if case == "disabled-trust-failure":
                    policy.designation_state = ProductionDesignationState.INVALIDATED
                bind_external_production_transition(
                    session, target, policy, now=datetime.now(UTC)
                )
                target.administrative_state = ExternalAdministrativeState(
                    "deactivated" if case == "retired" else "disabled"
                )
                if case == "retired":
                    target.deactivated_at = "2026-11-07T12:00:00.000000Z"
        elif case == "screenshots-off":
            preference = session.get(ExternalScreenshotPreferenceRow, str(household))
            assert preference
            preference.enabled = False
            preference.revision += 1
        elif case == "recovery":
            session.add(
                HouseholdOnboardingRow(
                    id=str(uuid4()),
                    target_id=str(household),
                    name="Living room",
                    host="192.168.50.40",
                    kodi_port=8080,
                    bridge_port=8765,
                    request_digest="a" * 64,
                    stage="blocked",
                    created_by="fixture",
                    created_at="2026-11-07T12:00:00.000000Z",
                    updated_at="2026-11-07T12:00:00.000000Z",
                )
            )
    login(client)
    listing = "/production-targets" + (
        "?include_retired=true" if case == "retired" else ""
    )
    pages = {}
    for path in [listing, f"/kodi/{household}"]:
        response = client.get(path)
        assert response.status_code == 200
        pages[path] = response.text
    assert all("External Production Kodi" in html for html in pages.values())
    listing_html = pages[listing]
    parser = ProductionStructure()
    parser.feed(listing_html)
    assert parser.target_count == 1  # One controller binding, no second inventory.
    assert parser.actions.count("probe") == (0 if case == "retired" else 1)
    if case != "retired":
        assert parser.probe_disabled == (
            case not in {"disabled", "disabled-trust-failure"}
        )
    assert parser.advanced_actions  # Existing privileged controls stay available.
    assert not {"start", "stop", "reset"}.intersection(parser.actions)
    assert parser.summary_advanced == [False]
    needs_review = case in {
        "identity-mismatch",
        "invalidated-trust",
        "disabled-trust-failure",
        "recovery",
        "retired",
    }
    assert parser.advanced_open == [needs_review]
    if case in {"identity-mismatch", "invalidated-trust", "disabled-trust-failure"}:
        assert "Connection trust or setup needs review" in listing_html
        assert parser.trust_alert_advanced == [False]
    for path, html in pages.items():
        assert "authenticated inspection access to this exact Kodi" in html
        assert "trusted Bridge connection" in html
        if case == "unavailable":
            assert "Kodi Lab cannot start it for you" in html
        if case in {"disabled", "disabled-trust-failure", "retired"}:
            assert f'href="/reconnect-kodi/{household}"' not in html
            assert "Reconnect unavailable" in html
        else:
            assert f'href="/reconnect-kodi/{household}"' in html
        if case == "screenshots-off":
            if path == listing:
                assert "Screenshots are disabled by your capture preference" in html
            else:
                assert f'data-screenshot-preference="{household}"' in html
                assert "Screenshots are disabled by your capture preference" not in html
        if case == "identity-mismatch":
            assert "Kodi or Bridge identity does not match" in html
        if case in {"invalidated-trust", "disabled-trust-failure"}:
            assert "Previous production trust is no longer valid" in html
        if case == "recovery":
            assert "Saved connection setup needs review" in html
            assert 'href="/add-kodi?mode=home"' in html
    assert f'href="/kodi/{household}"' in listing_html
    assert f'href="/production-targets#{household}"' in pages[f"/kodi/{household}"]
    if destination := os.getenv("KODI_PRODUCTION_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"case-{case}.json").write_text(
            json.dumps({"target": str(household), "case": case, "pages": pages})
        )


class ProductionStructure(HTMLParser):
    """Check meaningful placement without coupling tests to copy or CSS."""

    def __init__(self) -> None:
        super().__init__()
        self.details: list[bool] = []
        self.target_count = 0
        self.probe_disabled = False
        self.actions: list[str] = []
        self.advanced_actions: list[str] = []
        self.advanced_open: list[bool] = []
        self.summary_advanced: list[bool] = []
        self.trust_alert_advanced: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "details":
            advanced = "data-production-advanced" in values
            self.details.append(advanced)
            if advanced:
                self.advanced_open.append("open" in values)
        if "data-target-id" in values:
            self.target_count += 1
        if "data-production-summary" in values:
            self.summary_advanced.append(any(self.details))
        if action := values.get("data-action"):
            self.actions.append(action)
            if action == "probe":
                self.probe_disabled = "disabled" in values
            if any(self.details):
                self.advanced_actions.append(action)

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self.details.pop()

    def handle_data(self, data: str) -> None:
        if "Connection trust or setup needs review" in data:
            self.trust_alert_advanced.append(any(self.details))

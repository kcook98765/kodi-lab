"""Household views use recorded trust, preserve authority and perform no I/O."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.production_inspection import (
    _production_bridge_endpoint_digest,
)
from kodi_lab.application.production_policy import (
    advance_production_policy,
    bind_external_production_transition,
)
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceError
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ExternalScreenshotPreferenceRow,
    HouseholdOnboardingRow,
    ReviewedBridgeBuildRow,
    TargetRow,
)
from tests.application.test_production_bridges import _Verifier
from tests.web.conftest import EXTERNAL_TARGET_ID, INSTANCE_ID, login


@pytest.fixture
def household(
    client: TestClient, database: sessionmaker[Session], request: pytest.FixtureRequest
) -> UUID:
    """Synthetic native observation and stored Bridge snapshot, with no sockets."""
    service = cast(
        ExternalTargetAdministrationService,
        cast(FastAPI, client.app).state.kodi_lab.production_targets,
    )
    service._verifier = cast(Any, _Verifier())
    capabilities = {"target_inspect", "addon_inventory", "screenshot"}
    if getattr(request, "param", False):
        capabilities.add("semantic_remote")
    target = service.register(
        logical_name="Living room",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.40",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities=capabilities,
    )
    observed = service.probe(target.target_id)  # Fake verifier, no network.
    assert observed.pending_observation_id is not None
    service.confirm(target.target_id, observed.pending_observation_id)
    service.confirm_production_designation(
        target.target_id, observed.pending_observation_id
    )
    service.set_enabled(target.target_id, enabled=True)
    bridges = cast(FastAPI, client.app).state.kodi_lab.external_bridges
    build = bridges.register_reviewed_build(
        semantic_fingerprint="b" * 64,
        zip_sha256="c" * 64,
        addon_id="service.kodi_lab_bridge",
        addon_version="1.1.8",
        protocol_version="1",
        capabilities=("screenshot",),
        supported_kodi_versions=("21.3",),
        source_reference="synthetic Web view fixture",
    )
    with database.begin() as session:
        external = session.get(ExternalKodiTargetRow, str(target.target_id))
        policy = session.get(ExternalProductionPolicyRow, str(target.target_id))
        assert external is not None and policy is not None
        session.add(
            ExternalBridgePairingRow(
                target_id=str(target.target_id),
                bridge_port=8765,
                pairing_state=ExternalBridgePairingState.PAIRED,
                reachability_state=ExternalBridgeReachabilityState.REACHABLE,
                bridge_secret_reference=str(uuid4()),
                pinned_bridge_id=str(uuid4()),
                observed_capabilities_json='["screenshot"]',
                verified_capabilities_json='["screenshot"]',
                pinned_build_fingerprint="b" * 64,
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.8",
                pinned_protocol_version="1",
                pinned_kodi_version="21.3",
                endpoint_revision_digest=_production_bridge_endpoint_digest(
                    external, 8765
                ),
                production_identity_generation=policy.identity_generation,
                production_bridge_trust_revision=1,
                reviewed_build_id=str(build.build_id),
                production_confirmed_by="synthetic_fixture",
                production_confirmed_at="2026-10-08T12:00:00.000000Z",
                verified_at="2026-10-08T12:00:00.000000Z",
            )
        )
    return target.target_id


def detail(client: TestClient, target_id: UUID) -> str:
    response = client.get(f"/kodi/{target_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    return response.text


def test_empty_kodi_list(client: TestClient) -> None:
    login(client)
    page = client.get("/kodi")
    assert page.status_code == 200
    assert "No household Kodi added yet" in page.text
    assert 'href="/add-kodi"' in page.text
    assert 'class="kodi-card"' not in page.text


def test_detail_safe_auth_and_classification(
    client: TestClient, household: UUID
) -> None:
    assert client.get(f"/kodi/{household}").status_code == 401
    login(client)
    assert client.post(f"/kodi/{household}").status_code == 405
    for target in (INSTANCE_ID, EXTERNAL_TARGET_ID, uuid4()):
        assert client.get(f"/kodi/{target}").status_code == 404
    page = detail(client, household)
    assert 'href="/kodi" aria-current="page"' in page
    assert "Living room" in page
    assert 'data-status="connected">Connected' in page
    assert "21.3" in page and "Verified relationship" in page
    assert "08 Oct 2026, 12:00 UTC" in page
    assert "Read-only" in page and "Not configured" in page
    assert 'class="kodi-diagnostics"' in page
    assert "<summary>Advanced diagnostics</summary>" in page
    assert (
        "Target identifier" not in page.split('<details class="kodi-diagnostics">')[0]
    )
    assert "<button" not in page.split("<main ")[1]  # No mutation controls.
    assert "<form" not in page.split("<main ")[1]
    assert "http-equiv" not in page


@pytest.mark.parametrize("enabled", [True, False, None])
def test_screenshot_preference_display(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    enabled: bool | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if enabled is None:

        def unavailable(_target_id: UUID) -> None:
            raise ScreenshotPreferenceError("screenshot_preference_unavailable")

        monkeypatch.setattr(
            cast(FastAPI, client.app).state.kodi_lab.screenshot_preferences,
            "read",
            unavailable,
        )
    with database.begin() as session:
        row = session.get(ExternalScreenshotPreferenceRow, str(household))
        assert row is not None
        if enabled is not None and row.enabled != enabled:
            row.enabled = enabled
            row.revision += 1
    login(client)
    page = detail(client, household)
    label = "Unavailable" if enabled is None else "Enabled" if enabled else "Disabled"
    assert f"<dt>Capture preference</dt><dd>{label}</dd>" in page
    assert "ordinary read-only inspection" in page
    assert 'data-status="connected">Connected' in page  # Independent of preference.


@pytest.mark.parametrize(
    ("field", "value", "status", "message"),
    [
        (
            "bridge_reachability",
            "unreachable",
            "disconnected",
            "Temporarily unreachable",
        ),
        ("bridge_reachability", "unknown", "unknown", "No reliable Bridge"),
        ("bridge_reachability", "identity_mismatch", "attention", "trust needs"),
        ("bridge_reachability", "authentication_failed", "attention", "trust needs"),
        ("bridge_reachability", "protocol_unsupported", "attention", "trust needs"),
        ("bridge_pairing", "needs_reverification", "attention", "trust needs"),
        ("native_verification", "identity_mismatch", "attention", "trust needs"),
        ("native_verification", "needs_reverification", "attention", "trust needs"),
        (
            "native_observation",
            "unreachable",
            "disconnected",
            "Temporarily unreachable",
        ),
        ("admin", "disabled", "disconnected", "Disabled by an administrator"),
        ("admin", "deactivated", "disconnected", "Administratively disconnected"),
        ("build", "retired", "attention", "needs verification"),
        ("binding", "stale", "attention", "needs verification"),
        ("stage", "native_verified", "attention", "Setup is incomplete"),
        ("stage", "blocked", "attention", "Setup needs administrator attention"),
    ],
)
def test_authoritative_status(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    field: str,
    value: str,
    status: str,
    message: str,
) -> None:
    with database.begin() as session:
        external = session.get(ExternalKodiTargetRow, str(household))
        bridge = session.get(ExternalBridgePairingRow, str(household))
        assert external is not None and bridge is not None
        if field in {"native_verification", "admin"}:
            policy = session.get(ExternalProductionPolicyRow, str(household))
            assert policy is not None
            bind_external_production_transition(
                session, external, policy, now=datetime.now(UTC)
            )
        if field == "bridge_reachability":
            bridge.reachability_state = ExternalBridgeReachabilityState(value)
        elif field == "bridge_pairing":
            bridge.pairing_state = ExternalBridgePairingState(value)
        elif field == "native_verification":
            external.verification_state = ExternalVerificationState(value)
        elif field == "native_observation":
            external.observation_state = ExternalObservationState(value)
        elif field == "admin":
            external.administrative_state = ExternalAdministrativeState(value)
            if value == "deactivated":
                external.deactivated_at = "2026-10-08T12:00:00.000000Z"
        elif field == "build":
            build = session.get(ReviewedBridgeBuildRow, bridge.reviewed_build_id)
            assert build is not None
            build.state = value
            build.retired_by = "fixture"
            build.retired_at = "2026-10-08T12:00:00.000000Z"
        elif field == "binding":
            bridge.endpoint_revision_digest = "a" * 64
        elif field == "stage":
            session.add(
                HouseholdOnboardingRow(
                    id=str(uuid4()),
                    target_id=str(household),
                    name="Living room",
                    host="192.168.50.40",
                    kodi_port=8080,
                    bridge_port=8765,
                    request_digest="f" * 64,
                    stage=value,
                    created_by="fixture",
                    created_at="2026-10-08T12:00:00.000000Z",
                    updated_at="2026-10-08T12:00:00.000000Z",
                )
            )
    login(client)
    page = detail(client, household)
    assert f'data-status="{status}"' in page
    assert message in page
    if value == "identity_mismatch" and (
        evidence := os.getenv("KODI_VIEW_BROWSER_EVIDENCE")
    ):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"production-mismatch-{field}.html").write_text(
            client.get("/production-targets").text
        )
    assert 'data-status="connected"' not in page
    current = client.get("/kodi").text
    if field == "admin" and value == "deactivated":
        assert str(household) not in current
        history = client.get("/production-targets?include_retired=true").text
        assert str(household) in history
        assert "Retired" in history
    else:
        assert f'data-status="{status}"' in current


def test_native_response_alone_is_not_connected(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
) -> None:
    with database.begin() as session:
        row = session.get(ExternalBridgePairingRow, str(household))
        assert row is not None
        session.delete(row)
    login(client)
    assert "Setup is incomplete" in detail(client, household)
    assert 'data-status="connected"' not in client.get("/kodi").text


def test_native_binding_attention_precedes_unreachable(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
) -> None:
    with database.begin() as session:
        external = session.get(ExternalKodiTargetRow, str(household))
        policy = session.get(ExternalProductionPolicyRow, str(household))
        bridge = session.get(ExternalBridgePairingRow, str(household))
        assert external is not None and policy is not None and bridge is not None
        bind_external_production_transition(
            session, external, policy, now=datetime.now(UTC)
        )
        external.observed_kodi_version = "21.9"
        bridge.reachability_state = ExternalBridgeReachabilityState.UNREACHABLE
    login(client)
    page = detail(client, household)
    assert 'data-status="attention"' in page
    assert "needs verification" in page
    assert "Temporarily unreachable" not in page


@pytest.mark.parametrize("household", [True], indirect=True)
@pytest.mark.parametrize("suspended", [True, False])
def test_remote_configuration_is_not_authorization(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    suspended: bool,
) -> None:
    with database.begin() as session:
        row = session.get(ExternalKodiTargetRow, str(household))
        policy = session.get(ExternalProductionPolicyRow, str(household))
        assert row is not None and policy is not None
        if policy.mutations_suspended != suspended:
            policy.mutations_suspended = suspended
            advance_production_policy(policy, now=datetime.now(UTC))
    login(client)
    page = detail(client, household)
    assert (
        "Configured, currently suspended"
        if suspended
        else "Configured; separate authorization is required"
    ) in page
    assert "These settings do not grant an agent access" in page
    assert "<button" not in page.split("<main ")[1]


def test_views_do_not_mutate_or_probe(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A page view attempted live I/O or mutation")

    for service, names in [
        (context.production_targets, ["probe", "set_enabled", "confirm"]),
        (context.external_targets, ["probe", "set_enabled", "confirm"]),
        (
            context.external_bridges,
            ["reverify", "attempt_pairing", "capture_screenshot"],
        ),
        (context.status, ["list_statuses"]),
    ]:
        for name in names:
            monkeypatch.setattr(service, name, forbidden)
    statements: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _params: Any,
        _context: Any,
        _executemany: Any,
    ) -> None:
        statements.append(statement)

    bind = database.kw["bind"]
    event.listen(bind, "before_cursor_execute", record)
    try:
        assert client.get("/kodi").status_code == 200
        detail(client, household)
        assert client.get("/production-targets").status_code == 200
        assert client.get("/external-targets").status_code == 200
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert not [
        s
        for s in statements
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", s, re.I)
    ]


def test_multiple_household_and_development_separation(
    client: TestClient,
    database: sessionmaker[Session],
    household: UUID,
) -> None:
    login(client)
    service = cast(
        ExternalTargetAdministrationService,
        cast(FastAPI, client.app).state.kodi_lab.production_targets,
    )
    second = service.register(
        logical_name="Bedroom",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.41",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    client.cookies.set("kodi_lab_admin_view", "development")
    page = client.get("/kodi").text
    assert page.count('class="kodi-card"') == 2
    assert page.index("Bedroom") < page.index("Living room")
    assert f'href="/kodi/{second.target_id}"' in page
    household_html = page.split('<section class="navigation-group"')[0]
    assert "Managed Kodi" not in household_html
    assert "External development Kodi" not in household_html
    with database() as session:
        managed_name = session.scalar(
            select(TargetRow.name).where(TargetRow.id == str(INSTANCE_ID))
        )
    assert managed_name is not None and escape(managed_name) not in page
    assert "Current Kodi" in page
    assert "No development Kodi is selected" in page
    assert "<script>not markup</script>" not in page
    assert 'href="/development"' in page
    assert 'href="/external-targets"' in page
    assert "Target identifier" not in page and "fingerprint" not in page
    # Save real controller-rendered fixture HTML only when explicitly requested
    # by the browser validation runner. Never touches persistent preview data.
    if evidence := os.getenv("KODI_VIEW_BROWSER_EVIDENCE"):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        # Keep the escaping assertion above, then use a representative friendly
        # managed name for the separate browser evidence fixture.
        with database.begin() as session:
            managed = session.get(TargetRow, str(INSTANCE_ID))
            assert managed is not None
            managed.name = "Managed test Kodi"
        page = client.get("/kodi").text
        routes = {
            "/kodi": page,
            f"/kodi/{household}": detail(client, household),
            f"/kodi/{second.target_id}": detail(client, second.target_id),
        }
        with database.begin() as session:
            bridge = session.get(ExternalBridgePairingRow, str(household))
            assert bridge is not None
            bridge.reachability_state = ExternalBridgeReachabilityState.UNREACHABLE
        routes["/fixture/disconnected"] = detail(client, household)
        routes["/fixture/disconnected-list"] = client.get("/kodi").text
        with database.begin() as session:
            bridge = session.get(ExternalBridgePairingRow, str(household))
            assert bridge is not None
            bridge.reachability_state = (
                ExternalBridgeReachabilityState.IDENTITY_MISMATCH
            )
        routes["/fixture/attention"] = detail(client, household)
        routes["/fixture/attention-list"] = client.get("/kodi").text
        with database.begin() as session:
            bridge = session.get(ExternalBridgePairingRow, str(household))
            preference = session.get(ExternalScreenshotPreferenceRow, str(household))
            assert bridge is not None and preference is not None
            bridge.reachability_state = ExternalBridgeReachabilityState.REACHABLE
            preference.enabled = False
            preference.revision += 1
        routes["/fixture/screenshots-disabled"] = detail(client, household)
        (root / "fixture-routes.json").write_text(json.dumps(routes))


def test_administration_priorities_and_pending_identity(
    client: TestClient, household: UUID
) -> None:
    login(client)
    production = client.get("/production-targets")
    external = client.get("/external-targets")
    assert production.status_code == external.status_code == 200
    page = production.text
    assert page.index("Current production connections") < page.index(
        "Create production target"
    )
    assert page.index("Current production connections") < page.index(
        'id="reviewed-bridge-builds"'
    )
    assert "<details><summary>Capability administration</summary>" in page
    assert "<details><summary>Connection identity, trust and scopes</summary>" in page
    assert (
        "<details><summary>Connection identity and capabilities</summary>"
        in external.text
    )
    assert external.text.count('<label>Protocol <select name="protocol_kind">') == 2
    context = dict(cast(Any, external).context)
    bridge = context["bridge_statuses"][EXTERNAL_TARGET_ID]
    context["bridge_statuses"] = {
        **context["bridge_statuses"],
        EXTERNAL_TARGET_ID: bridge.model_copy(
            update={
                "pairing_state": ExternalBridgePairingState.PAIRING_PENDING,
                "observed_bridge_id": uuid4(),
            }
        ),
    }
    pending = cast(Any, external).template.render(**context)
    assert (
        "<details open><summary>Bridge identity and capabilities</summary>" in pending
    )
    assert (
        f"<details open><summary>Bridge {context['supported_bridge_version']} "
        "OTP pairing</summary>" in pending
    )
    if evidence := os.getenv("KODI_VIEW_BROWSER_EVIDENCE"):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        for name, content in [
            ("production", page),
            ("external", external.text),
            ("external-pending", pending),
        ]:
            (root / f"{name}.html").write_text(content)

"""Disposable stored-state fixtures; no external Kodi or Bridge is contacted."""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_capability_amendments import (
    DevelopmentCapabilityAmendmentError,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalObservationState,
    ExternalVerificationState,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalReconnectOperationRow,
    ExternalSecretRetirementRow,
)
from tests.web.conftest import EXTERNAL_TARGET_ID, ORIGIN, login, seed_external_target

CASES = (
    "empty",
    "registered-disabled",
    "enabled-unverified",
    "verified-unpaired",
    "pairing-pending",
    "paired-limited",
    "capable-no-grants",
    "native-mismatch",
    "bridge-mismatch",
    "unreachable-native",
    "unreachable-bridge",
    "auth-failure",
    "reconnect-recovery",
    "retired",
    "cleanup-failure",
    "active-cleanup",
    "verified-with-observation",
    "registered-observed",
)
STAMP = "2026-11-07T12:00:00.000000Z"


@pytest.fixture(params=CASES)
def development_case(
    request: pytest.FixtureRequest, database: sessionmaker[Session]
) -> str:
    case = str(request.param)
    if case == "empty":
        return case
    seed_external_target(database)
    with database.begin() as session:
        target = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
        assert target is not None
        if case in {"registered-disabled", "registered-observed"}:
            target.administrative_state = ExternalAdministrativeState.DISABLED
            target.verification_state = ExternalVerificationState.UNVERIFIED
            target.observation_state = None
            target.observed_kodi_version = None
            target.verified_capabilities_json = "[]"
            target.verified_at = target.last_observed_at = None
        if case in {"verified-with-observation", "registered-observed"}:
            target.pending_observation_id = str(uuid4())
        if case == "enabled-unverified":
            target.verification_state = ExternalVerificationState.UNVERIFIED
        if case == "native-mismatch":
            target.verification_state = ExternalVerificationState.IDENTITY_MISMATCH
            target.pending_observation_id = str(uuid4())
        if case == "unreachable-native":
            target.observation_state = ExternalObservationState.UNREACHABLE
        if case == "auth-failure":
            target.observation_state = ExternalObservationState.AUTHENTICATION_FAILED
        if case in {"retired", "cleanup-failure"}:
            target.administrative_state = ExternalAdministrativeState.DEACTIVATED
            target.deactivated_at = STAMP
        if case in {"cleanup-failure", "active-cleanup"}:
            session.add(
                ExternalSecretRetirementRow(
                    store_kind="kodi",
                    reference=str(uuid4()),
                    target_id=str(EXTERNAL_TARGET_ID),
                    created_at=STAMP,
                )
            )
        if case == "reconnect-recovery":
            session.add(
                ExternalReconnectOperationRow(
                    id=str(uuid4()),
                    target_id=str(EXTERNAL_TARGET_ID),
                    target_class="external_development",
                    bridge_port=8765,
                    stage="bootstrap_started",
                    native_binding="a" * 64,
                    expected_bridge_revision=1,
                    error_code="pairing_recovery_required",
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
        if case not in {
            "registered-disabled",
            "registered-observed",
            "enabled-unverified",
            "verified-unpaired",
        }:
            capabilities = (
                ["screenshot"]
                if case != "capable-no-grants"
                else [
                    "screenshot",
                    "workspace_deployment",
                    "ui_observation",
                    "log_read",
                ]
            )
            if case == "capable-no-grants":
                target.configured_capabilities_json = json.dumps(
                    [
                        "target_inspect",
                        "addon_inventory",
                        "screenshot",
                        "workspace_deployment",
                        "fixed_execution",
                    ]
                )
                session.flush()  # Configure before publishing the synthetic pairing.
            session.add(
                ExternalBridgePairingRow(
                    target_id=str(EXTERNAL_TARGET_ID),
                    bridge_port=8765,
                    pairing_state=ExternalBridgePairingState.PAIRING_PENDING
                    if case == "pairing-pending"
                    else ExternalBridgePairingState.PAIRED,
                    reachability_state=ExternalBridgeReachabilityState.IDENTITY_MISMATCH
                    if case == "bridge-mismatch"
                    else ExternalBridgeReachabilityState.UNREACHABLE
                    if case == "unreachable-bridge"
                    else ExternalBridgeReachabilityState.REACHABLE,
                    bridge_secret_reference=str(uuid4()),
                    observed_bridge_id=str(uuid4()),
                    observed_capabilities_json=json.dumps(
                        sorted(capabilities), separators=(",", ":")
                    ),
                    verified_capabilities_json="[]"
                    if case == "pairing-pending"
                    else json.dumps(sorted(capabilities), separators=(",", ":")),
                    pinned_bridge_id=None
                    if case == "pairing-pending"
                    else str(uuid4()),
                    pinned_protocol_version="1",
                    pinned_addon_id="service.kodi_lab_bridge",
                    pinned_addon_version="1.1.31",
                    pinned_kodi_version="21.3",
                    pinned_build_fingerprint="b" * 64,
                    paired_at=None if case == "pairing-pending" else STAMP,
                    verified_at=None if case == "pairing-pending" else STAMP,
                )
            )
    return case


def test_development_review_states(client: TestClient, development_case: str) -> None:
    assert client.get("/external-targets").status_code == 401
    login(client)
    route = "/external-targets" + (
        "?include_retired=true"
        if development_case in {"retired", "cleanup-failure"}
        else ""
    )
    response = client.get(route)
    assert response.status_code == 200
    assert "External Development Kodi" in response.text
    assert "outside Kodi Lab" in response.text
    assert "Verified relationship" not in response.text
    structure = DevelopmentStructure()
    structure.feed(response.text)
    if development_case == "empty":
        assert not structure.targets
        assert "No External Development Kodi connected yet" in response.text
    else:
        assert structure.targets == [str(EXTERNAL_TARGET_ID)]
        assert "Ready to inspect:" in response.text
        assert "explicit inspection permissions" in response.text
        assert "Ready for add-on testing:" in response.text
        assert "Deployed files do not prove" in response.text
        assert "Ready to view:" not in response.text
        assert structure.primary_in_details == [False]
        assert "Deployment readiness is not assessed here" in response.text
        assert "Verified support does not grant an agent access" in response.text
        assert "Ready for deployment" not in response.text
        if development_case == "unreachable-native":
            assert (
                "<dt>Kodi connection</dt><dd>Unreachable at last check</dd>"
                in response.text
            )
        if development_case == "unreachable-bridge":
            assert (
                "<dt>Bridge connection</dt><dd>Unreachable at last check</dd>"
                in response.text
            )
        if development_case not in {"retired", "cleanup-failure"}:
            assert structure.probe_disabled == (
                development_case not in {"registered-disabled", "registered-observed"}
            )
        assert structure.advanced_open == [
            development_case
            not in {
                "paired-limited",
                "verified-with-observation",
                "capable-no-grants",
                "unreachable-native",
                "unreachable-bridge",
            }
        ]
        assert structure.privileged_in_advanced
        assert all(structure.privileged_in_advanced)
        assert not any(structure.alert_in_details)
        assert not any(structure.privacy_in_details)
        assert not {"start", "stop", "reset"}.intersection(structure.actions)
        if development_case in {
            "native-mismatch",
            "bridge-mismatch",
            "pairing-pending",
            "reconnect-recovery",
            "auth-failure",
            "cleanup-failure",
        }:
            assert structure.alert_in_details
        if development_case == "capable-no-grants":
            assert 'data-development-status="verified"' in response.text
            assert (
                "does not establish deployment readiness or agent authority"
                in response.text
            )
        if development_case in {"cleanup-failure", "active-cleanup"}:
            assert "Credential cleanup is unfinished" in response.text
        if development_case == "active-cleanup":
            assert "Ask an administrator to review" in response.text
            assert "retry the existing connection retirement" not in response.text
        if development_case == "reconnect-recovery":
            assert "A saved reconnect is unfinished" in response.text
        if development_case == "pairing-pending":
            assert "do not submit another code" in response.text
            assert 'data-bridge-action="confirm"' in response.text
        if development_case == "verified-with-observation":
            assert 'data-development-status="verified"' in response.text
            assert "confirm" not in structure.actions
            assert "waiting for confirmation" not in response.text
        if development_case == "registered-observed":
            assert "confirm" in structure.actions
            assert "waiting for confirmation" in response.text
        reconnect = f'href="/reconnect-kodi/{EXTERNAL_TARGET_ID}"'
        assert (reconnect in response.text) == (
            development_case
            not in {
                "registered-disabled",
                "registered-observed",
                "enabled-unverified",
                "pairing-pending",
                "native-mismatch",
                "retired",
                "cleanup-failure",
            }
        )
    for path in (
        "/mcp-clients",
        "/development",
        "/development-sources",
        "/agent-workspaces",
        "/add-kodi?mode=development",
    ):
        assert f'href="{path}"' in response.text
    if destination := os.getenv("KODI_DEVELOPMENT_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        preference = client.get(
            f"/api/targets/{EXTERNAL_TARGET_ID}/screenshot-preference"
        )
        (root / f"case-{development_case}.json").write_text(
            json.dumps(
                {
                    "case": development_case,
                    "target": str(EXTERNAL_TARGET_ID),
                    "url": route,
                    "html": response.text,
                    "screenshot_preference": {
                        "status": preference.status_code,
                        "body": preference.json(),
                    },
                }
            )
        )


class DevelopmentStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.details: list[bool] = []
        self.targets: list[str] = []
        self.primary_in_details: list[bool] = []
        self.advanced_open: list[bool] = []
        self.privileged_in_advanced: list[bool] = []
        self.alert_in_details: list[bool] = []
        self.privacy_in_details: list[bool] = []
        self.probe_disabled = False
        self.actions: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "details":
            advanced = "data-development-advanced" in values
            self.details.append(advanced)
            if advanced:
                self.advanced_open.append("open" in values)
        if target := values.get("data-target-id"):
            self.targets.append(target)
        if "data-development-summary" in values:
            self.primary_in_details.append(bool(self.details))
        if values.get("role") == "alert" and "data-error" not in values:
            self.alert_in_details.append(bool(self.details))
        if action := values.get("data-action"):
            self.actions.append(action)
            self.privileged_in_advanced.append(any(self.details))
            if action == "probe":
                self.probe_disabled = "disabled" in values
        if values.get("data-bridge-action") or (tag == "form" and any(self.details)):
            self.privileged_in_advanced.append(any(self.details))

    def handle_data(self, data: str) -> None:
        if "HTTP privacy warning" in data:
            self.privacy_in_details.append(bool(self.details))

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self.details.pop()


def test_development_page_does_not_probe_pair_or_write(
    client: TestClient, database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_external_target(database)
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Reading the page attempted live I/O or mutation")

    for service, methods in (
        (context.external_targets, ("probe", "confirm", "set_enabled")),
        (
            context.external_bridges,
            (
                "begin_otp_pairing",
                "commit_otp_pairing",
                "confirm_pairing",
                "reverify",
                "capture_screenshot",
            ),
        ),
    ):
        for method in methods:
            monkeypatch.setattr(service, method, forbidden)
    writes: list[str] = []

    def record(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any
    ) -> None:
        if re.match(
            r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", statement, re.I
        ):
            writes.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        for suffix in ("", "?include_retired=true"):
            assert client.get("/external-targets" + suffix).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # Includes grants, presets, workspaces and screenshot preferences.
    assert not writes


def test_capability_amendment_web_admission_and_exact_service_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    calls: list[tuple[UUID, dict[str, object]]] = []

    def deny(target_id: UUID, **kwargs: object) -> NoReturn:
        calls.append((target_id, kwargs))
        raise DevelopmentCapabilityAmendmentError("bridge_identity_mismatch")

    monkeypatch.setattr(context.development_capability_amendments, "amend", deny)
    bridge = uuid4()
    payload: dict[str, object] = {
        "expected_capabilities": ["target_inspect", "screenshot"],
        "requested_capabilities": ["target_inspect"],
        "expected_bridge_id": str(bridge),
        "expected_bridge_fingerprint": "b" * 64,
        "confirmed": True,
    }
    route = f"/api/external-targets/{EXTERNAL_TARGET_ID}/capability-amendments"
    assert client.post(route, json=payload).status_code == 401
    csrf = login(client)
    assert client.post(route, json=payload).status_code == 403
    assert (
        client.post(route, json=payload, headers={"X-Kodi-Lab-CSRF": csrf}).status_code
        == 403
    )
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            route,
            json=payload,
            headers={**headers, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    for invalid in (
        {"confirmed": False},
        {"extra": True},
        {"expected_bridge_fingerprint": "wrong"},
    ):
        assert (
            client.post(route, json={**payload, **invalid}, headers=headers).status_code
            == 422
        )
    assert not calls
    assert client.post(route, json=payload, headers=headers).status_code == 409
    assert calls == [
        (
            EXTERNAL_TARGET_ID,
            {
                "expected_capabilities": frozenset({"target_inspect", "screenshot"}),
                "requested_capabilities": frozenset({"target_inspect"}),
                "expected_bridge_id": bridge,
                "expected_bridge_fingerprint": "b" * 64,
            },
        )
    ]

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_capability_amendments import (
    DevelopmentCapabilityAmendmentError,
    DevelopmentCapabilityAmendmentService,
)
from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.domain import ExternalBridgePairingState, TargetType
from kodi_lab.kodi.bridge import BridgeIdentity
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalDevelopmentCapabilityAmendmentRow,
    ExternalKodiTargetRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore, ExternalKodiCredential

TARGET = UUID("13000000-0000-4000-8000-000000000057")
BRIDGE = UUID("23000000-0000-4000-8000-000000000057")
FINGERPRINT = "a" * 64
NOW = "2026-09-29T12:00:00.000000Z"
BEFORE = frozenset({"target_inspect", "screenshot"})
EXPANDED = BEFORE | {"addon_inventory", "workspace_deployment", "fixed_execution"}


class FakeNative:
    supported = frozenset({"target_inspect", "addon_inventory", "fixed_execution"})

    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        _credential: ExternalKodiCredential | None,
        requested: frozenset[str],
    ) -> ExternalKodiProbeResult:
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name="Kodi",
            kodi_version="20.5",
            protocol_version="13.0.0",
            verified_capabilities=tuple(sorted(requested & self.supported)),
        )


class FakeBridge:
    capabilities: tuple[str, ...] = ("screenshot", "workspace_deployment")
    fingerprint = FINGERPRINT

    def observe_development_amendment_identity(
        self, _target_id: UUID
    ) -> tuple[BridgeIdentity, tuple[str, ...]]:
        return (
            BridgeIdentity(
                bridge_id=BRIDGE,
                protocol_version="1",
                addon_id="service.kodi_lab_bridge",
                addon_version="1.1.15",
                kodi_version="20.5",
                capabilities=self.capabilities,
                build_fingerprint=self.fingerprint,
            ),
            self.capabilities,
        )


@pytest.fixture
def amendment(
    tmp_path: Path,
) -> tuple[DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge]:
    engine = create_engine(f"sqlite:///{tmp_path / 'amendment.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            TargetRow(
                id=str(TARGET),
                name="mini",
                target_type="external_development",
                kodi_version="20.5",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ExternalKodiTargetRow(
                target_id=str(TARGET),
                protocol_kind="jsonrpc_http",
                host="127.0.0.1",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=json.dumps(
                    sorted(BEFORE), separators=(",", ":")
                ),
                verified_capabilities_json='["target_inspect"]',
                administrative_state="enabled",
                verification_state="verified",
                observation_state="reachable",
                verified_addresses_json='["127.0.0.1"]',
                verified_kodi_name="Kodi",
                verified_kodi_version="20.5",
                observed_kodi_name="Kodi",
                observed_kodi_version="20.5",
                observed_protocol_version="13.0.0",
                pending_observation_id=None,
                created_at=NOW,
                verified_at=NOW,
                deactivated_at=None,
                last_observed_at=NOW,
            )
        )
        session.flush()
        session.add(
            ExternalBridgePairingRow(
                target_id=str(TARGET),
                bridge_port=8765,
                pairing_state="paired",
                reachability_state="reachable",
                pairing_token_reference=None,
                bridge_secret_reference=str(uuid4()),
                pairing_token_expires_at=None,
                observed_bridge_id=str(BRIDGE),
                observed_protocol_version="1",
                observed_addon_id="service.kodi_lab_bridge",
                observed_addon_version="1.1.15",
                observed_kodi_version="20.5",
                observed_build_fingerprint=FINGERPRINT,
                observed_capabilities_json='["screenshot","workspace_deployment"]',
                pinned_bridge_id=str(BRIDGE),
                pinned_protocol_version="1",
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.15",
                pinned_kodi_version="20.5",
                pinned_build_fingerprint=FINGERPRINT,
                verified_capabilities_json='["screenshot","workspace_deployment"]',
                endpoint_revision_digest="b" * 64,
                token_generated_at=None,
                pairing_attempted_at=NOW,
                paired_at=NOW,
                verified_at=NOW,
                revoked_at=None,
                production_identity_generation=None,
                production_bridge_trust_revision=None,
                reviewed_build_id=None,
                production_confirmed_by=None,
                production_confirmed_at=None,
            )
        )
    bridge = FakeBridge()
    service = DevelopmentCapabilityAmendmentService(
        sessions,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8080,), test_loopback_enabled=True
        ),
        credential_store=ExternalCredentialStore(
            tmp_path / "secrets", master_key=None, key_version=1
        ),
        bridges=cast(Any, bridge),
        verifier=cast(Any, FakeNative()),
    )
    return service, sessions, bridge


def _amend(
    service: DevelopmentCapabilityAmendmentService,
    before: frozenset[str],
    requested: frozenset[str],
) -> UUID:
    return service.amend(
        TARGET,
        expected_capabilities=before,
        requested_capabilities=requested,
        expected_bridge_id=BRIDGE,
        expected_bridge_fingerprint=FINGERPRINT,
    ).amendment_id


def test_expansion_reduction_receipt_and_exact_replay(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, _bridge = amendment
    with sessions() as session:
        original = session.get(ExternalBridgePairingRow, str(TARGET))
        assert original is not None
        secret = original.bridge_secret_reference
    first = _amend(service, BEFORE, EXPANDED)
    assert _amend(service, BEFORE, EXPANDED) == first
    second = _amend(service, EXPANDED, BEFORE)
    assert second != first
    with sessions() as session:
        target = session.get(TargetRow, str(TARGET))
        external = session.get(ExternalKodiTargetRow, str(TARGET))
        pairing = session.get(ExternalBridgePairingRow, str(TARGET))
        receipts = session.scalars(
            select(ExternalDevelopmentCapabilityAmendmentRow)
        ).all()
        assert target is not None and target.target_type.value == "external_development"
        assert external is not None and json.loads(
            external.configured_capabilities_json
        ) == sorted(BEFORE)
        assert pairing is not None and pairing.pinned_bridge_id == str(BRIDGE)
        assert pairing.bridge_secret_reference == secret
        assert len(receipts) == 2
        assert all(
            row.target_classification == "external_development" for row in receipts
        )
        assert json.loads(receipts[0].requested_json) == sorted(EXPANDED)
        assert receipts[0].resulting_json == receipts[0].requested_json


def test_invalid_stale_and_unsupported_capability_fail_closed(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, bridge = amendment
    for requested in (BEFORE | {"unknown"}, BEFORE | {"semantic_remote"}):
        with pytest.raises(DevelopmentCapabilityAmendmentError):
            _amend(service, BEFORE, requested)
    bridge.capabilities = ("screenshot",)
    with pytest.raises(
        DevelopmentCapabilityAmendmentError, match="bridge_identity_stale"
    ):
        _amend(service, BEFORE, EXPANDED)
    bridge.capabilities = ("screenshot", "workspace_deployment")
    bridge.fingerprint = "c" * 64
    with pytest.raises(
        DevelopmentCapabilityAmendmentError, match="bridge_identity_stale"
    ):
        _amend(service, BEFORE, EXPANDED)
    with sessions.begin() as session:
        row = session.get(ExternalBridgePairingRow, str(TARGET))
        assert row is not None
        row.pairing_state = ExternalBridgePairingState.NEEDS_REVERIFICATION
    with pytest.raises(DevelopmentCapabilityAmendmentError, match="pairing_unverified"):
        _amend(service, BEFORE, EXPANDED)
    with sessions() as session:
        assert (
            session.scalars(select(ExternalDevelopmentCapabilityAmendmentRow)).all()
            == []
        )


def test_paired_target_cannot_change_maximum_without_receipt(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    _service, sessions, _bridge = amendment
    with (
        pytest.raises(
            IntegrityError, match="development_capability_amendment_required"
        ),
        sessions.begin() as session,
    ):
        row = session.get(ExternalKodiTargetRow, str(TARGET))
        assert row is not None
        row.configured_capabilities_json = '["target_inspect"]'


def test_database_failure_rolls_back_receipt_and_maximum(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, _bridge = amendment
    with sessions.begin() as session:
        session.execute(
            text(
                "CREATE TRIGGER reject_development_amendment BEFORE UPDATE OF "
                "configured_capabilities_json ON external_kodi_targets BEGIN "
                "SELECT RAISE(ABORT, 'injected_failure'); END"
            )
        )
    with pytest.raises(IntegrityError, match="injected_failure"):
        _amend(service, BEFORE, EXPANDED)
    with sessions() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET))
        assert row is not None
        assert json.loads(row.configured_capabilities_json) == sorted(BEFORE)
        assert (
            session.scalars(select(ExternalDevelopmentCapabilityAmendmentRow)).all()
            == []
        )


def test_paired_development_screenshot_reduction_keeps_pairing(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, _bridge = amendment
    receipt = _amend(service, BEFORE, frozenset({"target_inspect"}))
    with sessions() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET))
        pairing = session.get(ExternalBridgePairingRow, str(TARGET))
        assert (
            row is not None and row.configured_capabilities_json == '["target_inspect"]'
        )
        assert pairing is not None and pairing.pairing_state.value == "paired"
        assert pairing.bridge_secret_reference is not None
        assert session.get(ExternalDevelopmentCapabilityAmendmentRow, str(receipt))


def test_native_only_reduction_can_be_reexpanded_without_ordinary_bridge_authority(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, _bridge = amendment
    native_only = frozenset({"target_inspect"})
    _amend(service, BEFORE, native_only)
    with sessions() as session:
        with pytest.raises(
            ExternalBridgeServiceError, match="bridge_capability_unavailable"
        ):
            ExternalBridgeService._require_native_verified(session, TARGET)
        assert ExternalBridgeService._require_native_verified(
            session, TARGET, allow_no_configured_bridge_capability=True
        )
    _amend(service, native_only, BEFORE)
    with sessions() as session:
        assert ExternalBridgeService._require_native_verified(session, TARGET)


def test_production_target_cannot_use_development_amendment(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, _bridge = amendment
    with sessions.begin() as session:
        target = session.get(TargetRow, str(TARGET))
        assert target is not None
        target.target_type = TargetType.EXTERNAL_PRODUCTION
    with pytest.raises(DevelopmentCapabilityAmendmentError, match="target_not_found"):
        _amend(service, BEFORE, EXPANDED)


# Regression: Bridge capabilities and external-target capabilities are distinct.
def test_real_bridge_capabilities_navigation_amendment_roundtrip(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, sessions, bridge = amendment
    bridge.capabilities = ("screenshot", "ui_observation", "workspace_deployment")
    native = cast(FakeNative, service._verifier)
    native.supported = native.supported | {"semantic_remote"}
    requested = BEFORE | {"addon_inventory", "semantic_remote"}
    first = _amend(service, BEFORE, requested)
    assert _amend(service, BEFORE, requested) == first
    readback = service.list_receipts(TARGET)
    assert len(readback) == 1
    assert readback[0].bridge_verified_capabilities == bridge.capabilities
    assert set(readback[0].resulting_capabilities) == requested
    assert "ui_observation" not in readback[0].resulting_capabilities
    second = _amend(service, requested, BEFORE)
    assert first != second
    assert _amend(service, requested, BEFORE) == second
    with sessions() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET))
        assert row is not None
        assert row.configured_capabilities_json == '["screenshot","target_inspect"]'
        assert row.administrative_state.value == "enabled"
        assert row.verification_state.value == "verified"
        pairing = session.get(ExternalBridgePairingRow, str(TARGET))
        assert pairing is not None
        assert pairing.pinned_bridge_id == str(BRIDGE)
        assert pairing.pairing_state.value == "paired"
    assert len(service.list_receipts(TARGET)) == 2


def test_bridge_observation_is_not_a_target_permission(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
) -> None:
    service, _sessions, bridge = amendment
    bridge.capabilities = ("screenshot", "ui_observation", "workspace_deployment")
    with pytest.raises(
        DevelopmentCapabilityAmendmentError, match="capabilities_invalid"
    ):
        _amend(service, BEFORE, BEFORE | {"ui_observation"})
    assert service.list_receipts(TARGET) == ()


@pytest.mark.parametrize(
    "invalid_capability", ["unknown", "target_inspect", "semantic_remote"]
)
def test_invalid_bridge_receipt_rolls_back_without_widening(
    amendment: tuple[
        DevelopmentCapabilityAmendmentService, sessionmaker[Session], FakeBridge
    ],
    invalid_capability: str,
) -> None:
    service, sessions, bridge = amendment
    bridge.capabilities = ("screenshot", "workspace_deployment", invalid_capability)
    with pytest.raises(
        DevelopmentCapabilityAmendmentError, match="capabilities_invalid"
    ):
        _amend(service, BEFORE, EXPANDED)
    with sessions() as session:
        row = session.get(ExternalKodiTargetRow, str(TARGET))
        assert row is not None
        assert json.loads(row.configured_capabilities_json) == sorted(BEFORE)
        assert (
            session.scalars(select(ExternalDevelopmentCapabilityAmendmentRow)).all()
            == []
        )

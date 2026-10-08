"""Production policy and recovery over the real 1.1.15 OTP bridge endpoint."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.domain import ExternalBridgePairingState, TargetType
from kodi_lab.kodi.bridge import ExternalBridgeError
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalProductionPolicyRow,
    McpClientTargetGrantRow,
    McpProductionTargetScopeRow,
    ProductionBridgeHttpAcknowledgementRow,
    ProductionMutationControlRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore
from tests.application.test_production_bridges import _production_target
from tests.kodi.test_bridge_otp_bootstrap import _server
from tests.kodi.test_narrow_bridge_addon import _load


@pytest.fixture
def production_otp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[
    tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ]
]:
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "bridge-state.json",
        onboarding_path=tmp_path / "bridge-onboarding.json",
    )
    addon = SimpleNamespace(getSettingBool=lambda name: name == "enabled")
    monkeypatch.setattr(sys.modules["xbmcaddon"], "Addon", lambda _id: addon)
    bridge["_load_state"]()
    bridge["_onboarding_store"]().initialize(paired=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id, _credentials = _production_target(factory, tmp_path)
    store = ExternalBridgeSecretStore(
        tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
    )
    with _server(bridge) as port:
        service = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(port,), test_loopback_enabled=True
            ),
            secret_store=store,
        )
        service.register_reviewed_build(
            semantic_fingerprint=bridge["BUILD_FINGERPRINT"],
            zip_sha256="a" * 64,
            addon_id="service.kodi_lab_bridge",
            addon_version="1.1.15",
            protocol_version="1",
            capabilities=("screenshot", "workspace_deployment", "ui_observation"),
            supported_kodi_versions=("21.3",),
            source_reference="controlled bridge 1.1.15 fixture",
        )
        service.acknowledge_production_http_risk(target_id, bridge_port=port)
        yield factory, service, bridge, port, store, target_id
    engine.dispose()


def _code(bridge: dict[str, Any]) -> str:
    value = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert isinstance(value, str)
    return value


def _reference(factory: sessionmaker[Session], target_id: UUID) -> UUID:
    with factory() as session:
        row = session.get(ExternalBridgePairingRow, str(target_id))
        assert row is not None and row.bridge_secret_reference is not None
        return UUID(row.bridge_secret_reference)


def test_production_otp_stages_then_explicitly_confirms_with_hmac(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, service, bridge, port, store, target_id = production_otp
    client = service._client

    def legacy_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("production called legacy pair_bridge")

    monkeypatch.setattr(client, "pair", legacy_forbidden)
    code = _code(bridge)
    pending = service.begin_otp_pairing(target_id, bridge_port=port, code=code)
    assert pending.pairing_state is ExternalBridgePairingState.PAIRING_PENDING
    assert pending.observed_protocol_version == "1"
    assert pending.observed_addon_id == "service.kodi_lab_bridge"
    assert pending.observed_addon_version == "1.1.15"
    assert pending.production_bridge_trust_revision == 1
    assert pending.production_confirmed_at is None
    reference = _reference(factory, target_id)
    secret = store.read(target_id, reference, "request_authentication")
    assert len(secret) == 32 and secret != code.encode()
    assert secret not in (store._root / f"{reference}.json").read_bytes()
    with factory() as session:
        target = session.get(TargetRow, str(target_id))
        assert (
            target is not None and target.target_type is TargetType.EXTERNAL_PRODUCTION
        )
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        assert policy is not None and policy.mutations_suspended
        mutation_control = session.get(ProductionMutationControlRow, 1)
        assert (
            mutation_control is not None
            and mutation_control.production_mutations_blocked
        )
        assert (
            session.scalar(select(func.count()).select_from(McpClientTargetGrantRow))
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(McpProductionTargetScopeRow)
            )
            == 0
        )
    paired = service.commit_otp_pairing(target_id)
    assert paired.pairing_state is ExternalBridgePairingState.PAIRED
    assert paired.production_identity_generation == 1
    assert paired.production_confirmed_at is not None
    assert paired.verified_capabilities == ("screenshot",)
    service.authenticate_production_identity(target_id)
    assert service.status(target_id).pinned_bridge_id == paired.pinned_bridge_id
    assert service.commit_otp_pairing(target_id) == paired
    assert _reference(factory, target_id) == reference


def test_production_otp_recovers_lost_commit_after_service_restart(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, service, bridge, port, store, target_id = production_otp
    client = service._client
    original = client.commit_pairing
    calls = 0

    def lost_response(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 1:
            raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(client, "commit_pairing", lost_response)
    service.begin_otp_pairing(target_id, bridge_port=port, code=_code(bridge))
    reference = _reference(factory, target_id)
    with pytest.raises(ExternalBridgeServiceError, match="pairing_in_progress"):
        service.begin_otp_pairing(target_id, bridge_port=port, code="2222")
    with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
        service.commit_otp_pairing(target_id)
    assert (
        service.status(target_id).pairing_state
        is ExternalBridgePairingState.PAIRING_PENDING
    )
    assert json.loads((store._root.parent / "bridge-state.json").read_text())[
        "target_id"
    ] == str(target_id)
    restarted = ExternalBridgeService(
        factory,
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(port,), test_loopback_enabled=True
        ),
        secret_store=store,
    )
    paired = restarted.commit_otp_pairing(target_id)
    assert paired.pairing_state is ExternalBridgePairingState.PAIRED
    assert restarted.commit_otp_pairing(target_id) == paired
    assert _reference(factory, target_id) == reference
    restarted.authenticate_production_identity(target_id)
    assert restarted.status(target_id).pinned_bridge_id == paired.pinned_bridge_id
    with factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(TargetRow)
                .where(TargetRow.target_type == TargetType.EXTERNAL_PRODUCTION)
            )
            == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(ExternalBridgePairingRow))
            == 1
        )
    assert len(list(store._root.glob("*.json"))) == 1


def test_production_otp_requires_review_and_current_version(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
) -> None:
    factory, service, bridge, port, _store, target_id = production_otp
    bridge["_identity"].__globals__["ADDON_VERSION"] = "1.1.12"
    with pytest.raises(ExternalBridgeServiceError, match="bridge_identity_mismatch"):
        service.begin_otp_pairing(target_id, bridge_port=port, code=_code(bridge))
    assert (
        service.status(target_id).pairing_state is ExternalBridgePairingState.UNPAIRED
    )
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ExternalBridgePairingRow))
            == 0
        )


def test_production_otp_requires_exact_http_acknowledgement_before_contact(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
) -> None:
    factory, service, bridge, port, _store, target_id = production_otp
    code = _code(bridge)
    with factory.begin() as session:
        session.execute(delete(ProductionBridgeHttpAcknowledgementRow))
    with pytest.raises(
        ExternalBridgeServiceError, match="transport_acknowledgement_required"
    ):
        service.begin_otp_pairing(target_id, bridge_port=port, code=code)
    assert _code(bridge) == code
    assert (
        service.status(target_id).pairing_state is ExternalBridgePairingState.UNPAIRED
    )


def test_production_otp_review_failure_keeps_recoverable_provisional_state(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
) -> None:
    factory, service, bridge, port, store, target_id = production_otp
    pending = service.begin_otp_pairing(target_id, bridge_port=port, code=_code(bridge))
    reference = _reference(factory, target_id)
    assert pending.production_confirmed_at is None
    build_id = service.list_reviewed_builds()[0].build_id
    service.retire_reviewed_build(build_id)
    with pytest.raises(ExternalBridgeServiceError, match="bridge_build_not_reviewed"):
        service.commit_otp_pairing(target_id)
    assert (
        service.status(target_id).pairing_state
        is ExternalBridgePairingState.PAIRING_PENDING
    )
    assert _reference(factory, target_id) == reference
    assert (
        json.loads((store._root.parent / "bridge-state.json").read_text())["target_id"]
        is None
    )


def test_production_otp_recovers_lost_bootstrap_response_with_signed_identity(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, service, bridge, port, _store, target_id = production_otp
    original = service._client.bootstrap_otp
    calls = 0

    def lost(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(service._client, "bootstrap_otp", lost)
    pending = service.begin_otp_pairing(target_id, bridge_port=port, code=_code(bridge))
    assert pending.pairing_state is ExternalBridgePairingState.PAIRING_PENDING
    reference = _reference(factory, target_id)
    assert (
        service.commit_otp_pairing(target_id).pairing_state
        is ExternalBridgePairingState.PAIRED
    )
    assert calls == 1
    assert _reference(factory, target_id) == reference


def test_production_otp_rejects_native_kodi_identity_mismatch(
    production_otp: tuple[
        sessionmaker[Session],
        ExternalBridgeService,
        dict[str, Any],
        int,
        ExternalBridgeSecretStore,
        UUID,
    ],
) -> None:
    _factory, service, bridge, port, _store, target_id = production_otp
    bridge["_identity"].__globals__["_kodi_version"] = lambda: "20.5"
    with pytest.raises(ExternalBridgeServiceError, match="bridge_identity_mismatch"):
        service.begin_otp_pairing(target_id, bridge_port=port, code=_code(bridge))
    assert (
        service.status(target_id).pairing_state is ExternalBridgePairingState.UNPAIRED
    )

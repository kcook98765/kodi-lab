from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalKodiTargetEventRow,
    ExternalProductionPolicyRow,
    ProductionMutationControlRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore, ExternalKodiCredential


class FakeProductionVerifier:
    def __init__(self) -> None:
        self.name = "Kodi"
        self.version = "21.3"

    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        credential: ExternalKodiCredential | None,
        configured: frozenset[str],
    ) -> ExternalKodiProbeResult:
        capabilities = tuple(
            item for item in ("target_inspect", "addon_inventory") if item in configured
        )
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name=self.name,
            kodi_version=self.version,
            protocol_version="13.0.0",
            verified_capabilities=capabilities,
        )


type ProductionServiceFixture = tuple[
    ExternalTargetAdministrationService,
    ExternalTargetAdministrationService,
    sessionmaker[Session],
    FakeProductionVerifier,
]


@pytest.fixture
def production_service(
    tmp_path: Path,
) -> ProductionServiceFixture:
    engine = create_engine(f"sqlite:///{tmp_path / 'production.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            ProductionMutationControlRow(
                id=1,
                production_mutations_blocked=True,
                policy_epoch=1,
                updated_by="system",
                updated_at="2026-09-21T00:00:00.000000Z",
            )
        )
    verifier = FakeProductionVerifier()
    store = ExternalCredentialStore(
        tmp_path / "secrets", master_key=b"p" * 32, key_version=1
    )
    policy = ExternalEndpointPolicy(allowed_ports=(8080,))
    production = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=policy,
        credential_store=store,
        verifier=verifier,  # type: ignore[arg-type]
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    development = ExternalTargetAdministrationService(
        factory,
        endpoint_policy=policy,
        credential_store=store,
        verifier=verifier,  # type: ignore[arg-type]
    )
    return production, development, factory, verifier


def _register(service: ExternalTargetAdministrationService) -> ExternalTargetRead:
    return service.register(
        logical_name="Production Kodi",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.21",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
        configured_capabilities={"target_inspect", "addon_inventory"},
        username="kodi",
        password="production-secret-not-for-sqlite",
    )


def _designate(
    service: ExternalTargetAdministrationService,
) -> tuple[ExternalTargetRead, ExternalTargetRead]:
    created = _register(service)
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    native = service.confirm(created.target_id, observed.pending_observation_id)
    designated = service.confirm_production_designation(
        created.target_id, observed.pending_observation_id
    )
    return native, designated


def test_production_registration_and_two_separate_confirmations(
    production_service: ProductionServiceFixture,
) -> None:
    service, development, _factory, _verifier = production_service
    created = _register(service)
    assert created.target_class == "external_production"
    assert created.administrative_state.value == "disabled"
    assert created.verification_state.value == "unverified"
    assert created.production_policy is not None
    assert created.production_policy.lifecycle_state == "registered_disabled_unverified"
    assert created.production_policy.identity_generation == 1
    assert created.production_policy.mutations_suspended is True
    with pytest.raises(ExternalTargetError, match="target_not_found"):
        development.get(created.target_id)

    observed = service.probe(created.target_id)
    assert observed.production_policy is not None
    assert observed.production_policy.lifecycle_state == "observed_pending_confirmation"
    with pytest.raises(ExternalTargetError, match="verification_required"):
        service.set_enabled(created.target_id, enabled=True)

    assert observed.pending_observation_id is not None
    native = service.confirm(created.target_id, observed.pending_observation_id)
    assert native.verification_state.value == "verified"
    assert native.production_policy is not None
    assert native.production_policy.designation_state.value == "pending_confirmation"
    with pytest.raises(ExternalTargetError, match="production_designation_required"):
        service.set_enabled(created.target_id, enabled=True)

    designated = service.confirm_production_designation(
        created.target_id, observed.pending_observation_id
    )
    assert designated.production_policy is not None
    assert designated.production_policy.designation_state.value == "confirmed"
    assert designated.production_policy.confirmed_observation_id == (
        observed.pending_observation_id
    )
    assert len(designated.production_policy.confirmed_observation_digest or "") == 64
    enabled = service.set_enabled(created.target_id, enabled=True)
    assert enabled.production_policy is not None
    assert enabled.production_policy.lifecycle_state == "enabled_read_only"


def test_configuration_and_identity_drift_increment_generation_and_fail_closed(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, factory, verifier = production_service
    _native, designated = _designate(service)
    assert designated.production_policy is not None
    target_id = designated.target_id

    changed = service.edit_endpoint(
        target_id,
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="192.168.50.22",
        port=8080,
    )
    assert changed.production_policy is not None
    assert changed.production_policy.identity_generation == 2
    assert changed.production_policy.designation_state.value == "invalidated"
    assert changed.production_policy.mutations_suspended is True

    observed = service.probe(target_id)
    assert observed.pending_observation_id is not None
    service.confirm(target_id, observed.pending_observation_id)
    with pytest.raises(ExternalTargetError, match="production_designation_required"):
        service.set_enabled(target_id, enabled=True)
    service.confirm_production_designation(target_id, observed.pending_observation_id)

    rotated = service.rotate_credentials(
        target_id,
        authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
        username="kodi-rotated",
        password="rotated-production-secret",
    )
    assert rotated.production_policy is not None
    assert rotated.production_policy.identity_generation == 3
    assert rotated.production_policy.designation_state.value == "invalidated"
    reprobed = service.probe(target_id)
    assert reprobed.pending_observation_id is not None
    service.confirm(target_id, reprobed.pending_observation_id)
    service.confirm_production_designation(target_id, reprobed.pending_observation_id)

    verifier.version = "22.0"
    mismatch = service.probe(target_id)
    assert mismatch.verification_state.value == "identity_mismatch"
    assert mismatch.production_policy is not None
    assert mismatch.production_policy.identity_generation == 4
    assert mismatch.production_policy.lifecycle_state == "identity_mismatch"
    with pytest.raises(ExternalTargetError, match="identity_mismatch"):
        service.set_enabled(target_id, enabled=True)

    database_path = factory.kw["bind"].url.database
    assert database_path is not None
    assert b"production-secret-not-for-sqlite" not in Path(database_path).read_bytes()


def test_global_epoch_and_per_target_suspension_are_deny_only(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, _factory, _verifier = production_service
    created = _register(service)
    control = service.mutation_control()
    assert control.production_mutations_blocked is True
    assert control.policy_epoch == 1
    cleared = service.set_mutation_block(blocked=False)
    assert cleared.production_mutations_blocked is False
    assert cleared.policy_epoch == 2
    blocked = service.set_mutation_block(blocked=True)
    assert blocked.production_mutations_blocked is True
    assert blocked.policy_epoch == 3

    resumed = service.set_target_mutation_suspension(created.target_id, suspended=False)
    assert resumed.production_policy is not None
    assert resumed.production_policy.mutations_suspended is False
    suspended = service.set_target_mutation_suspension(
        created.target_id, suspended=True
    )
    assert suspended.production_policy is not None
    assert suspended.production_policy.mutations_suspended is True


def test_policy_is_mandatory_and_events_are_content_free(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    service.set_target_mutation_suspension(created.target_id, suspended=False)
    with factory() as session:
        events = session.scalars(
            select(ExternalKodiTargetEventRow).where(
                ExternalKodiTargetEventRow.target_id == str(created.target_id)
            )
        ).all()
        assert {event.event_kind for event in events} >= {
            "production_target_registered",
            "production_mutations_resumed",
        }
        serialized = "\n".join(
            f"{event.event_kind}:{event.classification}:{event.observation_id}"
            for event in events
        )
        assert "production-secret-not-for-sqlite" not in serialized

    with factory.begin() as session:
        policy = session.get(ExternalProductionPolicyRow, str(created.target_id))
        assert policy is not None
        session.delete(policy)
    with pytest.raises(ExternalTargetError, match="production_policy_missing"):
        service.get(created.target_id)


def test_production_rejects_development_capability_maximum(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, _factory, _verifier = production_service
    with pytest.raises(ExternalTargetError, match="endpoint_not_allowed"):
        service.register(
            logical_name="Invalid production",
            protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
            host="192.168.50.25",
            port=8080,
            authentication_mode=ExternalAuthenticationMode.NONE,
            configured_capabilities={"target_inspect", "workspace_deployment"},
        )

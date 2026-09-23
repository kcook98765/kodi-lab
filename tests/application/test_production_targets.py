from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

import kodi_lab.application.external_targets as external_targets_module
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
    ExternalTargetRead,
)
from kodi_lab.application.mcp_administration import (
    PRODUCTION_REMOTE_KODI_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.production_policy import advance_production_policy
from kodi_lab.domain import (
    ExternalAuthenticationMode,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ExternalProbeError,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalKodiTargetEventRow,
    ExternalProductionPolicyRow,
    McpTargetPresetAssignmentRow,
    ProductionMutationControlRow,
)
from kodi_lab.storage.secrets import ExternalCredentialStore, ExternalKodiCredential


class FakeProductionVerifier:
    def __init__(self) -> None:
        self.name = "Kodi"
        self.version = "21.3"
        self.error: str | None = None

    def probe(
        self,
        endpoint: ValidatedExternalEndpoint,
        credential: ExternalKodiCredential | None,
        configured: frozenset[str],
    ) -> ExternalKodiProbeResult:
        if self.error is not None:
            raise ExternalProbeError(self.error)
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
    observation_id = observed.pending_observation_id
    native = service.confirm(created.target_id, observation_id)
    designated = service.confirm_production_designation(
        created.target_id, observed.pending_observation_id
    )
    return native, designated


def _revision_state(
    factory: sessionmaker[Session], target_id: UUID
) -> tuple[int, int, int]:
    with factory() as session:
        row = session.execute(
            text(
                "SELECT p.policy_revision,e.production_policy_revision,"
                "g.maximum_policy_revision FROM external_production_policies p "
                "JOIN external_kodi_targets e ON e.target_id=p.target_id "
                "JOIN external_production_policy_revision_guards g "
                "ON g.target_id=p.target_id WHERE p.target_id=:target"
            ),
            {"target": str(target_id)},
        ).one()
    return row[0], row[1], row[2]


def _designation_event_count(factory: sessionmaker[Session], target_id: UUID) -> int:
    with factory() as session:
        return len(
            session.scalars(
                select(ExternalKodiTargetEventRow).where(
                    ExternalKodiTargetEventRow.target_id == str(target_id),
                    ExternalKodiTargetEventRow.event_kind
                    == "production_designation_confirmed",
                )
            ).all()
        )


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


def test_designation_is_policy_only_and_enablement_rebinds_native_revision(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    native = service.confirm(created.target_id, observed.pending_observation_id)
    assert native.production_policy is not None
    revision = native.production_policy.policy_revision
    generation = native.production_policy.identity_generation
    assert _revision_state(factory, created.target_id) == (
        revision,
        revision,
        revision,
    )

    designated = service.confirm_production_designation(
        created.target_id, observed.pending_observation_id
    )

    assert designated.production_policy is not None
    assert designated.production_policy.designation_state.value == "confirmed"
    assert designated.production_policy.policy_revision == revision + 1
    assert designated.production_policy.identity_generation == generation
    assert designated.administrative_state.value == "disabled"
    assert designated.verification_state.value == "verified"
    assert _revision_state(factory, created.target_id) == (
        revision + 1,
        revision,
        revision + 1,
    )
    assert _designation_event_count(factory, created.target_id) == 1

    with pytest.raises(ExternalTargetError, match="production_designation_required"):
        service.confirm_production_designation(
            created.target_id, observed.pending_observation_id
        )
    assert _revision_state(factory, created.target_id) == (
        revision + 1,
        revision,
        revision + 1,
    )
    assert _designation_event_count(factory, created.target_id) == 1

    enabled = service.set_enabled(created.target_id, enabled=True)
    assert enabled.production_policy is not None
    assert enabled.production_policy.policy_revision == revision + 2
    assert _revision_state(factory, created.target_id) == (
        revision + 2,
        revision + 2,
        revision + 2,
    )


def test_designation_failure_rolls_back_policy_binding_high_water_and_event(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    native = service.confirm(created.target_id, observed.pending_observation_id)
    assert native.production_policy is not None
    before = _revision_state(factory, created.target_id)
    before_events = _designation_event_count(factory, created.target_id)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER inject_designation_event_failure BEFORE INSERT ON "
                "external_kodi_target_events WHEN NEW.event_kind="
                "'production_designation_confirmed' BEGIN SELECT "
                "RAISE(ABORT,'injected_designation_event_failure'); END"
            )
        )
    try:
        with pytest.raises(IntegrityError, match="injected_designation_event_failure"):
            service.confirm_production_designation(
                created.target_id, observed.pending_observation_id
            )
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER inject_designation_event_failure"))

    current = service.get(created.target_id)
    assert current.production_policy is not None
    assert current.production_policy.designation_state.value == "pending_confirmation"
    assert _revision_state(factory, created.target_id) == before
    assert _designation_event_count(factory, created.target_id) == before_events


def test_concurrent_designation_has_one_winner_and_one_stale_loser(
    production_service: ProductionServiceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    observed = service.probe(created.target_id)
    assert observed.pending_observation_id is not None
    observation_id = observed.pending_observation_id
    native = service.confirm(created.target_id, observation_id)
    assert native.production_policy is not None
    revision = native.production_policy.policy_revision
    barrier = Barrier(2)
    advance = advance_production_policy

    def synchronized_advance(
        policy: ExternalProductionPolicyRow, *, now: datetime
    ) -> None:
        barrier.wait(timeout=5)
        advance(policy, now=now)

    monkeypatch.setattr(
        external_targets_module,
        "advance_production_policy",
        synchronized_advance,
    )

    def designate() -> str:
        try:
            service.confirm_production_designation(created.target_id, observation_id)
        except StaleDataError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: designate(), range(2)))

    assert sorted(outcomes) == ["committed", "stale"]
    assert _revision_state(factory, created.target_id) == (
        revision + 1,
        revision,
        revision + 1,
    )
    assert _designation_event_count(factory, created.target_id) == 1


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


def test_observation_revision_semantics_are_exact_and_generation_is_independent(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, _factory, verifier = production_service
    created = _register(service)
    assert created.production_policy is not None
    assert created.production_policy.policy_revision == 1

    first = service.probe(created.target_id)
    assert first.production_policy is not None
    assert first.production_policy.policy_revision == 2
    assert first.production_policy.identity_generation == 1
    repeated = service.probe(created.target_id)
    assert repeated.production_policy is not None
    assert repeated.production_policy.policy_revision == 3
    assert repeated.production_policy.identity_generation == 1

    unchanged = service.set_enabled(created.target_id, enabled=False)
    assert unchanged.production_policy is not None
    assert unchanged.production_policy.policy_revision == 3

    rotated = service.rotate_credentials(
        created.target_id,
        authentication_mode=ExternalAuthenticationMode.USERNAME_PASSWORD,
        username="replacement",
        password="replacement-secret",
    )
    assert rotated.production_policy is not None
    assert rotated.production_policy.policy_revision == 4
    assert rotated.production_policy.identity_generation == 2
    assert rotated.production_policy.designation_state.value == "invalidated"

    reverified_observation = service.probe(created.target_id)
    assert reverified_observation.production_policy is not None
    assert reverified_observation.production_policy.policy_revision == 5
    assert reverified_observation.production_policy.identity_generation == 2
    assert (
        reverified_observation.production_policy.designation_state.value
        == "pending_native_identity"
    )

    verifier.error = "target_unreachable"
    with pytest.raises(ExternalTargetError, match="target_unreachable"):
        service.probe(created.target_id)
    failed = service.get(created.target_id)
    assert failed.production_policy is not None
    assert failed.production_policy.policy_revision == 6
    assert failed.production_policy.identity_generation == 2
    assert failed.pending_observation_id is None


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


def test_deactivation_and_preset_cleanup_advance_one_revision(
    production_service: ProductionServiceFixture,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    administration = McpAdministrationService(factory)
    administration.assign_target_preset(
        created.target_id, PRODUCTION_REMOTE_KODI_PRESET_ID
    )
    assigned = service.get(created.target_id)
    assert assigned.production_policy is not None
    before = assigned.production_policy.policy_revision

    deactivated = service.deactivate(created.target_id)

    assert deactivated.production_policy is not None
    assert deactivated.production_policy.policy_revision == before + 1
    with factory() as session:
        assert session.get(McpTargetPresetAssignmentRow, str(created.target_id)) is None


def test_concurrent_policy_updates_have_one_revision_winner(
    production_service: ProductionServiceFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _development, factory, _verifier = production_service
    created = _register(service)
    fixed_now = datetime(2026, 9, 22, 20, tzinfo=UTC)
    monkeypatch.setattr(service, "_clock", lambda: fixed_now)
    barrier = Barrier(2)
    advance = advance_production_policy

    def synchronized_advance(
        policy: ExternalProductionPolicyRow, *, now: datetime
    ) -> None:
        barrier.wait(timeout=5)
        advance(policy, now=now)

    monkeypatch.setattr(
        external_targets_module,
        "advance_production_policy",
        synchronized_advance,
    )

    def resume() -> str:
        try:
            service.set_target_mutation_suspension(created.target_id, suspended=False)
        except StaleDataError:
            return "stale"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: resume(), range(2)))

    assert sorted(outcomes) == ["committed", "stale"]
    current = service.get(created.target_id)
    assert current.production_policy is not None
    assert current.production_policy.policy_revision == 2
    assert current.production_policy.mutations_suspended is False
    with factory() as session:
        events = session.scalars(
            select(ExternalKodiTargetEventRow).where(
                ExternalKodiTargetEventRow.target_id == str(created.target_id),
                ExternalKodiTargetEventRow.event_kind == "production_mutations_resumed",
            )
        ).all()
    assert len(events) == 1


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

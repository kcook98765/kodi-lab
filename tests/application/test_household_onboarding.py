from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.external_onboarding import ExternalKodiOnboardingService
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.household_onboarding import (
    HouseholdKodiOnboardingService,
    HouseholdOnboardingResult,
    HouseholdResumeRequest,
    HouseholdStartRequest,
    HouseholdTransportDecision,
)
from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.production_inspection import (
    PRODUCTION_VISUAL_PRESET_ID,
    ProductionInspectionError,
    evaluate_production_inspection_state,
)
from kodi_lab.application.screenshot_preferences import ScreenshotPreferenceService
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.bridge import ExternalBridgeError, otp_bootstrap_request_id
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetEventRow,
    ExternalKodiTargetRow,
    ExternalSecretRetirementRow,
    HouseholdOnboardingRow,
    McpClientTargetGrantRow,
    McpProductionTargetScopeRow,
    McpTargetPresetAssignmentRow,
    ProductionBridgeHttpAcknowledgementRow,
    ReviewedBridgeBuildRow,
    TargetRow,
)
from kodi_lab.storage.secrets import SecretStoreError
from tests.application.test_external_onboarding import (
    _code,
    _port,
)
from tests.application.test_external_onboarding import (
    live_application as _base_live_application,
)
from tests.application.test_production_bridges import _Verifier

live_application = _base_live_application


@dataclass
class Environment:
    service: HouseholdKodiOnboardingService
    targets: ExternalTargetAdministrationService
    bridges: ExternalBridgeService
    sessions: sessionmaker[Session]
    bridge: dict[str, Any]
    request: HouseholdStartRequest
    otp_file: Path
    admin: McpAdministrationService

    def start(self) -> HouseholdOnboardingResult:
        return self.service.start(self.request)

    def connect(
        self, first: HouseholdOnboardingResult | None = None
    ) -> HouseholdOnboardingResult:
        first = first or self.start()
        assert first.transport is not None
        return self.service.resume(
            self.request.operation_id,
            HouseholdResumeRequest(
                code=self.request.code,
                transport_decision=HouseholdTransportDecision(
                    disclosure_token=first.transport.disclosure_token,
                    accept_http_risk=True,
                ),
            ),
        )


@pytest.fixture
def env(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    tmp_path: Path,
) -> Environment:
    validation, development, bridges, bridge, otp_file = live_application
    sessions = development._session_factory
    targets = ExternalTargetAdministrationService(
        sessions,
        endpoint_policy=development._endpoint_policy,
        credential_store=development._credential_store,
        verifier=cast(Any, _Verifier()),
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    admin = McpAdministrationService(sessions)
    admin.create_client(
        display_name="Existing reader", preset_id=PRODUCTION_VISUAL_PRESET_ID
    )
    bridges.register_reviewed_build(
        semantic_fingerprint=bridge["BUILD_FINGERPRINT"],
        zip_sha256="a" * 64,
        addon_id=bridge["ADDON_ID"],
        addon_version=bridge["ADDON_VERSION"],
        protocol_version="1",
        capabilities=tuple(bridge["CAPABILITIES"]),
        supported_kodi_versions=("21.3",),
        source_reference="independent-test-review",
    )
    service = HouseholdKodiOnboardingService(
        sessions,
        targets=targets,
        bridges=bridges,
        validation=validation,
        administration=admin,
        lock_root=tmp_path / "locks",
    )
    return Environment(
        service,
        targets,
        bridges,
        sessions,
        bridge,
        HouseholdStartRequest(
            operation_id=uuid4(),
            name="Living room",
            address="127.0.0.1",
            code=SecretStr(_code(bridge)),
            bridge_port=_port(bridges),
        ),
        otp_file,
        admin,
    )


def test_household_safe_connected_defaults_and_single_use(env: Environment) -> None:
    result = env.connect()
    assert result.state == "connected", result
    assert result.target_id is not None
    target = env.targets.get(result.target_id)
    assert target.target_class == "external_production"
    assert target.administrative_state is ExternalAdministrativeState.ENABLED
    assert set(target.configured_capabilities) == {
        "target_inspect",
        "addon_inventory",
        "screenshot",
    }
    assert (
        target.production_policy is not None
        and target.production_policy.mutations_suspended
    )
    preset = env.admin.get_target_policy(result.target_id).preset
    assert preset is not None and preset.id == PRODUCTION_VISUAL_PRESET_ID
    assert ScreenshotPreferenceService(env.sessions).read(result.target_id).enabled
    bridge = env.bridges.status(result.target_id)
    assert bridge.pairing_state is ExternalBridgePairingState.PAIRED
    assert bridge.reviewed_build_id is not None
    assert env.bridges.screenshot_available(result.target_id)
    assert not env.bridges.deployment_available(result.target_id)
    with env.sessions() as session:
        assert session.scalars(select(McpClientTargetGrantRow)).all() == []
        assert session.scalars(select(McpProductionTargetScopeRow)).all() == []
        native = evaluate_production_inspection_state(
            session,
            result.target_id,
            required_capabilities=frozenset({"addon_inventory"}),
        )
        assert "semantic_remote" not in native.capabilities
        assert "workspace_deployment" not in native.capabilities
    assert json.loads(env.otp_file.read_text())["code"] is None
    assert env.request.code.get_secret_value() not in result.model_dump_json()


def test_explicit_staged_consent_and_partial_agent_denial(env: Environment) -> None:
    first = env.start()
    assert (
        first.state == "requires_action" and first.code == "transport_consent_required"
    )
    assert first.transport is not None and "unencrypted HTTP" in first.transport.message
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )
    assert first.target_id is not None
    with (
        env.sessions() as session,
        pytest.raises(
            ProductionInspectionError, match="household_onboarding_incomplete"
        ),
    ):
        evaluate_production_inspection_state(
            session,
            first.target_id,
            required_capabilities=frozenset({"target_inspect"}),
        )
    declined = env.service.resume(
        env.request.operation_id,
        HouseholdResumeRequest(
            transport_decision=HouseholdTransportDecision(
                disclosure_token=first.transport.disclosure_token,
                accept_http_risk=False,
            ),
            code=env.request.code,
        ),
    )
    assert declined.code == "transport_consent_required"
    stale = env.service.resume(
        env.request.operation_id,
        HouseholdResumeRequest(
            transport_decision=HouseholdTransportDecision(
                disclosure_token="0" * 64, accept_http_risk=True
            ),
            code=env.request.code,
        ),
    )
    assert stale.code == "transport_disclosure_changed"
    assert env.connect(first).state == "connected"


@pytest.mark.parametrize("retired", [False, True])
def test_unknown_or_retired_build_does_not_consume_otp(
    env: Environment, retired: bool
) -> None:
    with env.sessions.begin() as session:
        row = session.scalar(select(ReviewedBridgeBuildRow))
        assert row is not None
        if retired:
            row.state = "retired"
            row.retired_by = "test_admin"
            row.retired_at = row.reviewed_at
        else:
            row.semantic_fingerprint = "0" * 64
    result = env.connect()
    assert (
        result.state == "requires_action" and result.code == "bridge_build_not_reviewed"
    )
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )
    assert result.target_id is not None
    assert (
        env.bridges.status(result.target_id).pairing_state
        is ExternalBridgePairingState.UNPAIRED
    )


def test_lost_initial_and_final_responses_retry_same_operation(
    env: Environment,
) -> None:
    first = env.start()
    retry = env.start()
    assert first == retry
    connected = env.connect(retry)
    assert connected.state == "connected"
    assert env.start() == connected
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest())
        == connected
    )
    with env.sessions() as session:
        assert len(session.scalars(select(TargetRow)).all()) == 1
        assert len(session.scalars(select(HouseholdOnboardingRow)).all()) == 1


@pytest.mark.parametrize("committed", [False, True])
def test_lost_commit_recovers_without_bootstrap(
    env: Environment, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    client = env.bridges._client
    commit, bootstrap = client.commit_pairing, client.bootstrap_otp
    bootstrap_calls = 0
    commit_calls = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        return bootstrap(*args, **kwargs)

    def lost(*args: Any, **kwargs: Any) -> Any:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            if committed:
                commit(*args, **kwargs)
            raise ExternalBridgeError("bridge_unreachable")
        return commit(*args, **kwargs)

    monkeypatch.setattr(client, "bootstrap_otp", counted)
    monkeypatch.setattr(client, "commit_pairing", lost)
    result = env.connect()
    assert result.state == "incomplete"
    resumed = env.service.resume(env.request.operation_id, HouseholdResumeRequest())
    assert resumed.state == "connected", resumed
    assert bootstrap_calls == 1


def test_consumed_otp_without_persisted_credential_never_rebootstraps(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    bootstrap = env.bridges._client.bootstrap_otp

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return bootstrap(*args, **kwargs)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise SecretStoreError("sensitive internal secret detail")

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    monkeypatch.setattr(env.bridges._secrets, "write", unavailable)
    result = env.connect()
    assert result.code == "pairing_recovery_required", result
    assert (
        env.service.resume(
            env.request.operation_id, HouseholdResumeRequest(code=env.request.code)
        ).code
        == "pairing_recovery_required"
    )
    assert calls == 1 and json.loads(env.otp_file.read_text())["code"] is None
    assert "sensitive" not in result.model_dump_json()
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    assert result.target_id is not None
    assert (
        env.targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )


def test_duplicate_and_overlapping_requests_are_serialized(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    probe = env.targets.probe

    def pause(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return probe(*args, **kwargs)

    monkeypatch.setattr(env.targets, "probe", pause)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(env.start)
        assert entered.wait(5)
        # A separate coordinator instance shares the filesystem lock.
        second = HouseholdKodiOnboardingService(
            env.sessions,
            targets=env.targets,
            bridges=env.bridges,
            validation=env.service._validation,
            administration=env.admin,
            lock_root=env.service._lock_root,
        )
        assert second.start(env.request).code == "operation_in_progress"
        other = env.request.model_copy(update={"operation_id": uuid4()})
        assert second.start(other).code == "endpoint_conflict"
        release.set()
        assert pending.result().state == "requires_action"
    assert len(env.targets.list_targets()) == 1


@pytest.mark.parametrize(
    "target_type", [TargetType.EXTERNAL_PRODUCTION, TargetType.EXTERNAL_DEVELOPMENT]
)
def test_existing_endpoint_is_never_adopted(
    env: Environment, target_type: TargetType
) -> None:
    service = ExternalTargetAdministrationService(
        env.sessions,
        endpoint_policy=env.targets._endpoint_policy,
        credential_store=env.targets._credential_store,
        verifier=cast(Any, _Verifier()),
        target_type=target_type,
    )
    target = service.register(
        logical_name="Already here",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    assert env.start().code == "endpoint_conflict"
    assert service.get(target.target_id) == target


def test_request_id_cannot_change_endpoint_or_name(env: Environment) -> None:
    env.start()
    changed = env.request.model_copy(update={"name": "Other Kodi"})
    assert env.service.start(changed).code == "operation_conflict"


def test_native_identity_change_fails_closed(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.start()
    original = env.targets._verifier.probe

    def replaced(*args: Any, **kwargs: Any) -> Any:
        from dataclasses import replace

        return replace(original(*args, **kwargs), kodi_name="Replacement")

    monkeypatch.setattr(env.targets._verifier, "probe", replaced)
    result = env.connect(first)
    assert result.code == "identity_mismatch"
    assert result.target_id is not None
    assert (
        env.targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).code
        == "identity_mismatch"
    )


def test_restart_after_registration_uses_reserved_target(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.targets.probe

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("connection lost")

    monkeypatch.setattr(env.targets, "probe", interrupted)
    assert env.start().code == "persistence_failed"
    monkeypatch.setattr(env.targets, "probe", original)
    assert env.start().state == "requires_action"
    assert len(env.targets.list_targets()) == 1


def test_screenshot_preference_retry_preserves_explicit_choice(
    env: Environment,
) -> None:
    result = env.connect()
    assert result.target_id is not None
    preference = ScreenshotPreferenceService(env.sessions)
    preference.update(
        result.target_id,
        enabled=False,
        expected_revision=1,
        administrator_reference="web_admin",
    )
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).state
        == "connected"
    )
    assert not preference.read(result.target_id).enabled


def test_native_credentials_bind_reserved_uuid_and_are_redacted(
    env: Environment,
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("a private password")}
    )
    result = env.connect()
    assert result.state == "connected", result
    assert "a private password" not in repr(env.request)
    assert "a private password" not in result.model_dump_json()
    assert result.target_id is not None
    with env.sessions() as session:
        from kodi_lab.storage.models import ExternalKodiTargetRow

        target = session.get(ExternalKodiTargetRow, str(result.target_id))
        assert target is not None and target.secret_reference is not None
        credential = env.targets._credential_store.read(
            result.target_id, UUID(target.secret_reference)
        )
        assert credential.username == "kodi"


def test_bridge_already_paired_elsewhere_is_not_adopted(env: Environment) -> None:
    endpoint = env.targets._endpoint_policy.validate(
        ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080
    )
    client = env.bridges._client
    port = env.request.bridge_port
    info = client.bootstrap_info(endpoint, bridge_port=port)
    other_target_id = uuid4()
    transaction = client.bootstrap_otp(
        endpoint,
        bridge_port=port,
        target_id=other_target_id,
        info=info,
        code=env.request.code.get_secret_value(),
        secret=b"x" * 32,
        request_id=otp_bootstrap_request_id(b"x" * 32, other_target_id),
    )
    client.commit_pairing(endpoint, bridge_port=port, transaction=transaction)
    result = env.connect()
    assert result.state == "requires_action" and result.code == "already_paired"
    assert result.target_id is not None
    assert (
        env.bridges.status(result.target_id).pairing_state
        is ExternalBridgePairingState.UNPAIRED
    )


def test_wrong_otp_can_be_explicitly_corrected(env: Environment) -> None:
    first = env.start()
    assert first.transport is not None
    result = env.service.resume(
        env.request.operation_id,
        HouseholdResumeRequest(
            code=SecretStr(
                "2222" if env.request.code.get_secret_value() != "2222" else "3333"
            ),
            transport_decision=HouseholdTransportDecision(
                disclosure_token=first.transport.disclosure_token, accept_http_risk=True
            ),
        ),
    )
    assert result.code == "wrong_code"
    assert env.connect(first).state == "connected"


def test_authenticated_modified_build_is_rejected(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    original = env.bridges._client.identity

    def modified(*args: Any, **kwargs: Any) -> Any:
        return replace(original(*args, **kwargs), build_fingerprint="0" * 64)

    monkeypatch.setattr(env.bridges._client, "identity", modified)
    result = env.connect()
    assert result.code == "identity_mismatch"
    assert result.target_id is not None
    assert (
        env.targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )


def test_stale_persisted_credential_blocks_completed_target(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = env.connect()
    assert result.state == "connected" and result.target_id is not None

    def stale(*args: Any, **kwargs: Any) -> Any:
        return b"s" * 32

    monkeypatch.setattr(env.bridges._secrets, "read", stale)
    resumed = env.service.resume(env.request.operation_id, HouseholdResumeRequest())
    assert resumed.state == "requires_action" and resumed.code == "identity_mismatch"
    assert (
        env.targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )


def test_process_loss_after_bootstrap_marker_is_fail_closed(env: Environment) -> None:
    first = env.start()
    env.service._save(env.request.operation_id, "bootstrap_started")
    result = env.service.resume(
        env.request.operation_id, HouseholdResumeRequest(code=env.request.code)
    )
    assert result.code == "pairing_recovery_required"
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )
    assert result.target_id == first.target_id


def test_explicit_administrator_disable_is_not_undone(env: Environment) -> None:
    first = env.start()
    assert first.target_id is not None
    env.targets.set_enabled(first.target_id, enabled=False)
    result = env.connect(first)
    assert result.code == "administration_changed"
    assert (
        env.targets.get(first.target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )


def test_final_publication_rejects_concurrent_policy_change(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.service._complete

    def changed(operation_id: UUID, native: Any, bridge: Any) -> Any:
        env.targets.set_target_mutation_suspension(native.target_id, suspended=False)
        return original(operation_id, native, bridge)

    monkeypatch.setattr(env.service, "_complete", changed)
    result = env.connect()
    assert result.code == "administration_changed"
    assert result.target_id is not None
    with env.sessions() as session:
        row = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert row is not None and row.stage == "blocked"
    assert (
        env.targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )


def test_administrator_deactivation_keeps_structured_action_result(
    env: Environment,
) -> None:
    first = env.start()
    assert first.target_id is not None
    env.targets.deactivate(first.target_id)
    result = env.connect(first)
    assert result.state == "requires_action"
    assert (
        env.targets.get(first.target_id).administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )


def test_abandon_releases_reservation_without_adopting_retired_target(
    env: Environment,
) -> None:
    first = env.start()
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    fresh = env.service.start(env.request.model_copy(update={"operation_id": uuid4()}))
    assert fresh.state == "requires_action" and fresh.target_id != first.target_id
    assert first.target_id is not None
    assert (
        env.targets.get(first.target_id).administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).code
        == "abandoned"
    )


def test_missing_encryption_key_is_detected_before_otp_consumption(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.bridges._secrets, "_master_key", None)
    result = env.connect()
    assert result.code == "bridge_credential_store_unavailable"
    assert result.state == "requires_action"
    assert (
        json.loads(env.otp_file.read_text())["code"]
        == env.request.code.get_secret_value()
    )


def _reverify_endpoint(
    env: Environment, target_id: UUID, port: int = 8081, *, confirm: bool = True
) -> None:
    from kodi_lab.kodi.external import ExternalEndpointPolicy

    env.targets._endpoint_policy = ExternalEndpointPolicy(
        allowed_ports=(8080, 8081), test_loopback_enabled=True
    )
    env.targets.set_enabled(target_id, enabled=False)
    env.targets.edit_endpoint(
        target_id,
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=port,
    )
    if not confirm:
        return
    probed = env.targets.probe(target_id)
    assert probed.pending_observation_id is not None
    env.targets.confirm(target_id, probed.pending_observation_id)
    env.targets.confirm_production_designation(
        target_id,
        probed.pending_observation_id,
        administrator_reference="other_admin",
    )
    env.targets.set_enabled(target_id, enabled=True)


def _assert_unpublished(env: Environment, target_id: UUID) -> None:
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage != "connected"
        assert session.scalars(select(McpClientTargetGrantRow)).all() == []
        assert session.scalars(select(McpProductionTargetScopeRow)).all() == []
        with pytest.raises(ProductionInspectionError):
            evaluate_production_inspection_state(
                session,
                target_id,
                required_capabilities=frozenset({"target_inspect"}),
            )


@pytest.mark.parametrize(
    "change", ["endpoint", "unverified_endpoint", "policy_revision"]
)
def test_original_consent_endpoint_race_rejected_before_bootstrap(
    env: Environment, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("private password")}
    )
    first = env.start()
    assert first.target_id is not None and first.transport is not None
    assert first.transport.kodi_endpoint.endswith(":8080")
    with env.sessions() as session:
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None
        credential = native.secret_reference
        policy = session.get(McpTargetPresetAssignmentRow, str(first.target_id))
        assert policy is not None
        assignment = policy.assigned_at
    original = env.bridges.acknowledge_production_http_risk

    def changed(target_id: UUID, **kwargs: Any) -> Any:
        # The original fault injection: edit/reconfirm after token acceptance.
        with ThreadPoolExecutor() as pool:
            if change != "policy_revision":
                pool.submit(
                    _reverify_endpoint,
                    env,
                    target_id,
                    confirm=change != "unverified_endpoint",
                ).result(timeout=5)
            else:
                pool.submit(
                    env.targets.set_target_mutation_suspension,
                    target_id,
                    suspended=False,
                ).result(timeout=5)
                pool.submit(
                    env.targets.set_target_mutation_suspension,
                    target_id,
                    suspended=True,
                ).result(timeout=5)
        return original(target_id, **kwargs)

    monkeypatch.setattr(env.bridges, "acknowledge_production_http_risk", changed)
    result = env.connect(first)
    assert result.state == "requires_action"
    assert result.code == "onboarding_binding_changed"
    assert env.targets.get(first.target_id).port == (
        8080 if change == "policy_revision" else 8081
    )
    assert json.loads(env.otp_file.read_text())["code"] is not None
    with env.sessions() as session:
        assert (
            session.scalars(select(ProductionBridgeHttpAcknowledgementRow)).all() == []
        )
        assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None and native.secret_reference == credential
        policy = session.get(McpTargetPresetAssignmentRow, str(first.target_id))
        assert policy is not None and policy.assigned_at == assignment
    _assert_unpublished(env, first.target_id)


@pytest.mark.parametrize("change", ["endpoint", "policy_revision"])
def test_final_publication_rejects_reverified_endpoint_in_fresh_snapshot(
    env: Environment, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = env.service._complete

    def changed(operation_id: UUID, native: Any, bridge: Any) -> None:
        if change == "endpoint":
            _reverify_endpoint(env, native.target_id)
        else:
            env.targets.set_target_mutation_suspension(
                native.target_id, suspended=False
            )
            env.targets.set_target_mutation_suspension(native.target_id, suspended=True)
        # Even matching fresh snapshots must retain the original operation binding.
        original(
            operation_id,
            env.targets.verify_current_identity(native.target_id),
            env.bridges.status(native.target_id),
        )

    monkeypatch.setattr(env.service, "_complete", changed)
    result = env.connect()
    assert result.code == "onboarding_binding_changed"
    assert result.target_id is not None
    assert env.targets.get(result.target_id).port == (
        8081 if change == "endpoint" else 8080
    )
    with env.sessions() as session:
        acknowledgements = session.scalars(
            select(ProductionBridgeHttpAcknowledgementRow)
        ).all()
        assert len(acknowledgements) == 1 and acknowledgements[0].native_port == 8080
    _assert_unpublished(env, result.target_id)


def test_endpoint_change_after_acknowledgement_cannot_start_bootstrap(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.start()
    original = env.bridges.acknowledge_production_http_risk

    def changed(target_id: UUID, **kwargs: Any) -> Any:
        result = original(target_id, **kwargs)
        _reverify_endpoint(env, target_id)
        return result

    monkeypatch.setattr(env.bridges, "acknowledge_production_http_risk", changed)
    result = env.connect(first)
    assert result.state == "requires_action", result
    assert result.target_id is not None
    assert json.loads(env.otp_file.read_text())["code"] is not None
    assert (
        env.bridges.status(result.target_id).pairing_state
        is ExternalBridgePairingState.UNPAIRED
    )
    _assert_unpublished(env, result.target_id)


@pytest.mark.parametrize("change", ["policy_revision", "identity_generation"])
def test_original_native_binding_cannot_be_rebound_by_new_consent(
    env: Environment, change: str
) -> None:
    first = env.start()
    assert first.target_id is not None
    if change == "policy_revision":
        env.targets.set_target_mutation_suspension(first.target_id, suspended=False)
        env.targets.set_target_mutation_suspension(first.target_id, suspended=True)
    else:
        _reverify_endpoint(env, first.target_id)
        _reverify_endpoint(env, first.target_id, port=8080)
    result = env.connect(first)
    assert result.code == "onboarding_binding_changed"
    assert result.transport is None
    assert json.loads(env.otp_file.read_text())["code"] is not None
    _assert_unpublished(env, first.target_id)


def test_consent_writer_transaction_serializes_concurrent_administrator_edit(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.start()
    assert first.target_id is not None
    reading, release, editing, edited = Event(), Event(), Event(), Event()
    require_native = env.bridges._require_native_verified
    acknowledge = env.bridges.acknowledge_production_http_risk

    def held(session: Session, target_id: UUID, **kwargs: Any) -> Any:
        result = require_native(session, target_id, **kwargs)
        if not reading.is_set():
            reading.set()
            assert release.wait(5)
        return result

    def acknowledged(target_id: UUID, **kwargs: Any) -> Any:
        result = acknowledge(target_id, **kwargs)
        assert edited.wait(5)
        return result

    def edit() -> None:
        editing.set()
        assert first.target_id is not None
        env.targets.set_enabled(first.target_id, enabled=False)
        edited.set()

    monkeypatch.setattr(env.bridges, "_require_native_verified", held)
    monkeypatch.setattr(env.bridges, "acknowledge_production_http_risk", acknowledged)
    with ThreadPoolExecutor() as pool:
        connect = pool.submit(env.connect, first)
        assert reading.wait(5)
        edit_request = pool.submit(edit)
        assert editing.wait(5)
        try:
            assert not edited.wait(0.1)
        finally:
            release.set()
        edit_request.result(timeout=5)
        result = connect.result(timeout=5)
    assert result.state == "requires_action", result
    assert json.loads(env.otp_file.read_text())["code"] is not None
    with env.sessions() as session:
        acknowledgement = session.scalar(select(ProductionBridgeHttpAcknowledgementRow))
        assert acknowledgement is not None and acknowledgement.native_port == 8080
    _assert_unpublished(env, first.target_id)


def test_original_abandon_persistence_loss_retry_releases_reservation(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("private password")}
    )

    def incomplete(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("interrupt before connected publication")

    monkeypatch.setattr(env.service, "_complete", incomplete)
    first = env.start()
    assert first.target_id is not None
    assert env.connect(first).code == "persistence_failed"
    with env.sessions() as session:
        bridge = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert bridge is not None and bridge.bridge_secret_reference is not None
        bridge_reference = UUID(bridge.bridge_secret_reference)
        assert (
            session.get(McpTargetPresetAssignmentRow, str(first.target_id)) is not None
        )
    save = env.service._save

    def lost(operation_id: UUID, stage: str, code: str | None = None) -> None:
        if stage == "abandoned":
            raise SQLAlchemyError("crash after target deactivation")
        save(operation_id, stage, code)

    monkeypatch.setattr(env.service, "_save", lost)
    assert env.service.abandon(env.request.operation_id).code == "persistence_failed"
    assert (
        env.targets.get(first.target_id).administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    fresh_request = env.request.model_copy(update={"operation_id": uuid4()})
    assert env.service.start(fresh_request).code == "endpoint_conflict"
    monkeypatch.setattr(env.service, "_save", save)
    recovered = env.service.abandon(env.request.operation_id)
    assert recovered.code == "abandoned"
    assert env.service.abandon(env.request.operation_id) == recovered
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage == "abandoned"
        bridge = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert (
            bridge is not None
            and bridge.pairing_state is ExternalBridgePairingState.REVOKED
        )
        assert bridge.bridge_secret_reference is None
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None and native.secret_reference is None
        assert session.get(McpTargetPresetAssignmentRow, str(first.target_id)) is None
        events = session.scalars(
            select(ExternalKodiTargetEventRow).where(
                ExternalKodiTargetEventRow.target_id == str(first.target_id),
                ExternalKodiTargetEventRow.event_kind == "deactivated",
            )
        ).all()
        assert len(events) == 1
    with pytest.raises(SecretStoreError):
        env.bridges._secrets.read(
            first.target_id, bridge_reference, "request_authentication"
        )
    _assert_unpublished(env, first.target_id)
    fresh = env.service.start(fresh_request)
    assert fresh.state == "requires_action" and fresh.target_id != first.target_id


@pytest.mark.parametrize(
    "phase", ["revoke", "deactivate", "invalidate_production_native_identity"]
)
@pytest.mark.parametrize("error_type", [SQLAlchemyError, SecretStoreError])
def test_abandon_cleanup_failure_preserves_reservation_for_retry(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    error_type: type[Exception],
) -> None:
    first = env.start()
    assert first.target_id is not None
    if phase == "invalidate_production_native_identity":
        env.targets.deactivate(first.target_id)
    owner = env.targets if phase == "deactivate" else env.bridges
    original = getattr(owner, phase)

    def failed(*args: Any, **kwargs: Any) -> None:
        raise error_type("cleanup interrupted")

    monkeypatch.setattr(owner, phase, failed)
    result = env.service.abandon(env.request.operation_id)
    assert result.state == "incomplete" and result.code == "persistence_failed"
    assert (
        env.service.start(env.request.model_copy(update={"operation_id": uuid4()})).code
        == "endpoint_conflict"
    )
    _assert_unpublished(env, first.target_id)
    monkeypatch.setattr(owner, phase, original)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"


def test_abandon_finishes_local_bridge_cleanup_for_already_retired_target(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.start()
    assert first.target_id is not None

    def incomplete(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("publication interrupted")

    monkeypatch.setattr(env.service, "_complete", incomplete)
    assert env.connect(first).state == "incomplete"
    env.targets.deactivate(first.target_id)
    with env.sessions() as session:
        bridge = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert (
            bridge is not None
            and bridge.pairing_state is ExternalBridgePairingState.PAIRED
        )
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with env.sessions() as session:
        bridge = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert (
            bridge is not None
            and bridge.pairing_state is ExternalBridgePairingState.REVOKED
        )
        assert (
            bridge.bridge_secret_reference is None and bridge.reviewed_build_id is None
        )
    _assert_unpublished(env, first.target_id)


def test_concurrent_and_repeated_abandonment_converges(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.start()
    assert first.target_id is not None
    entered, release = Event(), Event()
    deactivate = env.targets.deactivate

    def held(target_id: UUID) -> Any:
        entered.set()
        assert release.wait(5)
        return deactivate(target_id)

    monkeypatch.setattr(env.targets, "deactivate", held)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(env.service.abandon, env.request.operation_id)
        assert entered.wait(5)
        second = HouseholdKodiOnboardingService(
            env.sessions,
            targets=env.targets,
            bridges=env.bridges,
            validation=env.service._validation,
            administration=env.admin,
            lock_root=env.service._lock_root,
        )
        try:
            assert (
                second.abandon(env.request.operation_id).code == "operation_in_progress"
            )
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.code == "abandoned"
    assert second.abandon(env.request.operation_id) == result
    assert (
        env.targets.get(first.target_id).administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )


def _recreate_coordinator(env: Environment) -> None:
    env.bridges = ExternalBridgeService(
        env.sessions,
        endpoint_policy=env.bridges._endpoint_policy,
        secret_store=env.bridges._secrets,
        client=env.bridges._client,
    )
    env.service = HouseholdKodiOnboardingService(
        env.sessions,
        targets=env.targets,
        bridges=env.bridges,
        validation=env.service._validation,
        administration=env.admin,
        lock_root=env.service._lock_root,
    )


def _interrupt_publication(*args: Any, **kwargs: Any) -> None:
    raise SQLAlchemyError("interrupted before connected publication")


@pytest.mark.parametrize("kind", ["bridge", "kodi"])
@pytest.mark.parametrize("retired_target", [False, True])
def test_abandon_retries_durable_secret_retirement_after_restart(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    retired_target: bool,
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("private password")}
    )
    monkeypatch.setattr(env.service, "_complete", _interrupt_publication)
    first = env.start()
    assert first.target_id is not None
    assert env.connect(first).code == "persistence_failed"
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert pairing is not None and pairing.bridge_secret_reference is not None
        assert native is not None and native.secret_reference is not None
        reference = UUID(
            pairing.bridge_secret_reference
            if kind == "bridge"
            else native.secret_reference
        )
    if retired_target and kind == "bridge":
        env.targets.deactivate(first.target_id)
    store = env.bridges._secrets if kind == "bridge" else env.targets._credential_store
    retire = store.retire
    calls = 0

    def failure(value: UUID) -> None:
        nonlocal calls
        assert value == reference
        calls += 1
        raise OSError("credential unlink interrupted")

    monkeypatch.setattr(store, "retire", failure)
    if retired_target and kind == "kodi":
        with pytest.raises(OSError):
            env.targets.deactivate(first.target_id)
    for _ in range(2):
        _recreate_coordinator(env)
        result = env.service.abandon(env.request.operation_id)
        assert result.code == "persistence_failed"
        with env.sessions() as session:
            pending = session.get(ExternalSecretRetirementRow, (kind, str(reference)))
            assert pending is not None and pending.target_id == str(first.target_id)
            operation = session.get(
                HouseholdOnboardingRow, str(env.request.operation_id)
            )
            assert operation is not None and operation.stage != "abandoned"
            pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
            assert pairing is not None and pairing.bridge_secret_reference is None
            assert pairing.pairing_state is ExternalBridgePairingState.REVOKED
            assert session.scalars(select(McpClientTargetGrantRow)).all() == []
            assert session.scalars(select(McpProductionTargetScopeRow)).all() == []
        assert (
            env.service.start(
                env.request.model_copy(update={"operation_id": uuid4()})
            ).code
            == "endpoint_conflict"
        )
        if kind == "bridge":
            env.bridges._secrets.read(
                first.target_id, reference, "request_authentication"
            )
        else:
            env.targets._credential_store.read(first.target_id, reference)
    assert calls >= 2
    monkeypatch.setattr(store, "retire", retire)
    _recreate_coordinator(env)
    completed = env.service.abandon(env.request.operation_id)
    assert completed.code == "abandoned"
    assert env.service.abandon(env.request.operation_id) == completed
    assert env.targets.deactivate(first.target_id).administrative_state is (
        ExternalAdministrativeState.DEACTIVATED
    )
    with env.sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert native is not None and native.secret_reference is None
        assert session.get(McpTargetPresetAssignmentRow, str(first.target_id)) is None
        events = session.scalars(
            select(ExternalKodiTargetEventRow).where(
                ExternalKodiTargetEventRow.target_id == str(first.target_id),
                ExternalKodiTargetEventRow.event_kind == "deactivated",
            )
        ).all()
        assert len(events) == 1
    with pytest.raises(SecretStoreError):
        if kind == "bridge":
            env.bridges._secrets.read(
                first.target_id, reference, "request_authentication"
            )
        else:
            env.targets._credential_store.read(first.target_id, reference)
    fresh = env.service.start(env.request.model_copy(update={"operation_id": uuid4()}))
    assert fresh.target_id is not None and fresh.target_id != first.target_id


@pytest.mark.parametrize("kind", ["bridge", "kodi"])
def test_deleted_secret_with_failed_db_acknowledgement_recovers(
    env: Environment, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("private password")}
    )
    monkeypatch.setattr(env.service, "_complete", _interrupt_publication)
    first = env.start()
    assert first.target_id is not None and env.connect(first).state == "incomplete"
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        native = session.get(ExternalKodiTargetRow, str(first.target_id))
        assert pairing is not None and native is not None
        raw = (
            pairing.bridge_secret_reference
            if kind == "bridge"
            else native.secret_reference
        )
        assert raw is not None
        reference = UUID(raw)
    bind = env.sessions.kw["bind"]

    def failed_acknowledgement(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            statement.startswith("DELETE FROM external_secret_retirements")
            and kind in parameters
        ):
            raise SQLAlchemyError("deleted file but lost cleanup acknowledgement")

    event.listen(bind, "before_cursor_execute", failed_acknowledgement)
    try:
        assert (
            env.service.abandon(env.request.operation_id).code == "persistence_failed"
        )
        with env.sessions() as session:
            assert (
                session.get(ExternalSecretRetirementRow, (kind, str(reference)))
                is not None
            )
        assert (
            env.service.start(
                env.request.model_copy(update={"operation_id": uuid4()})
            ).code
            == "endpoint_conflict"
        )
    finally:
        event.remove(bind, "before_cursor_execute", failed_acknowledgement)
    _recreate_coordinator(env)
    # The exact file is already absent; existing retire semantics accept that.
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with env.sessions() as session:
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    with pytest.raises(SecretStoreError):
        if kind == "bridge":
            env.bridges._secrets.read(
                first.target_id, reference, "request_authentication"
            )
        else:
            env.targets._credential_store.read(first.target_id, reference)


def test_revocation_and_retirement_obligation_commit_together(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(env.service, "_complete", _interrupt_publication)
    first = env.start()
    assert first.target_id is not None and env.connect(first).state == "incomplete"
    bind = env.sessions.kw["bind"]

    def failed_insert(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO external_secret_retirements"):
            raise SQLAlchemyError("retirement journal insert failed")

    event.listen(bind, "before_cursor_execute", failed_insert)
    try:
        assert (
            env.service.abandon(env.request.operation_id).code == "persistence_failed"
        )
    finally:
        event.remove(bind, "before_cursor_execute", failed_insert)
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert (
            pairing is not None
            and pairing.pairing_state is ExternalBridgePairingState.PAIRED
        )
        assert pairing.bridge_secret_reference is not None
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    assert env.service.abandon(env.request.operation_id).code == "abandoned"


def test_shared_bridge_revoke_retries_show_once_token_cleanup(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    assert first.target_id is not None
    env.bridges.acknowledge_production_http_risk(
        first.target_id, bridge_port=env.request.bridge_port
    )
    env.bridges.generate_pairing_token(
        first.target_id, bridge_port=env.request.bridge_port
    )
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.pairing_token_reference is not None
        reference = UUID(pairing.pairing_token_reference)
    retire = env.bridges._secrets.retire

    def fail(value: UUID) -> None:
        raise OSError("token deletion failed")

    monkeypatch.setattr(env.bridges._secrets, "retire", fail)
    with pytest.raises(OSError):
        env.bridges.revoke(first.target_id)
    _recreate_coordinator(env)
    monkeypatch.setattr(env.bridges._secrets, "retire", retire)
    assert (
        env.bridges.revoke(first.target_id).pairing_state
        is ExternalBridgePairingState.REVOKED
    )
    with env.sessions() as session:
        assert (
            session.get(ExternalSecretRetirementRow, ("bridge", str(reference))) is None
        )
    with pytest.raises(SecretStoreError):
        env.bridges._secrets.read(first.target_id, reference, "pairing_token")


@pytest.mark.parametrize("lost_bootstrap", [False, True])
def test_consumed_otp_protocol_error_never_redispatches_after_restart(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
    lost_bootstrap: bool,
) -> None:
    client = env.bridges._client
    bootstrap, post, discovery = (
        client.bootstrap_otp,
        client._post_json,
        client.bootstrap_info,
    )
    calls = 0
    cached: list[Any] = []

    def replayed(*args: Any, **kwargs: Any) -> Any:
        if not cached:
            cached.append(discovery(*args, **kwargs))
        return cached[0]

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = bootstrap(*args, **kwargs)
        if lost_bootstrap:
            raise ExternalBridgeError("bridge_unreachable")
        return result

    def damaged(endpoint: Any, port: int, body: dict[str, Any]) -> Any:
        result = post(endpoint, port, body)
        assert isinstance(result, dict)
        if body["operation"] == "bridge_identity":
            result["protocol_version"] = "unsupported"
        return result

    monkeypatch.setattr(client, "bootstrap_info", replayed)
    monkeypatch.setattr(client, "bootstrap_otp", counted)
    monkeypatch.setattr(client, "_post_json", damaged)
    first = env.start()
    assert first.target_id is not None
    result = env.connect(first)
    assert result.code == "pairing_recovery_required"
    assert json.loads(env.otp_file.read_text())["code"] is None
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage == "bootstrap_started"
        assert session.get(ExternalBridgePairingRow, str(first.target_id)) is None
    monkeypatch.setattr(client, "_post_json", post)
    for _ in range(2):
        _recreate_coordinator(env)
        assert env.connect(first).code == "pairing_recovery_required"
    assert calls == 1
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).code
        == "abandoned"
    )
    _assert_unpublished(env, first.target_id)


def test_lost_bootstrap_response_recovers_with_one_dispatch(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env.bridges._client
    bootstrap = client.bootstrap_otp
    calls = 0

    def lost(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        bootstrap(*args, **kwargs)
        raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(client, "bootstrap_otp", lost)
    assert env.connect().state == "connected"
    _recreate_coordinator(env)
    assert (
        env.service.resume(env.request.operation_id, HouseholdResumeRequest()).state
        == "connected"
    )
    assert calls == 1


@pytest.mark.parametrize("code", ["bridge_unreachable", "bridge_protocol_unsupported"])
def test_definitive_predispatch_failure_can_retry(
    env: Environment, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    discovery = env.bridges._client.bootstrap_info
    bootstrap = env.bridges._client.bootstrap_otp
    calls = 0

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise ExternalBridgeError(code)

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_info", failed)
    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    first = env.start()
    assert env.connect(first).code == code
    with env.sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(env.request.operation_id))
        assert operation is not None and operation.stage == "native_verified"
    assert calls == 0 and json.loads(env.otp_file.read_text())["code"] is not None
    monkeypatch.setattr(env.bridges._client, "bootstrap_info", discovery)
    _recreate_coordinator(env)
    assert env.connect(first).state == "connected"
    assert calls == 1


def test_abandon_retires_residual_reference_after_deactivation(
    env: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = env.start()
    assert first.target_id is not None
    deactivate = env.targets.deactivate
    reference = env.bridges._secrets.write(
        first.target_id, "request_authentication", b"r" * 32
    )

    def concurrent_pairing(target_id: UUID) -> Any:
        # An administrator's pairing can publish in the revoke/deactivate gap.
        # Once deactivation commits, ordinary pairing cannot publish new trust.
        with env.sessions.begin() as session:
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
            if pairing is None:
                pairing = ExternalBridgePairingRow(
                    target_id=str(target_id),
                    pairing_state=ExternalBridgePairingState.UNPAIRED,
                    reachability_state=ExternalBridgeReachabilityState.UNKNOWN,
                    observed_capabilities_json="[]",
                    verified_capabilities_json="[]",
                )
                session.add(pairing)
            pairing.bridge_secret_reference = str(reference)
        return deactivate(target_id)

    monkeypatch.setattr(env.targets, "deactivate", concurrent_pairing)
    assert env.service.abandon(env.request.operation_id).code == "abandoned"
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(first.target_id))
        assert pairing is not None and pairing.bridge_secret_reference is None
        assert pairing.pairing_state is ExternalBridgePairingState.REVOKED
        assert session.scalars(select(ExternalSecretRetirementRow)).all() == []
    with pytest.raises(SecretStoreError):
        env.bridges._secrets.read(first.target_id, reference, "request_authentication")

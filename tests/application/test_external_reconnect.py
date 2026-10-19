"""Existing UUID/policy reconnect uses fresh consent and one owned OTP dispatch."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.external_reconnect import (
    ExternalReconnectService,
    ReconnectStartRequest,
)
from kodi_lab.application.household_onboarding import (
    HouseholdResumeRequest,
    HouseholdTransportDecision,
)
from kodi_lab.domain import ExternalAdministrativeState, ExternalBridgePairingState
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalReconnectOperationRow,
    McpClientTargetGrantRow,
    McpTargetPresetAssignmentRow,
    ProductionBridgeHttpAcknowledgementRow,
    TargetRow,
)
from kodi_lab.storage.secrets import SecretStoreError
from tests.application import test_household_onboarding as household_fixtures
from tests.application.test_external_onboarding import _code, _port
from tests.application.test_household_onboarding import Environment

env = household_fixtures.env
live_application = household_fixtures.live_application


def _service(environment: Environment, tmp_path: Path) -> ExternalReconnectService:
    return ExternalReconnectService(
        environment.sessions,
        development=environment.service._validation._targets,
        production=environment.targets,
        bridges=environment.bridges,
        lock_root=tmp_path / "reconnect-locks",
    )


def _reset(environment: Environment) -> SecretStr:
    environment.bridge["_reset_pairing"]()
    environment.bridge["_onboarding_store"]().generate_new_code(paired=False)
    return SecretStr(_code(environment.bridge))


def _decision(first: Any, code: SecretStr) -> HouseholdResumeRequest:
    assert first.transport is not None
    return HouseholdResumeRequest(
        code=code,
        transport_decision=HouseholdTransportDecision(
            disclosure_token=first.transport.disclosure_token, accept_http_risk=True
        ),
    )


def _revoked(environment: Environment) -> tuple[UUID, SecretStr]:
    connected = environment.connect()
    assert connected.target_id is not None
    environment.bridges.revoke(connected.target_id)
    return connected.target_id, _reset(environment)


def test_reconnect_retains_registration_policy_grants_and_uses_revision_three(
    env: Environment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = env.start()
    connected = env.connect(initial)
    assert connected.target_id is not None
    target_id = connected.target_id
    before = env.targets.get(target_id)
    with env.sessions() as session:
        assignments = [
            (row.target_id, row.preset_id)
            for row in session.scalars(select(McpTargetPresetAssignmentRow))
        ]
        grants = [
            (row.client_id, row.target_id)
            for row in session.scalars(select(McpClientTargetGrantRow))
        ]
    env.bridges.revoke(target_id)
    code = _reset(env)
    calls: list[int] = []
    bootstrap = env.bridges._client.bootstrap_otp

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    assert first.code == "transport_consent_required" and first.transport is not None
    assert first.transport.bridge_endpoint.endswith(f":{_port(env.bridges)}")
    assert calls == []
    assert initial.transport is not None
    stale = service.resume(target_id, operation_id, _decision(initial, code))
    assert stale.code == "transport_disclosure_changed" and calls == []
    result = service.resume(target_id, operation_id, _decision(first, code))
    assert result.state == "connected" and result.target_id == target_id
    assert calls == [1]
    after = env.targets.get(target_id)
    assert after.production_policy == before.production_policy
    assert (
        after.logical_name == before.logical_name
        and after.target_class == before.target_class
    )
    assert after.configured_capabilities == before.configured_capabilities
    with env.sessions() as session:
        assert [
            (row.target_id, row.preset_id)
            for row in session.scalars(select(McpTargetPresetAssignmentRow))
        ] == assignments
        assert [
            (row.client_id, row.target_id)
            for row in session.scalars(select(McpClientTargetGrantRow))
        ] == grants
        assert session.scalar(select(func.count()).select_from(TargetRow)) == 1
        row = session.get(ExternalReconnectOperationRow, str(operation_id))
        assert row is not None and row.stage == "connected"
        assert row.owned_bridge_reference is not None
        assert row.expected_bridge_revision == 3
        assert (
            3
            in session.scalars(
                select(ProductionBridgeHttpAcknowledgementRow.bridge_trust_revision)
            ).all()
        )
    assert (
        service.resume(target_id, operation_id, HouseholdResumeRequest()).state
        == "connected"
    )
    assert calls == [1]
    # The original Home receipt cannot replay revision-one consent and disable
    # a connection that now has an independently completed reconnect receipt.
    old = env.service.resume(env.request.operation_id, HouseholdResumeRequest())
    assert old.code == "reconnect_operation_exists"
    assert (
        env.targets.get(target_id).administrative_state
        is ExternalAdministrativeState.ENABLED
    )


def test_uncertain_final_response_recovers_owned_credential_without_reusing_otp(
    env: Environment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id, code = _revoked(env)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    bootstrap = env.bridges._client.bootstrap_otp
    calls: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)
    commit = env.bridges.commit_otp_pairing
    with monkeypatch.context() as patch:

        def lost(requested: UUID) -> Any:
            commit(requested)
            raise SQLAlchemyError("fixture lost final response")

        patch.setattr(env.bridges, "commit_otp_pairing", lost)
        assert (
            service.resume(target_id, operation_id, _decision(first, code)).code
            == "persistence_failed"
        )
    with env.sessions() as session:
        row = session.get(ExternalReconnectOperationRow, str(operation_id))
        assert row is not None and row.owned_bridge_reference is not None
    recovered = service.resume(target_id, operation_id, HouseholdResumeRequest())
    assert recovered.state == "connected" and calls == [1]


@pytest.mark.parametrize("retired", [False, True])
def test_reconnect_never_reenables_disabled_or_retired_registrations(
    env: Environment, tmp_path: Path, retired: bool
) -> None:
    target_id, _code_value = _revoked(env)
    if retired:
        env.targets.deactivate(target_id)
    else:
        env.targets.set_enabled(target_id, enabled=False)
    result = _service(env, tmp_path).start(
        target_id, ReconnectStartRequest(operation_id=uuid4())
    )
    assert result.code == ("connection_retired" if retired else "connection_disabled")
    with env.sessions() as session:
        assert session.scalars(select(ExternalReconnectOperationRow)).all() == []
    assert env.targets.get(target_id).administrative_state is (
        ExternalAdministrativeState.DEACTIVATED
        if retired
        else ExternalAdministrativeState.DISABLED
    )


def test_native_admin_change_before_otp_dispatch_is_rejected(
    env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_id, code = _revoked(env)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    original = env.bridges._client.bootstrap_info
    bootstrap_calls: list[int] = []

    def changed(*args: Any, **kwargs: Any) -> Any:
        info = original(*args, **kwargs)
        env.targets.set_enabled(target_id, enabled=False)
        return info

    monkeypatch.setattr(env.bridges._client, "bootstrap_info", changed)
    monkeypatch.setattr(
        env.bridges._client,
        "bootstrap_otp",
        lambda *_args, **_kwargs: bootstrap_calls.append(1),
    )
    result = service.resume(target_id, operation_id, _decision(first, code))
    assert result.code == "connection_disabled" and bootstrap_calls == []
    assert (
        env.targets.get(target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )


def test_manual_pairing_cannot_be_adopted_or_cancelled_by_another_operation(
    env: Environment, tmp_path: Path
) -> None:
    target_id, code = _revoked(env)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    env.bridges.acknowledge_production_http_risk(
        target_id, bridge_port=_port(env.bridges)
    )
    env.bridges.begin_otp_pairing(
        target_id, bridge_port=_port(env.bridges), code=code.get_secret_value()
    )
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        assert pairing is not None
        manual_reference = pairing.bridge_secret_reference
    result = service.resume(target_id, operation_id, HouseholdResumeRequest())
    assert result.code == "onboarding_binding_changed"
    assert service.cancel(target_id, operation_id).code == "cancelled"
    with env.sessions() as session:
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        assert (
            pairing is not None and pairing.bridge_secret_reference == manual_reference
        )
        assert pairing.pairing_state is ExternalBridgePairingState.PAIRING_PENDING


def test_duplicate_reconnect_start_resumes_the_exact_active_operation(
    env: Environment, tmp_path: Path
) -> None:
    target_id, _code_value = _revoked(env)
    service = _service(env, tmp_path)
    first = service.start(target_id, ReconnectStartRequest(operation_id=uuid4()))
    duplicate = service.start(target_id, ReconnectStartRequest(operation_id=uuid4()))
    assert duplicate.operation_id == first.operation_id
    with env.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ExternalReconnectOperationRow)
            )
            == 1
        )


def test_development_reconnect_keeps_class_and_does_not_assign_home_policy(
    env: Environment, tmp_path: Path
) -> None:
    validation = env.service._validation
    first = validation.onboard(
        name="Development Kodi",
        address="127.0.0.1",
        code=_code(env.bridge),
        bridge_port=_port(env.bridges),
    )
    env.bridges.revoke(first.target_id)
    code = _reset(env)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    disclosure = service.start(
        first.target_id, ReconnectStartRequest(operation_id=operation_id)
    )
    result = service.resume(first.target_id, operation_id, _decision(disclosure, code))
    assert result.state == "connected" and result.target_class == "external_development"
    assert result.target_id == first.target_id
    with env.sessions() as session:
        assert session.get(McpTargetPresetAssignmentRow, str(first.target_id)) is None


def test_healthy_pairing_can_be_rechecked_without_new_otp_or_policy_changes(
    env: Environment, tmp_path: Path
) -> None:
    connected = env.connect()
    assert connected.target_id is not None
    previous = env.bridges.status(connected.target_id)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    result = service.start(
        connected.target_id, ReconnectStartRequest(operation_id=operation_id)
    )
    assert result.state == "connected"
    assert (
        env.bridges.status(connected.target_id).production_bridge_trust_revision
        == previous.production_bridge_trust_revision
    )
    assert (
        service.resume(
            connected.target_id, operation_id, HouseholdResumeRequest()
        ).state
        == "connected"
    )


def test_explicit_pairing_replacement_stays_in_guided_flow(
    env: Environment, tmp_path: Path
) -> None:
    connected = env.connect()
    assert connected.target_id is not None
    target_id = connected.target_id
    before = env.targets.get(target_id)
    env.bridges.mark_needs_reverification(target_id)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    assert first.code == "replace_pairing_required"
    consent = service.replace_pairing(target_id, operation_id)
    assert consent.code == "transport_consent_required"
    result = service.resume(target_id, operation_id, _decision(consent, _reset(env)))
    assert result.state == "connected"
    assert env.targets.get(target_id).production_policy == before.production_policy


def test_revoke_exact_refuses_another_pairing_reference(env: Environment) -> None:
    connected = env.connect()
    assert connected.target_id is not None
    before = env.bridges.status(connected.target_id)
    with pytest.raises(ExternalBridgeServiceError, match="transport_binding_changed"):
        env.bridges.revoke_exact(connected.target_id, str(uuid4()))
    assert env.bridges.status(connected.target_id) == before


def test_unpublished_credential_never_causes_a_second_otp_attempt(
    env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_id, code = _revoked(env)
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    bootstrap = env.bridges._client.bootstrap_otp
    calls: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return bootstrap(*args, **kwargs)

    monkeypatch.setattr(env.bridges._client, "bootstrap_otp", counted)

    def fail_write(*_args: Any, **_kwargs: Any) -> Any:
        raise SecretStoreError("fixture protected store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(env.bridges._secrets, "write", fail_write)
        assert (
            service.resume(target_id, operation_id, _decision(first, code)).code
            == "pairing_recovery_required"
        )
    assert (
        service.resume(target_id, operation_id, _decision(first, code)).code
        == "pairing_recovery_required"
    )
    assert calls == [1]
    assert (
        env.targets.get(target_id).administrative_state
        is ExternalAdministrativeState.ENABLED
    )


def test_authenticated_native_credentials_are_reused_without_replacement(
    env: Environment, tmp_path: Path
) -> None:
    env.request = env.request.model_copy(
        update={"username": "kodi", "password": SecretStr("fixture-password")}
    )
    target_id, code = _revoked(env)
    from kodi_lab.storage.models import ExternalKodiTargetRow

    with env.sessions() as session:
        original = session.get(ExternalKodiTargetRow, str(target_id))
        assert original is not None and original.secret_reference is not None
        reference = original.secret_reference
    service = _service(env, tmp_path)
    operation_id = uuid4()
    first = service.start(target_id, ReconnectStartRequest(operation_id=operation_id))
    assert (
        service.resume(target_id, operation_id, _decision(first, code)).state
        == "connected"
    )
    with env.sessions() as session:
        current = session.get(ExternalKodiTargetRow, str(target_id))
        assert current is not None and current.secret_reference == reference

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.external_onboarding import (
    ExternalKodiOnboardingError,
    ExternalKodiOnboardingService,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetError,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalProtocolKind,
    TargetType,
)
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.external import ExternalEndpointPolicy, ExternalProbeError
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.secrets import (
    ExternalBridgeSecretStore,
    ExternalCredentialStore,
    SecretStoreError,
)
from tests.application.test_external_targets import FakeVerifier
from tests.kodi.test_bridge_otp_bootstrap import _server
from tests.kodi.test_narrow_bridge_addon import _load


@pytest.fixture
def live_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[
    tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ]
]:
    state = tmp_path / "state.json"
    onboarding = tmp_path / "onboarding.json"
    bridge = _load(monkeypatch, state_path=state, onboarding_path=onboarding)
    addon = SimpleNamespace(getSettingBool=lambda name: name == "enabled")
    monkeypatch.setattr(sys.modules["xbmcaddon"], "Addon", lambda _id: addon)
    bridge["_load_state"]()
    bridge["_onboarding_store"]().initialize(paired=False)
    engine = create_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    kodi_policy = ExternalEndpointPolicy(
        allowed_ports=(8080,), test_loopback_enabled=True
    )
    with _server(bridge) as port:
        bridge_policy = ExternalEndpointPolicy(
            allowed_ports=(port,), test_loopback_enabled=True
        )
        targets = ExternalTargetAdministrationService(
            factory,
            endpoint_policy=kodi_policy,
            credential_store=ExternalCredentialStore(
                tmp_path / "kodi-secrets", master_key=b"k" * 32, key_version=1
            ),
            verifier=FakeVerifier(),  # type: ignore[arg-type]
        )
        bridges = ExternalBridgeService(
            factory,
            endpoint_policy=bridge_policy,
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "bridge-secrets", master_key=b"b" * 32, key_version=1
            ),
        )
        service = ExternalKodiOnboardingService(
            factory,
            targets=targets,
            bridges=bridges,
            kodi_endpoint_policy=kodi_policy,
            bridge_endpoint_policy=bridge_policy,
        )
        yield service, targets, bridges, bridge, onboarding
    engine.dispose()


def _code(bridge: dict[str, Any]) -> str:
    value = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert isinstance(value, str)
    return value


def _onboard(
    service: ExternalKodiOnboardingService,
    code: str,
    default_bridge_port: int,
    **kwargs: Any,
) -> Any:
    return service.onboard(
        name=kwargs.pop("name", "Native Kodi"),
        address=kwargs.pop("address", "127.0.0.1"),
        code=code,
        kodi_port=kwargs.pop("kodi_port", 8080),
        bridge_port=kwargs.pop("bridge_port", default_bridge_port),
        **kwargs,
    )


def _port(bridges: ExternalBridgeService) -> int:
    return next(iter(bridges._endpoint_policy._allowed_ports))


def test_happy_path_stages_disabled_target_then_enables_after_commit(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, targets, bridges, bridge, onboarding = live_application
    original_begin = bridges.begin_otp_pairing
    original_commit = bridges.commit_otp_pairing

    def begin(*args: Any, **kwargs: Any) -> Any:
        assert (
            targets.get(args[0]).administrative_state
            is ExternalAdministrativeState.DISABLED
        )
        return original_begin(*args, **kwargs)

    def commit(target_id: Any) -> Any:
        assert (
            targets.get(target_id).administrative_state
            is ExternalAdministrativeState.DISABLED
        )
        return original_commit(target_id)

    monkeypatch.setattr(bridges, "begin_otp_pairing", begin)
    monkeypatch.setattr(bridges, "commit_otp_pairing", commit)
    code = _code(bridge)
    result = _onboard(service, code, _port(bridges))
    assert result.target_class == "external_development"
    assert result.enabled and result.verified and result.paired
    assert result.kodi_version == "21.3"
    assert result.bridge_version == "1.1.14"
    assert (
        targets.get(result.target_id).administrative_state
        is ExternalAdministrativeState.ENABLED
    )
    assert (
        bridges.status(result.target_id).pairing_state
        is ExternalBridgePairingState.PAIRED
    )
    assert json.loads(onboarding.read_text())["code"] is None
    assert service.resume(result.target_id) == result
    assert code not in repr(result)
    with pytest.raises(ExternalKodiOnboardingError, match="target_already_exists"):
        _onboard(service, "2222", _port(bridges))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"address": "public.example"}, "invalid_address"),
        ({"address": "8.8.8.8"}, "invalid_address"),
        ({"kodi_port": 9090}, "invalid_port"),
        ({"bridge_port": 9090}, "invalid_port"),
        ({"username": "kodi"}, "invalid_kodi_credentials"),
    ],
)
def test_invalid_input_creates_no_target(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    changes: dict[str, Any],
    expected: str,
) -> None:
    service, targets, bridges, bridge, _onboarding = live_application
    with pytest.raises(ExternalKodiOnboardingError, match=expected):
        _onboard(service, _code(bridge), _port(bridges), **changes)
    assert targets.list_targets() == []


@pytest.mark.parametrize("code", ["1234", "AAAAA", "A0B2", "a234", ""])
def test_invalid_otp_shape_creates_no_target(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    code: str,
) -> None:
    service, targets, bridges, _bridge, _onboarding = live_application
    with pytest.raises(ExternalKodiOnboardingError, match="otp_invalid_format"):
        _onboard(service, code, _port(bridges))
    assert targets.list_targets() == []


def test_wrong_otp_retires_disabled_target_and_preserves_code(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
) -> None:
    service, targets, bridges, bridge, onboarding = live_application
    code = _code(bridge)
    wrong = "2222" if code != "2222" else "3333"
    with pytest.raises(ExternalKodiOnboardingError, match="otp_invalid"):
        _onboard(service, wrong, _port(bridges))
    assert (
        targets.list_targets()[0].administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert json.loads(onboarding.read_text())["code"] == code
    result = _onboard(service, code, _port(bridges), name="Native Kodi retry")
    assert result.paired


def test_lost_bootstrap_response_recovers_without_second_otp_submission(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _targets, bridges, bridge, _onboarding = live_application
    client = ExternalBridgeClient()
    calls = 0
    original = client.bootstrap_otp

    def lost(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(client, "bootstrap_otp", lost)
    bridges._client = client
    result = _onboard(service, _code(bridge), _port(bridges))
    assert result.paired and calls == 1


@pytest.mark.parametrize("after_bridge_commit", [False, True])
def test_commit_failure_resumes_without_resubmitting_otp(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
    after_bridge_commit: bool,
) -> None:
    service, targets, bridges, bridge, _onboarding = live_application
    client = ExternalBridgeClient()
    calls = 0
    original = client.commit_pairing

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            if after_bridge_commit:
                original(*args, **kwargs)
            raise ExternalBridgeError("bridge_unreachable")
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "commit_pairing", interrupted)
    bridges._client = client
    with pytest.raises(ExternalKodiOnboardingError) as raised:
        _onboard(service, _code(bridge), _port(bridges))
    target_id = raised.value.target_id
    assert target_id is not None
    assert (
        targets.get(target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )
    assert (
        bridges.status(target_id).pairing_state
        is ExternalBridgePairingState.PAIRING_PENDING
    )
    resumed = service.resume(target_id)
    assert resumed.paired and resumed.enabled
    assert calls == 2
    assert service.resume(target_id) == resumed


def test_enable_failure_resumes_committed_pairing(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, targets, bridges, bridge, _onboarding = live_application
    original = targets.set_enabled
    calls = 0

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ExternalTargetError("verification_required")
        return original(*args, **kwargs)

    monkeypatch.setattr(targets, "set_enabled", interrupted)
    with pytest.raises(ExternalKodiOnboardingError) as raised:
        _onboard(service, _code(bridge), _port(bridges))
    target_id = raised.value.target_id
    assert target_id is not None
    assert (
        targets.get(target_id).administrative_state
        is ExternalAdministrativeState.DISABLED
    )
    assert bridges.status(target_id).pairing_state is ExternalBridgePairingState.PAIRED
    assert service.resume(target_id).enabled


def test_legacy_bridge_is_explicitly_rejected_without_long_token_fallback(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, targets, bridges, bridge, onboarding = live_application
    client = ExternalBridgeClient()

    def unsupported(*_args: Any, **_kwargs: Any) -> Any:
        raise ExternalBridgeError("bridge_protocol_unsupported")

    monkeypatch.setattr(client, "bootstrap_info", unsupported)
    bridges._client = client
    code = _code(bridge)
    with pytest.raises(
        ExternalKodiOnboardingError, match="bootstrap_version_unsupported"
    ):
        _onboard(service, code, _port(bridges))
    assert (
        targets.list_targets()[0].administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert json.loads(onboarding.read_text())["code"] == code


def test_bad_kodi_auth_retires_target_without_contacting_bridge(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
) -> None:
    service, targets, bridges, bridge, onboarding = live_application

    class RejectingVerifier:
        def probe(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ExternalProbeError("authentication_failed")

    targets._verifier = RejectingVerifier()  # type: ignore[assignment]
    code = _code(bridge)
    with pytest.raises(ExternalKodiOnboardingError, match="invalid_kodi_credentials"):
        _onboard(service, code, _port(bridges), username="kodi", password="bad")
    assert (
        targets.list_targets()[0].administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert json.loads(onboarding.read_text())["code"] == code


def test_existing_production_endpoint_is_never_repurposed(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
) -> None:
    service, targets, bridges, bridge, _onboarding = live_application
    production = ExternalTargetAdministrationService(
        targets._session_factory,
        endpoint_policy=targets._endpoint_policy,
        credential_store=targets._credential_store,
        verifier=FakeVerifier(),  # type: ignore[arg-type]
        target_type=TargetType.EXTERNAL_PRODUCTION,
    )
    existing = production.register(
        logical_name="Existing production",
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        host="127.0.0.1",
        port=8080,
        authentication_mode=ExternalAuthenticationMode.NONE,
        configured_capabilities={"target_inspect"},
    )
    result = _onboard(service, _code(bridge), _port(bridges))
    assert production.get(existing.target_id) == existing
    assert result.target_id != existing.target_id
    assert targets.get(result.target_id).target_class == "external_development"


@pytest.mark.parametrize(
    ("bridge_error", "expected"),
    [
        ("bridge_unreachable", "bridge_unreachable"),
        ("pairing_code_expired", "otp_expired"),
        ("pairing_temporarily_locked", "otp_locked"),
        ("already_paired", "bridge_already_paired"),
    ],
)
def test_bootstrap_info_failure_is_bounded_and_does_not_consume_code(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
    bridge_error: str,
    expected: str,
) -> None:
    service, targets, bridges, bridge, onboarding = live_application
    client = ExternalBridgeClient()

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise ExternalBridgeError(bridge_error)

    monkeypatch.setattr(client, "bootstrap_info", fail)
    bridges._client = client
    code = _code(bridge)
    with pytest.raises(ExternalKodiOnboardingError, match=expected):
        _onboard(service, code, _port(bridges))
    assert (
        targets.list_targets()[0].administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert json.loads(onboarding.read_text())["code"] == code


def test_registration_persistence_failure_is_bounded(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, targets, bridges, bridge, _onboarding = live_application

    def fail(**_kwargs: Any) -> Any:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(targets, "register", fail)
    with pytest.raises(ExternalKodiOnboardingError, match="target_persistence_failed"):
        _onboard(service, _code(bridge), _port(bridges))
    assert targets.list_targets() == []


def test_secret_store_failure_after_bootstrap_is_uncertain_and_redacted(
    live_application: tuple[
        ExternalKodiOnboardingService,
        ExternalTargetAdministrationService,
        ExternalBridgeService,
        dict[str, Any],
        Path,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, targets, bridges, bridge, onboarding = live_application
    code = _code(bridge)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise SecretStoreError("secret store unavailable")

    monkeypatch.setattr(bridges._secrets, "write", fail)
    with pytest.raises(ExternalKodiOnboardingError) as raised:
        _onboard(service, code, _port(bridges))
    assert raised.value.code == "pairing_uncertain"
    assert raised.value.target_id is None
    assert code not in repr(raised.value)
    assert (
        targets.list_targets()[0].administrative_state
        is ExternalAdministrativeState.DEACTIVATED
    )
    assert json.loads(onboarding.read_text())["code"] is None

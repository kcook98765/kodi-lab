from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import (
    BOOTSTRAP_PROTOCOL_VERSION,
    BridgeOtpBootstrapResult,
    ExternalBridgeClient,
    ExternalBridgeError,
    b64url,
    otp_bootstrap_request_id,
    request_signature,
    response_signature,
    timestamp_text,
)
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from tests.kodi.test_narrow_bridge_addon import _load


@contextmanager
def _server(bridge: dict[str, Any]) -> Iterator[int]:
    server = bridge["Server"](("127.0.0.1", 0), bridge["Handler"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Path, Path, list[str]]:
    state = tmp_path / "state.json"
    onboarding = tmp_path / "onboarding.json"
    commands: list[str] = []
    bridge = _load(
        monkeypatch,
        state_path=state,
        onboarding_path=onboarding,
        screenshot_commands=commands,
    )
    addon = SimpleNamespace(getSettingBool=lambda name: name == "enabled")
    monkeypatch.setattr(sys.modules["xbmcaddon"], "Addon", lambda _id: addon)
    bridge["_load_state"]()
    bridge["_onboarding_store"]().initialize(paired=False)
    return bridge, state, onboarding, commands


def _endpoint() -> ValidatedExternalEndpoint:
    return ValidatedExternalEndpoint(
        ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080
    )


def _bootstrap(
    client: ExternalBridgeClient,
    bridge: dict[str, Any],
    port: int,
    *,
    code: str | None = None,
    target_id: Any = None,
) -> tuple[Any, Any, bytes]:
    info = client.bootstrap_info(_endpoint(), bridge_port=port)
    target_id = target_id or uuid4()
    secret = os.urandom(32)
    result = client.bootstrap_otp(
        _endpoint(),
        bridge_port=port,
        target_id=target_id,
        info=info,
        code=code or bridge["_onboarding_store"]().status(paired=False)["code"],
        secret=secret,
        request_id=otp_bootstrap_request_id(secret, target_id),
    )
    return info, result, secret


def test_local_pairing_reset_invalidates_old_credential_and_preserves_identity(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, state_path, onboarding_path, _commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        _info, result, secret = _bootstrap(client, bridge, port)
        client.commit_pairing(_endpoint(), bridge_port=port, transaction=result)
        assert bridge["_local_pairing_status"]() is True
        state = json.loads(state_path.read_text())
        state["deployments"] = {str(uuid4()): {"state": "succeeded", "created": 1}}
        state["rollback"] = {"retained": True}
        bridge["_save_state"](state)
        old_bridge_id = state["bridge_id"]
        envelope: dict[str, Any] = {
            "protocol_version": "1",
            "operation": "bridge_identity",
            "target_id": str(result.target_id),
            "bridge_id": old_bridge_id,
            "request_id": str(uuid4()),
            "timestamp": timestamp_text(client._clock()),
            "nonce": b64url(os.urandom(32)),
            "payload": {},
        }
        envelope["signature"] = request_signature(secret, envelope)
        with httpx.Client(trust_env=False) as http:
            url = f"http://127.0.0.1:{port}/kodi-lab/bridge/v1"
            first = http.post(url, json=envelope)
            assert first.status_code == 200
            assert bridge["_reset_pairing"]() is True
            assert bridge["_reset_pairing"]() is False
            # Even an identical cached request must not recover an old receipt.
            assert http.post(url, json=envelope).status_code == 401
            envelope["request_id"] = str(uuid4())
            envelope["nonce"] = b64url(os.urandom(32))
            envelope["signature"] = request_signature(secret, envelope)
            assert http.post(url, json=envelope).status_code == 410
            assert http.get(f"http://127.0.0.1:{port}/reset").status_code == 404
            assert (
                http.post(f"http://127.0.0.1:{port}/reset", json={}).status_code == 404
            )
        reset = json.loads(state_path.read_text())
        assert reset["bridge_id"] == old_bridge_id
        assert reset["target_id"] is None
        assert reset["secret"] is None
        assert reset["replay"] == []
        assert reset["deployments"] == state["deployments"]
        assert reset["rollback"] == state["rollback"]
        assert json.loads(onboarding_path.read_text())["committed_request_id"] is None
        assert json.loads(onboarding_path.read_text())["transaction"] is None
        assert bridge["_local_pairing_status"]() is False
        with pytest.raises(ExternalBridgeError, match="pairing_code_unavailable"):
            client.bootstrap_info(_endpoint(), bridge_port=port)
        store = bridge["_onboarding_store"]()
        fresh_code = store.generate_new_code(paired=False)
        assert len(fresh_code) == 4
        assert store.status(paired=False)["code"] == fresh_code
        info = client.bootstrap_info(_endpoint(), bridge_port=port)
        assert str(info.bridge_id) == old_bridge_id
        assert info.addon_version == "1.1.29"
    restarted = _load(
        monkeypatch, state_path=state_path, onboarding_path=onboarding_path
    )
    monkeypatch.setattr(
        sys.modules["xbmcaddon"],
        "Addon",
        lambda _id: SimpleNamespace(getSettingBool=lambda name: name == "enabled"),
    )
    assert restarted["_local_pairing_status"]() is False
    assert restarted["_load_state"]()["bridge_id"] == old_bridge_id
    assert restarted["_onboarding_store"]().status(paired=False)["code"] == fresh_code
    with _server(restarted) as restarted_port:
        with pytest.raises(ExternalBridgeError):
            client.identity(
                _endpoint(),
                bridge_port=restarted_port,
                target_id=result.target_id,
                bridge_id=result.bridge_id,
                secret=secret,
            )
        assert (
            str(
                client.bootstrap_info(_endpoint(), bridge_port=restarted_port).bridge_id
            )
            == old_bridge_id
        )


def test_local_reset_needs_no_old_server_and_failed_state_write_stays_paired(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, state_path, onboarding_path, _commands = runtime
    state = bridge["_load_state"]()
    old_bridge_id = state["bridge_id"]
    state["target_id"] = str(uuid4())
    state["secret"] = b64url(os.urandom(32))
    bridge["_save_state"](state)
    globals_ = bridge["_reset_pairing"].__globals__
    save = globals_["_save_state"]

    def fail_write(_state: object) -> None:
        raise RuntimeError("simulated write failure")

    monkeypatch.setitem(globals_, "_save_state", fail_write)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        bridge["_reset_pairing"]()
    assert json.loads(state_path.read_text())["target_id"] == state["target_id"]
    assert json.loads(state_path.read_text())["secret"] == state["secret"]
    assert json.loads(onboarding_path.read_text())["transaction"] is None
    monkeypatch.setitem(globals_, "_save_state", save)
    assert bridge["_reset_pairing"]() is True
    assert bridge["_load_state"]()["bridge_id"] == old_bridge_id
    assert bridge["_local_pairing_status"]() is False


def test_local_reset_keeps_standard_otp_lifetime_and_attempt_limit(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, _state_path, onboarding_path, _commands = runtime
    state = bridge["_load_state"]()
    state["target_id"] = str(uuid4())
    state["secret"] = b64url(os.urandom(32))
    bridge["_save_state"](state)
    assert bridge["_reset_pairing"]() is True
    now = [1000.0]
    store = bridge["OnboardingStore"](
        onboarding_path,
        clock=lambda: now[0],
        choose=lambda _alphabet: "7" if now[0] < 1300 else "8",
    )
    code = store.generate_new_code(paired=False)
    initial = json.loads(onboarding_path.read_text())
    assert initial["expires_at"] - initial["created_at"] == 300
    assert initial["failed_attempts"] == 0
    now[0] = 1301.0
    assert store.status(paired=False)["status"] == "Pairing code expired"
    code = store.generate_new_code(paired=False)
    wrong = "2222" if code != "2222" else "3333"
    for attempt in range(5):
        assert not store.verify_and_reserve(
            wrong, "192.168.2.10", uuid4(), uuid4(), paired=False
        )
        assert json.loads(onboarding_path.read_text())["failed_attempts"] == attempt + 1
    assert store.status(paired=False)["status"] == "Pairing temporarily locked"


def test_bootstrap_provisional_commit_and_idempotent_recovery(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, state_path, onboarding_path, commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        info, result, secret = _bootstrap(client, bridge, port)
        assert info.bootstrap_version == BOOTSTRAP_PROTOCOL_VERSION
        assert info.bridge_id == result.bridge_id
        assert result.secret == secret and len(secret) == 32
        onboarding = json.loads(onboarding_path.read_text())
        assert onboarding["code"] is None
        assert onboarding["transaction"]["target_id"] == str(result.target_id)
        assert onboarding["transaction"]["request_id"] == str(result.request_id)
        assert (
            onboarding["transaction"]["credential_digest"]
            == hashlib.sha256(secret).hexdigest()
        )
        assert not json.loads(state_path.read_text())["target_id"]
        identity = client.identity(
            _endpoint(),
            bridge_port=port,
            target_id=result.target_id,
            bridge_id=result.bridge_id,
            secret=secret,
        )
        assert identity.addon_version == "1.1.29"
        assert identity.build_fingerprint == info.build_fingerprint
        with pytest.raises(ExternalBridgeError, match="pairing_code_unavailable"):
            client.bootstrap_info(_endpoint(), bridge_port=port)
        # A signed screenshot operation is rejected before _capture_png can run.
        envelope: dict[str, object] = {
            "protocol_version": "1",
            "operation": "capture_screenshot",
            "target_id": str(result.target_id),
            "bridge_id": str(result.bridge_id),
            "request_id": str(uuid4()),
            "timestamp": timestamp_text(client._clock()),
            "nonce": b64url(os.urandom(32)),
            "payload": {},
        }
        envelope["signature"] = request_signature(secret, envelope)
        with httpx.Client(trust_env=False) as http:
            denied = http.post(
                f"http://127.0.0.1:{port}/kodi-lab/bridge/v1", json=envelope
            )
        assert denied.status_code == 422
        denial = denied.json()
        assert denial["status"] == "provisional_operation_not_allowed"
        assert denial["signature"] == response_signature(
            secret,
            target_id=str(result.target_id),
            bridge_id=str(result.bridge_id),
            operation="capture_screenshot",
            request_id=str(envelope["request_id"]),
            timestamp=denial["timestamp"],
            nonce=denial["nonce"],
            status="provisional_operation_not_allowed",
            payload_digest=hashlib.sha256(b"{}").hexdigest(),
        )
        assert commands == []
        with pytest.raises(
            ExternalBridgeError, match="provisional_operation_not_allowed"
        ):
            client.capabilities(
                _endpoint(),
                bridge_port=port,
                target_id=result.target_id,
                bridge_id=result.bridge_id,
                secret=secret,
            )
        wrong_session = BridgeOtpBootstrapResult(
            result.target_id,
            result.bridge_id,
            uuid4(),
            secret,
            result.provisional_expires_at,
        )
        with pytest.raises(ExternalBridgeError, match="conflicting_commit"):
            client.commit_pairing(
                _endpoint(), bridge_port=port, transaction=wrong_session
            )
        client.commit_pairing(_endpoint(), bridge_port=port, transaction=result)
        client.commit_pairing(_endpoint(), bridge_port=port, transaction=result)
        with pytest.raises(ExternalBridgeError, match="conflicting_commit"):
            client.commit_pairing(
                _endpoint(), bridge_port=port, transaction=wrong_session
            )
        paired = json.loads(state_path.read_text())
        assert paired["target_id"] == str(result.target_id)
        assert paired["secret"] == b64url(secret)
        assert json.loads(onboarding_path.read_text())["transaction"] is None
        assert (
            client.identity(
                _endpoint(),
                bridge_port=port,
                target_id=result.target_id,
                bridge_id=result.bridge_id,
                secret=secret,
            )
            == identity
        )
        with pytest.raises(ExternalBridgeError, match="already_paired"):
            client.bootstrap_info(_endpoint(), bridge_port=port)


def test_wrong_code_lockout_and_replay(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, state_path, onboarding_path, _commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        info = client.bootstrap_info(_endpoint(), bridge_port=port)
        target_id = uuid4()
        code = bridge["_onboarding_store"]().status(paired=False)["code"]
        wrong = "2222" if code != "2222" else "3333"
        for attempt in range(5):
            secret = os.urandom(32)
            with pytest.raises(ExternalBridgeError, match="wrong_code"):
                client.bootstrap_otp(
                    _endpoint(),
                    bridge_port=port,
                    target_id=target_id,
                    info=info,
                    code=wrong,
                    secret=secret,
                    request_id=otp_bootstrap_request_id(secret, target_id),
                )
            state = json.loads(onboarding_path.read_text())
            assert state["failed_attempts"] == attempt + 1
            assert len(state["source_failures"]["127.0.0.1"]) == attempt + 1
            assert len(state["bridge_failures"]) == attempt + 1
            assert state["transaction"] is None
        with pytest.raises(ExternalBridgeError, match="pairing_temporarily_locked"):
            client.bootstrap_info(_endpoint(), bridge_port=port)
        secret = os.urandom(32)
        with pytest.raises(ExternalBridgeError, match="pairing_temporarily_locked"):
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=code,
                secret=secret,
                request_id=otp_bootstrap_request_id(secret, target_id),
            )
        assert json.loads(state_path.read_text())["target_id"] is None


def test_provisional_survives_server_restart_then_expires(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, _state_path, onboarding_path, _commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        _info, result, secret = _bootstrap(client, bridge, port)
    with _server(bridge) as port:
        assert (
            client.identity(
                _endpoint(),
                bridge_port=port,
                target_id=result.target_id,
                bridge_id=result.bridge_id,
                secret=secret,
            ).bridge_id
            == result.bridge_id
        )
    state = json.loads(onboarding_path.read_text())
    state["transaction"]["expires_at"] = 1.0
    onboarding_path.write_text(json.dumps(state))
    with _server(bridge) as port:
        with pytest.raises(ExternalBridgeError, match="provisional_expired"):
            client.identity(
                _endpoint(),
                bridge_port=port,
                target_id=result.target_id,
                bridge_id=result.bridge_id,
                secret=secret,
            )
        assert json.loads(onboarding_path.read_text())["transaction"] is None


def test_lab_stages_existing_target_then_commits_without_schema_change(
    runtime: tuple[dict[str, Any], Path, Path, list[str]], tmp_path: Path
) -> None:
    from sqlalchemy import create_engine

    from kodi_lab.application.external_bridges import ExternalBridgeService
    from kodi_lab.domain import ExternalBridgePairingState
    from kodi_lab.kodi.external import ExternalEndpointPolicy
    from kodi_lab.storage.engine import create_session_factory
    from kodi_lab.storage.metadata import Base
    from kodi_lab.storage.models import ExternalBridgePairingRow
    from kodi_lab.storage.secrets import ExternalBridgeSecretStore
    from tests.application.test_external_bridges import _seed

    bridge, _state_path, onboarding_path, _commands = runtime
    engine = create_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    with _server(bridge) as port:
        service = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "secrets", master_key=b"s" * 32, key_version=1
            ),
        )
        code = bridge["_onboarding_store"]().status(paired=False)["code"]
        pending = service.begin_otp_pairing(target_id, bridge_port=port, code=code)
        assert pending.pairing_state is ExternalBridgePairingState.PAIRING_PENDING
        assert pending.verified_capabilities == ()
        assert pending.observed_bridge_id is not None
        with factory() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            assert row is not None and row.bridge_secret_reference is not None
            assert row.pairing_token_reference is None
        assert json.loads(onboarding_path.read_text())["code"] is None
        paired = service.commit_otp_pairing(target_id)
        assert paired.pairing_state is ExternalBridgePairingState.PAIRED
        assert paired.pinned_bridge_id == pending.observed_bridge_id
        assert paired.verified_capabilities == (
            "log_read",
            "screenshot",
            "ui_observation",
            "workspace_deployment",
        )
    engine.dispose()


def test_wrong_then_correct_code_consumption_and_bootstrap_replay(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, _state_path, onboarding_path, _commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        info = client.bootstrap_info(_endpoint(), bridge_port=port)
        target_id = uuid4()
        code = bridge["_onboarding_store"]().status(paired=False)["code"]
        wrong = "2222" if code != "2222" else "3333"
        first_secret = os.urandom(32)
        first_id = otp_bootstrap_request_id(first_secret, target_id)
        with pytest.raises(ExternalBridgeError, match="wrong_code"):
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=wrong,
                secret=first_secret,
                request_id=first_id,
            )
        with pytest.raises(ExternalBridgeError, match="bootstrap_replay_rejected"):
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=wrong,
                secret=first_secret,
                request_id=first_id,
            )
        assert json.loads(onboarding_path.read_text())["failed_attempts"] == 1
        second_secret = os.urandom(32)
        second_id = otp_bootstrap_request_id(second_secret, target_id)
        result = client.bootstrap_otp(
            _endpoint(),
            bridge_port=port,
            target_id=target_id,
            info=info,
            code=code,
            secret=second_secret,
            request_id=second_id,
        )
        assert result.request_id == second_id
        assert json.loads(onboarding_path.read_text())["code"] is None
        with pytest.raises(ExternalBridgeError, match="bootstrap_replay_rejected"):
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=code,
                secret=second_secret,
                request_id=second_id,
            )
        with pytest.raises(ExternalBridgeError, match="pairing_in_progress"):
            third_secret = os.urandom(32)
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=code,
                secret=third_secret,
                request_id=otp_bootstrap_request_id(third_secret, target_id),
            )


def test_expired_code_and_version_mismatch_do_not_create_provisional_state(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, _state_path, onboarding_path, _commands = runtime
    client = ExternalBridgeClient()
    with _server(bridge) as port:
        info = client.bootstrap_info(_endpoint(), bridge_port=port)
        code = bridge["_onboarding_store"]().status(paired=False)["code"]
        target_id = uuid4()
        secret = os.urandom(32)
        request_id = otp_bootstrap_request_id(secret, target_id)
        request = {
            "bootstrap_version": "wrong-version",
            "operation": "otp_bootstrap",
            "target_id": str(target_id),
            "bridge_id": str(info.bridge_id),
            "request_id": str(request_id),
            "timestamp": timestamp_text(client._clock()),
            "nonce": b64url(os.urandom(32)),
            "credential": b64url(secret),
            "otp": code,
        }
        with httpx.Client(trust_env=False) as http:
            invalid = http.post(
                f"http://127.0.0.1:{port}/kodi-lab/bridge/bootstrap/v1",
                json=request,
            )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "bootstrap_version_mismatch"
        state = json.loads(onboarding_path.read_text())
        assert state["failed_attempts"] == 0
        now = time.time()
        state["created_at"] = now - 301
        state["expires_at"] = now - 1
        state["last_generated_at"] = now - 301
        onboarding_path.write_text(json.dumps(state))
        with pytest.raises(ExternalBridgeError, match="pairing_code_expired"):
            client.bootstrap_info(_endpoint(), bridge_port=port)
        with pytest.raises(ExternalBridgeError, match="pairing_code_expired"):
            client.bootstrap_otp(
                _endpoint(),
                bridge_port=port,
                target_id=target_id,
                info=info,
                code=code,
                secret=secret,
                request_id=request_id,
            )
        assert json.loads(onboarding_path.read_text())["transaction"] is None


def test_new_bridge_refuses_legacy_pairing_operation(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, state_path, onboarding_path, _commands = runtime
    before = onboarding_path.read_bytes()
    with _server(bridge) as port:
        with httpx.Client(trust_env=False) as http:
            response = http.post(
                f"http://127.0.0.1:{port}/kodi-lab/bridge/v1",
                json={"operation": "pair_bridge", "request_id": str(uuid4())},
            )
        assert response.status_code == 404
    assert onboarding_path.read_bytes() == before
    assert json.loads(state_path.read_text())["target_id"] is None


def test_existing_paired_state_remains_authoritative_without_otp(
    runtime: tuple[dict[str, Any], Path, Path, list[str]],
) -> None:
    bridge, state_path, onboarding_path, _commands = runtime
    target_id = uuid4()
    secret = os.urandom(32)
    state = json.loads(state_path.read_text())
    bridge_id = state["bridge_id"]
    state["target_id"] = str(target_id)
    state["secret"] = b64url(secret)
    state_path.write_text(json.dumps(state))
    onboarding_path.write_text("{truncated")
    with _server(bridge) as port:
        client = ExternalBridgeClient()
        assert client.identity(
            _endpoint(),
            bridge_port=port,
            target_id=target_id,
            bridge_id=UUID(bridge_id),
            secret=secret,
        ).bridge_id == UUID(bridge_id)
        with pytest.raises(ExternalBridgeError, match="already_paired"):
            client.bootstrap_info(_endpoint(), bridge_port=port)
    assert onboarding_path.read_text() == "{truncated"


def test_lost_bootstrap_and_commit_responses_recover_without_second_otp(
    runtime: tuple[dict[str, Any], Path, Path, list[str]], tmp_path: Path
) -> None:
    from sqlalchemy import create_engine

    from kodi_lab.application.external_bridges import (
        ExternalBridgeService,
        ExternalBridgeServiceError,
    )
    from kodi_lab.domain import ExternalBridgePairingState
    from kodi_lab.kodi.external import ExternalEndpointPolicy
    from kodi_lab.storage.engine import create_session_factory
    from kodi_lab.storage.metadata import Base
    from kodi_lab.storage.secrets import ExternalBridgeSecretStore
    from tests.application.test_external_bridges import _seed

    class LostResponses(ExternalBridgeClient):
        bootstrap_dispatches = 0
        commit_dispatches = 0

        def bootstrap_otp(self, *args: Any, **kwargs: Any) -> Any:
            self.bootstrap_dispatches += 1
            super().bootstrap_otp(*args, **kwargs)
            raise ExternalBridgeError("bridge_unreachable") from None

        def commit_pairing(self, *args: Any, **kwargs: Any) -> None:
            self.commit_dispatches += 1
            super().commit_pairing(*args, **kwargs)
            if self.commit_dispatches == 1:
                raise ExternalBridgeError("bridge_unreachable")

    bridge, _state_path, _onboarding_path, _commands = runtime
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    target_id = uuid4()
    _seed(factory, target_id)
    client = LostResponses()
    with _server(bridge) as port:
        service = ExternalBridgeService(
            factory,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(port,), test_loopback_enabled=True
            ),
            secret_store=ExternalBridgeSecretStore(
                tmp_path / "secrets", master_key=b"s" * 32, key_version=1
            ),
            client=client,
        )
        code = bridge["_onboarding_store"]().status(paired=False)["code"]
        assert (
            service.begin_otp_pairing(
                target_id, bridge_port=port, code=code
            ).pairing_state
            is ExternalBridgePairingState.PAIRING_PENDING
        )
        assert client.bootstrap_dispatches == 1
        with pytest.raises(ExternalBridgeServiceError, match="bridge_unreachable"):
            service.commit_otp_pairing(target_id)
        assert (
            service.status(target_id).pairing_state
            is ExternalBridgePairingState.PAIRING_PENDING
        )
        assert (
            service.commit_otp_pairing(target_id).pairing_state
            is ExternalBridgePairingState.PAIRED
        )
        assert client.commit_dispatches == 2
        assert client.bootstrap_dispatches == 1
    engine.dispose()

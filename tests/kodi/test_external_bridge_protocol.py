from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import (
    BRIDGE_ENDPOINT_PATH,
    BRIDGE_PROTOCOL_VERSION,
    ExternalBridgeClient,
    ExternalBridgeError,
    b64url,
    request_signature,
    timestamp_text,
)
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.screenshots import EXPECTED_HEIGHT, EXPECTED_WIDTH
from tests.kodi.fake_external_bridge import FakeExternalBridge


def _endpoint(port: int) -> ValidatedExternalEndpoint:
    return ValidatedExternalEndpoint(
        protocol_kind=ExternalProtocolKind.JSONRPC_HTTP,
        address="127.0.0.1",
        port=port,
    )


def test_fake_bridge_pair_identity_capability_screenshot_and_replay() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        assert pairing.identity.bridge_id == fake.bridge_id
        assert (
            client.identity(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
            )
            == pairing.identity
        )
        assert client.capabilities(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
        ) == ("screenshot", "workspace_deployment")
        request_id = uuid4()
        nonce = b64url(os.urandom(32))
        requested_at = datetime.now(UTC)
        first = client.capture_screenshot(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            request_id=request_id,
            nonce=nonce,
            requested_at=requested_at,
        )
        replay = client.capture_screenshot(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            request_id=request_id,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert (first.width, first.height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT)
        assert replay.png == first.png
        assert fake.capture_count == 1


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("image", "bridge_authentication_failed"),
        ("altered_status", "bridge_identity_mismatch"),
        ("wrong_request_id", "bridge_identity_mismatch"),
        ("wrong_bridge_id", "bridge_identity_mismatch"),
        ("bad_signature", "bridge_authentication_failed"),
        ("media_type", "screenshot_invalid"),
        ("invalid_png", "screenshot_invalid"),
        ("wrong_dimensions", "screenshot_invalid"),
        ("oversize", "screenshot_too_large"),
    ],
)
def test_response_authentication_rejects_tampering(tamper: str, code: str) -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        fake.tamper = tamper
        with pytest.raises(ExternalBridgeError) as raised:
            client.capture_screenshot(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
                request_id=uuid4(),
                nonce=b64url(os.urandom(32)),
                requested_at=datetime.now(UTC),
            )
        assert raised.value.code == code


def test_wrong_secret_target_protocol_and_stale_clock_fail_closed() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        for target, bridge, secret, when in (
            (target_id, fake.bridge_id, os.urandom(32), datetime.now(UTC)),
            (uuid4(), fake.bridge_id, pairing.secret, datetime.now(UTC)),
            (target_id, uuid4(), pairing.secret, datetime.now(UTC)),
            (
                target_id,
                fake.bridge_id,
                pairing.secret,
                datetime.now(UTC) - timedelta(minutes=2),
            ),
        ):
            with pytest.raises(ExternalBridgeError) as raised:
                client.capture_screenshot(
                    _endpoint(fake.port),
                    bridge_port=fake.port,
                    target_id=target,
                    bridge_id=bridge,
                    secret=secret,
                    request_id=uuid4(),
                    nonce=b64url(os.urandom(32)),
                    requested_at=when,
                )
            assert raised.value.code in {
                "bridge_authentication_failed",
                "bridge_unreachable",
            }


def test_nonce_and_request_uuid_replay_cannot_reexecute_capture() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        request_id = uuid4()
        nonce = b64url(os.urandom(32))
        now = datetime.now(UTC)
        client.capture_screenshot(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            request_id=request_id,
            nonce=nonce,
            requested_at=now,
        )
        with pytest.raises(ExternalBridgeError, match="bridge_replay_rejected"):
            client.capture_screenshot(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
                request_id=request_id,
                nonce=b64url(os.urandom(32)),
                requested_at=now,
            )
        with pytest.raises(ExternalBridgeError, match="bridge_authentication_failed"):
            client.capture_screenshot(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
                request_id=uuid4(),
                nonce=nonce,
                requested_at=now,
            )
        assert fake.capture_count == 1


def test_operation_payload_and_identity_are_covered_by_request_hmac() -> None:
    secret = os.urandom(32)
    envelope: dict[str, object] = {
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "operation": "capture_screenshot",
        "target_id": str(uuid4()),
        "bridge_id": str(uuid4()),
        "request_id": str(uuid4()),
        "timestamp": timestamp_text(datetime.now(UTC)),
        "nonce": b64url(os.urandom(32)),
        "payload": {},
    }
    signature = request_signature(secret, envelope)
    for key, value in (
        ("operation", "bridge_identity"),
        ("target_id", str(uuid4())),
        ("bridge_id", str(uuid4())),
        ("payload", {"path": "/forbidden"}),
    ):
        tampered = dict(envelope)
        tampered[key] = value
        assert request_signature(secret, tampered) != signature


def test_content_addressed_deployment_receipt_prevents_duplicate_replacement() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    deployment_id = uuid4()
    semantic_request_id = uuid4()
    content = b"fixture"
    digest = hashlib.sha256(content).hexdigest()
    manifest_digest = "sha256:" + hashlib.sha256(b"manifest").hexdigest()
    entries: list[dict[str, object]] = [
        {
            "type": "file",
            "mode": 420,
            "path": "addon.py",
            "sha256": digest,
            "size": len(content),
        }
    ]
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        begun = client.deployment_begin(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            deployment_id=deployment_id,
            request_id=semantic_request_id,
            addon_id=fake.addon_id,
            addon_version="2.0.0",
            generation=3,
            manifest_digest=manifest_digest,
            entries=entries,
            file_count=1,
            total_bytes=len(content),
        )
        assert begun.state == "staging"
        client.deployment_chunk(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            deployment_id=deployment_id,
            path="addon.py",
            file_digest=digest,
            offset=0,
            content=content,
        )
        first = client.deployment_commit(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            deployment_id=deployment_id,
            manifest_digest=manifest_digest,
        )
        replay = client.deployment_commit(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            deployment_id=deployment_id,
            manifest_digest=manifest_digest,
        )
        recovered = client.deployment_receipt(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            bridge_id=fake.bridge_id,
            secret=pairing.secret,
            deployment_id=deployment_id,
        )
        assert first.state == replay.state == recovered.state == "succeeded"
        assert first.receipt_id == replay.receipt_id == recovered.receipt_id
        assert fake.replacement_count == 1


def test_unsupported_bridge_protocol_is_bounded() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        fake.protocol_version = "2"
        with pytest.raises(ExternalBridgeError, match="bridge_protocol_unsupported"):
            ExternalBridgeClient().pair(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                token=token,
            )


def test_bridge_transport_rejects_redirect_and_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = uuid4()
    token = os.urandom(32)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        assert pairing.identity.bridge_id == fake.bridge_id
        fake.tamper = "redirect"
        with pytest.raises(ExternalBridgeError, match="bridge_unreachable"):
            client.identity(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
            )


def test_remote_caller_without_pairing_token_cannot_enroll_bridge() -> None:
    target_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        request = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "operation": "pair_bridge",
            "target_id": str(target_id),
            "request_id": str(uuid4()),
            "timestamp": timestamp_text(datetime.now(UTC)),
            "server_nonce": b64url(os.urandom(32)),
            "pairing_proof": b64url(os.urandom(32)),
        }
        response = httpx.post(
            f"http://127.0.0.1:{fake.port}{BRIDGE_ENDPOINT_PATH}",
            json=request,
            follow_redirects=False,
            trust_env=False,
            timeout=2,
        )
        assert response.status_code == 401
        assert fake.secret is None

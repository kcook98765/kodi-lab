from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from kodi_lab.domain import ExternalProtocolKind
from kodi_lab.kodi.bridge import (
    BRIDGE_ENDPOINT_PATH,
    BRIDGE_PROTOCOL_VERSION,
    SCREENSHOT_ERROR_CODES,
    ExternalBridgeClient,
    ExternalBridgeError,
    b64url,
    request_signature,
    response_signature,
    timestamp_text,
)
from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.screenshots import EXPECTED_HEIGHT, EXPECTED_WIDTH
from kodi_lab.screenshot_diagnostics import screenshot_diagnostic_trace
from tests.kodi.fake_external_bridge import FakeExternalBridge, fake_png


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


def test_screenshot_diagnostic_trace_orders_safe_transport_and_image_stages() -> None:
    target_id = uuid4()
    client_id = uuid4()
    request_id = uuid4()
    token = os.urandom(32)
    with FakeExternalBridge(target_id=target_id, token=token) as fake:
        client = ExternalBridgeClient()
        pairing = client.pair(
            _endpoint(fake.port),
            bridge_port=fake.port,
            target_id=target_id,
            token=token,
        )
        with screenshot_diagnostic_trace(
            protocol_request={"type": "string", "byte_length": 36, "sha256": "a" * 64},
            client_id=client_id,
            target_id=target_id,
            request_id=request_id,
        ) as trace:
            assert trace is not None
            result = client.capture_screenshot(
                _endpoint(fake.port),
                bridge_port=fake.port,
                target_id=target_id,
                bridge_id=fake.bridge_id,
                secret=pairing.secret,
                request_id=request_id,
                nonce=b64url(os.urandom(32)),
                requested_at=datetime.now(UTC),
            )
        stages = [item["stage"] for item in trace.events]
        assert stages == [
            "bridge_request_prepared",
            "bridge_request_sent",
            "bridge_response_received",
            "response_schema",
            "bridge_operation_returned",
            "image_payload",
            "image_validation",
        ]
        assert [item["order"] for item in trace.events] == list(
            range(1, len(trace.events) + 1)
        )
        assert all(item["request_id"] == str(request_id) for item in trace.events)
        assert all(item["target_id"] == str(target_id) for item in trace.events)
        assert trace.events[-1]["image_sha256"] == result.digest
        assert trace.events[-1]["code"] == "png_validated_unchanged"
        assert "normalized_byte_length" not in trace.events[-1]
        assert "normalized_sha256" not in trace.events[-1]
        assert result.png == fake_png()
        assert "screenshot_normalization_failed" not in SCREENSHOT_ERROR_CODES
        serialized = json.dumps(trace.events, sort_keys=True)
        assert base64.b64encode(result.png).decode("ascii") not in serialized
        assert result.png.hex() not in serialized


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("image", "bridge_authentication_failed"),
        ("altered_status", "bridge_identity_mismatch"),
        ("wrong_request_id", "bridge_identity_mismatch"),
        ("wrong_bridge_id", "bridge_identity_mismatch"),
        ("bad_signature", "bridge_authentication_failed"),
        ("media_type", "screenshot_payload_type_invalid"),
        ("missing_payload", "screenshot_payload_missing"),
        ("missing_width_header", "screenshot_response_schema_invalid"),
        ("invalid_png", "screenshot_format_invalid"),
        ("wrong_dimensions", "screenshot_dimensions_invalid"),
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


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        ("capture_error", "screenshot_capture_failed"),
        ("capture_error_bad_signature", "bridge_authentication_failed"),
    ],
)
def test_authenticated_screenshot_error_is_distinct_from_authentication(
    tamper: str, code: str
) -> None:
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
        request_id = uuid4()
        nonce = b64url(os.urandom(32))
        requested_at = datetime.now(UTC)
        for _attempt in range(2):
            with pytest.raises(ExternalBridgeError) as raised:
                client.capture_screenshot(
                    _endpoint(fake.port),
                    bridge_port=fake.port,
                    target_id=target_id,
                    bridge_id=fake.bridge_id,
                    secret=pairing.secret,
                    request_id=request_id,
                    nonce=nonce,
                    requested_at=requested_at,
                )
            assert raised.value.code == code
        assert fake.capture_count == 1


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
    ],
)
def test_screenshot_transport_failures_remain_unreachable(
    error_type: type[httpx.TransportError],
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("fixture transport failure", request=request)

    client = ExternalBridgeClient(transport=httpx.MockTransport(fail))
    with pytest.raises(ExternalBridgeError) as raised:
        client.capture_screenshot(
            _endpoint(8765),
            bridge_port=8765,
            target_id=uuid4(),
            bridge_id=uuid4(),
            secret=os.urandom(32),
            request_id=uuid4(),
            nonce=b64url(os.urandom(32)),
            requested_at=datetime.now(UTC),
        )
    assert raised.value.code == "bridge_unreachable"


def test_slow_screenshot_uses_longer_read_timeout_and_accepts_signed_png() -> None:
    target_id = uuid4()
    bridge_id = uuid4()
    request_id = uuid4()
    secret = os.urandom(32)
    png = fake_png()
    digest = hashlib.sha256(png).hexdigest()
    dispatches = 0
    simulated_processing_seconds = 9.845

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        timeout = request.extensions["timeout"]
        assert timeout == {"connect": 2.0, "read": 20.0, "write": 5.0, "pool": 5.0}
        assert simulated_processing_seconds > 5.0
        if simulated_processing_seconds >= timeout["read"]:
            raise httpx.ReadTimeout(
                "synthetic processing exceeded read timeout", request=request
            )
        envelope = json.loads(request.content)
        assert envelope["operation"] == "capture_screenshot"
        assert envelope["request_id"] == str(request_id)
        timestamp = timestamp_text(datetime.now(UTC))
        nonce = b64url(os.urandom(32))
        headers = {
            "Content-Type": "image/png",
            "X-Kodi-Lab-Protocol": BRIDGE_PROTOCOL_VERSION,
            "X-Kodi-Lab-Operation": "capture_screenshot",
            "X-Kodi-Lab-Target-Id": str(target_id),
            "X-Kodi-Lab-Bridge-Id": str(bridge_id),
            "X-Kodi-Lab-Request-Id": str(request_id),
            "X-Kodi-Lab-Timestamp": timestamp,
            "X-Kodi-Lab-Nonce": nonce,
            "X-Kodi-Lab-Status": "ok",
            "X-Kodi-Lab-Content-SHA256": digest,
            "X-Kodi-Lab-Width": str(EXPECTED_WIDTH),
            "X-Kodi-Lab-Height": str(EXPECTED_HEIGHT),
            "X-Kodi-Lab-Signature": response_signature(
                secret,
                target_id=str(target_id),
                bridge_id=str(bridge_id),
                operation="capture_screenshot",
                request_id=str(request_id),
                timestamp=timestamp,
                nonce=nonce,
                status="ok",
                payload_digest=digest,
            ),
        }
        return httpx.Response(200, headers=headers, content=png)

    client = ExternalBridgeClient(transport=httpx.MockTransport(respond))
    result = client.capture_screenshot(
        _endpoint(8765),
        bridge_port=8765,
        target_id=target_id,
        bridge_id=bridge_id,
        secret=secret,
        request_id=request_id,
        nonce=b64url(os.urandom(32)),
        requested_at=datetime.now(UTC),
    )
    assert dispatches == 1
    assert result.png == png
    assert result.digest == digest
    assert (result.width, result.height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT)


def test_screenshot_exceeding_new_read_timeout_fails_without_retry() -> None:
    dispatches = 0
    simulated_processing_seconds = 25.0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        timeout = request.extensions["timeout"]
        assert timeout["connect"] == 2.0
        assert timeout["read"] == 20.0
        assert simulated_processing_seconds > timeout["read"]
        if simulated_processing_seconds >= timeout["read"]:
            raise httpx.ReadTimeout(
                "synthetic processing exceeded read timeout", request=request
            )
        raise AssertionError("synthetic processing unexpectedly fit the read timeout")

    client = ExternalBridgeClient(transport=httpx.MockTransport(respond))
    with pytest.raises(ExternalBridgeError) as raised:
        client.capture_screenshot(
            _endpoint(8765),
            bridge_port=8765,
            target_id=uuid4(),
            bridge_id=uuid4(),
            secret=os.urandom(32),
            request_id=uuid4(),
            nonce=b64url(os.urandom(32)),
            requested_at=datetime.now(UTC),
        )
    assert raised.value.code == "bridge_unreachable"
    assert dispatches == 1


def test_identity_keeps_short_read_timeout() -> None:
    dispatches = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal dispatches
        dispatches += 1
        assert request.extensions["timeout"] == {
            "connect": 2.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        raise httpx.ReadTimeout("synthetic identity timeout", request=request)

    client = ExternalBridgeClient(transport=httpx.MockTransport(respond))
    with pytest.raises(ExternalBridgeError) as raised:
        client.identity(
            _endpoint(8765),
            bridge_port=8765,
            target_id=uuid4(),
            bridge_id=uuid4(),
            secret=os.urandom(32),
        )
    assert raised.value.code == "bridge_unreachable"
    assert dispatches == 1


def test_authenticated_addon_inspection_error_is_not_replay_rejection() -> None:
    target_id = uuid4()
    bridge_id = uuid4()
    secret = os.urandom(32)

    def respond(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        payload: dict[str, object] = {}
        timestamp = timestamp_text(datetime.now(UTC))
        nonce = b64url(os.urandom(32))
        status = "addon_inspection_failed"
        response = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "operation": envelope["operation"],
            "target_id": envelope["target_id"],
            "bridge_id": envelope["bridge_id"],
            "request_id": envelope["request_id"],
            "timestamp": timestamp,
            "nonce": nonce,
            "status": status,
            "payload": payload,
        }
        response["signature"] = response_signature(
            secret,
            target_id=envelope["target_id"],
            bridge_id=envelope["bridge_id"],
            operation=envelope["operation"],
            request_id=envelope["request_id"],
            timestamp=timestamp,
            nonce=nonce,
            status=status,
            payload_digest=hashlib.sha256(b"{}").hexdigest(),
        )
        return httpx.Response(422, json=response)

    client = ExternalBridgeClient(transport=httpx.MockTransport(respond))
    with pytest.raises(ExternalBridgeError) as raised:
        client.inspect_addon(
            _endpoint(8765),
            bridge_port=8765,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            addon_id="plugin.video.fixture",
        )
    assert raised.value.code == "addon_inspection_failed"


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

"""Closed authenticated Phase 13 bridge protocol and direct client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx

from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.screenshots import (
    MAX_PNG_BYTES,
    PNG_MIME_TYPE,
    ScreenshotArtifactError,
    validate_png,
)

BRIDGE_PROTOCOL_VERSION = "1"
BRIDGE_ENDPOINT_PATH = "/kodi-lab/bridge/v1"
BRIDGE_ADDON_ID = "service.kodi_lab_bridge"
BRIDGE_CAPABILITIES = frozenset({"screenshot"})
BRIDGE_OPERATIONS = frozenset(
    {"bridge_identity", "bridge_capabilities", "capture_screenshot"}
)
PAIRING_OPERATION = "pair_bridge"
MAX_BRIDGE_JSON_BYTES = 65_536
MAX_CLOCK_SKEW_SECONDS = 60


class ExternalBridgeError(RuntimeError):
    """Bounded semantic bridge error; raw network detail never escapes."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BridgeIdentity:
    bridge_id: UUID
    protocol_version: str
    addon_id: str
    addon_version: str
    kodi_version: str
    capabilities: tuple[str, ...]
    build_fingerprint: str


@dataclass(frozen=True, slots=True)
class BridgePairingResult:
    identity: BridgeIdentity
    secret: bytes


@dataclass(frozen=True, slots=True)
class BridgeScreenshotResult:
    request_id: UUID
    bridge_id: UUID
    png: bytes
    width: int
    height: int
    digest: str
    captured_at: datetime


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_b64url(value: str, *, size: int) -> bytes:
    try:
        raw = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as error:
        raise ExternalBridgeError("bridge_authentication_failed") from error
    if len(raw) != size or b64url(raw) != value:
        raise ExternalBridgeError("bridge_authentication_failed")
    return raw


def timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def parse_fresh_timestamp(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or len(value) != 27:
        raise ExternalBridgeError("bridge_authentication_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExternalBridgeError("bridge_authentication_failed") from error
    if abs((now.astimezone(UTC) - parsed.astimezone(UTC)).total_seconds()) > (
        MAX_CLOCK_SKEW_SECONDS
    ):
        raise ExternalBridgeError("bridge_authentication_failed")
    return parsed


def derive_pairing_key(token: bytes) -> bytes:
    if len(token) != 32:
        raise ValueError("pairing token must be 256 bits")
    return hmac.new(token, b"kodi-lab-bridge-pairing-key-v1", hashlib.sha256).digest()


def derive_bridge_secret(
    token: bytes,
    *,
    target_id: UUID,
    bridge_id: UUID,
    server_nonce: str,
    bridge_nonce: str,
) -> bytes:
    key = derive_pairing_key(token)
    context = canonical_json(
        {
            "bridge_id": str(bridge_id),
            "bridge_nonce": bridge_nonce,
            "label": "kodi-lab-bridge-request-secret-v1",
            "server_nonce": server_nonce,
            "target_id": str(target_id),
        }
    )
    return hmac.new(key, context, hashlib.sha256).digest()


def request_key(secret: bytes) -> bytes:
    return hmac.new(
        secret, b"kodi-lab-bridge-request-signing-v1", hashlib.sha256
    ).digest()


def response_key(secret: bytes) -> bytes:
    return hmac.new(
        secret, b"kodi-lab-bridge-response-signing-v1", hashlib.sha256
    ).digest()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def request_signature(secret: bytes, envelope: dict[str, object]) -> str:
    fields = {
        "bridge_id": envelope["bridge_id"],
        "nonce": envelope["nonce"],
        "operation": envelope["operation"],
        "payload_digest": _digest(canonical_json(envelope["payload"])),
        "protocol_version": envelope["protocol_version"],
        "request_id": envelope["request_id"],
        "target_id": envelope["target_id"],
        "timestamp": envelope["timestamp"],
    }
    return b64url(
        hmac.new(request_key(secret), canonical_json(fields), hashlib.sha256).digest()
    )


def response_signature(
    secret: bytes,
    *,
    target_id: str,
    bridge_id: str,
    operation: str,
    request_id: str,
    timestamp: str,
    nonce: str,
    status: str,
    payload_digest: str,
) -> str:
    fields = {
        "bridge_id": bridge_id,
        "nonce": nonce,
        "operation": operation,
        "payload_digest": payload_digest,
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "request_id": request_id,
        "status": status,
        "target_id": target_id,
        "timestamp": timestamp,
    }
    return b64url(
        hmac.new(response_key(secret), canonical_json(fields), hashlib.sha256).digest()
    )


class ExternalBridgeClient:
    """Fixed-path, no-proxy, no-redirect bridge transport."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        connect_timeout: float = 2.0,
        response_timeout: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transport = transport
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._clock = clock

    def pair(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        token: bytes,
    ) -> BridgePairingResult:
        request_id = uuid4()
        server_nonce = b64url(os.urandom(32))
        request: dict[str, object] = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "operation": PAIRING_OPERATION,
            "target_id": str(target_id),
            "request_id": str(request_id),
            "timestamp": timestamp_text(self._clock()),
            "server_nonce": server_nonce,
        }
        request["pairing_proof"] = b64url(
            hmac.new(
                derive_pairing_key(token), canonical_json(request), hashlib.sha256
            ).digest()
        )
        document = self._post_json(endpoint, bridge_port, request)
        expected = {
            "protocol_version",
            "operation",
            "target_id",
            "request_id",
            "timestamp",
            "bridge_nonce",
            "identity",
            "pairing_proof",
        }
        if not isinstance(document, dict) or set(document) != expected:
            raise ExternalBridgeError("bridge_authentication_failed")
        parse_fresh_timestamp(document["timestamp"], self._clock())
        if document["protocol_version"] != BRIDGE_PROTOCOL_VERSION:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        if (
            document["operation"] != PAIRING_OPERATION
            or document["target_id"] != str(target_id)
            or document["request_id"] != str(request_id)
            or not isinstance(document["bridge_nonce"], str)
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        decode_b64url(document["bridge_nonce"], size=32)
        identity = self._identity(document["identity"])
        proof_fields = {key: document[key] for key in expected - {"pairing_proof"}}
        expected_proof = b64url(
            hmac.new(
                derive_pairing_key(token), canonical_json(proof_fields), hashlib.sha256
            ).digest()
        )
        if not isinstance(document["pairing_proof"], str) or not hmac.compare_digest(
            expected_proof, document["pairing_proof"]
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        secret = derive_bridge_secret(
            token,
            target_id=target_id,
            bridge_id=identity.bridge_id,
            server_nonce=server_nonce,
            bridge_nonce=document["bridge_nonce"],
        )
        return BridgePairingResult(identity=identity, secret=secret)

    def identity(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
    ) -> BridgeIdentity:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="bridge_identity",
        )
        return self._identity(payload)

    def capabilities(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
    ) -> tuple[str, ...]:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="bridge_capabilities",
        )
        if not isinstance(payload, dict) or set(payload) != {"capabilities"}:
            raise ExternalBridgeError("bridge_capability_unavailable")
        values = payload["capabilities"]
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) for item in values)
            or len(values) != len(set(values))
            or not frozenset(values) <= BRIDGE_CAPABILITIES
        ):
            raise ExternalBridgeError("bridge_capability_unavailable")
        return tuple(sorted(values))

    def capture_screenshot(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        request_id: UUID,
        nonce: str,
        requested_at: datetime,
    ) -> BridgeScreenshotResult:
        envelope = self._envelope(
            target_id=target_id,
            bridge_id=bridge_id,
            operation="capture_screenshot",
            request_id=request_id,
            nonce=nonce,
            requested_at=requested_at,
        )
        envelope["signature"] = request_signature(secret, envelope)
        try:
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    self._url(endpoint, bridge_port),
                    json=envelope,
                    timeout=self._timeout(),
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise ExternalBridgeError("bridge_authentication_failed")
                if 300 <= response.status_code < 400:
                    raise ExternalBridgeError("bridge_unreachable")
                if response.status_code == 409:
                    raise ExternalBridgeError("bridge_replay_rejected")
                if response.status_code != 200:
                    raise ExternalBridgeError("bridge_unreachable")
                if (
                    response.headers.get("content-type", "").split(";", 1)[0]
                    != PNG_MIME_TYPE
                ):
                    raise ExternalBridgeError("screenshot_invalid")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_PNG_BYTES:
                        raise ExternalBridgeError("screenshot_too_large")
                    chunks.append(chunk)
                png = b"".join(chunks)
                headers = response.headers
        except ExternalBridgeError:
            raise
        except httpx.TransportError as error:
            raise ExternalBridgeError("bridge_unreachable") from error
        digest = _digest(png)
        if headers.get("x-kodi-lab-protocol") != BRIDGE_PROTOCOL_VERSION:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        required = {
            "x-kodi-lab-operation": "capture_screenshot",
            "x-kodi-lab-target-id": str(target_id),
            "x-kodi-lab-bridge-id": str(bridge_id),
            "x-kodi-lab-request-id": str(request_id),
            "x-kodi-lab-status": "ok",
        }
        if any(headers.get(name) != value for name, value in required.items()):
            raise ExternalBridgeError("bridge_identity_mismatch")
        if headers.get("x-kodi-lab-content-sha256") != digest:
            raise ExternalBridgeError("bridge_authentication_failed")
        response_timestamp = headers.get("x-kodi-lab-timestamp")
        response_nonce = headers.get("x-kodi-lab-nonce")
        signature = headers.get("x-kodi-lab-signature")
        if response_timestamp is None or response_nonce is None or signature is None:
            raise ExternalBridgeError("bridge_authentication_failed")
        parse_fresh_timestamp(response_timestamp, self._clock())
        decode_b64url(response_nonce, size=32)
        expected_signature = response_signature(
            secret,
            target_id=str(target_id),
            bridge_id=str(bridge_id),
            operation="capture_screenshot",
            request_id=str(request_id),
            timestamp=response_timestamp,
            nonce=response_nonce,
            status="ok",
            payload_digest=digest,
        )
        if not hmac.compare_digest(expected_signature, signature):
            raise ExternalBridgeError("bridge_authentication_failed")
        try:
            validated = validate_png(png)
        except ScreenshotArtifactError as error:
            raise ExternalBridgeError("screenshot_invalid") from error
        try:
            width = int(headers["x-kodi-lab-width"])
            height = int(headers["x-kodi-lab-height"])
        except (KeyError, ValueError) as error:
            raise ExternalBridgeError("screenshot_invalid") from error
        if (width, height) != (validated.width, validated.height):
            raise ExternalBridgeError("screenshot_invalid")
        return BridgeScreenshotResult(
            request_id=request_id,
            bridge_id=bridge_id,
            png=png,
            width=width,
            height=height,
            digest=digest,
            captured_at=parse_fresh_timestamp(response_timestamp, self._clock()),
        )

    def _authenticated_json(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        operation: str,
    ) -> object:
        request_id = uuid4()
        envelope = self._envelope(
            target_id=target_id,
            bridge_id=bridge_id,
            operation=operation,
            request_id=request_id,
            nonce=b64url(os.urandom(32)),
            requested_at=self._clock(),
        )
        envelope["signature"] = request_signature(secret, envelope)
        document = self._post_json(endpoint, bridge_port, envelope)
        if not isinstance(document, dict) or set(document) != {
            "protocol_version",
            "operation",
            "target_id",
            "bridge_id",
            "request_id",
            "timestamp",
            "nonce",
            "status",
            "payload",
            "signature",
        }:
            raise ExternalBridgeError("bridge_authentication_failed")
        if document["protocol_version"] != BRIDGE_PROTOCOL_VERSION:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        if (
            document["operation"] != operation
            or document["target_id"] != str(target_id)
            or document["bridge_id"] != str(bridge_id)
            or document["request_id"] != str(request_id)
            or document["status"] != "ok"
            or not isinstance(document["timestamp"], str)
            or not isinstance(document["nonce"], str)
            or not isinstance(document["signature"], str)
        ):
            raise ExternalBridgeError("bridge_identity_mismatch")
        parse_fresh_timestamp(document["timestamp"], self._clock())
        decode_b64url(document["nonce"], size=32)
        expected = response_signature(
            secret,
            target_id=str(target_id),
            bridge_id=str(bridge_id),
            operation=operation,
            request_id=str(request_id),
            timestamp=document["timestamp"],
            nonce=document["nonce"],
            status="ok",
            payload_digest=_digest(canonical_json(document["payload"])),
        )
        if not hmac.compare_digest(expected, document["signature"]):
            raise ExternalBridgeError("bridge_authentication_failed")
        return document["payload"]

    @staticmethod
    def _identity(value: object) -> BridgeIdentity:
        if not isinstance(value, dict) or set(value) != {
            "bridge_id",
            "protocol_version",
            "addon_id",
            "addon_version",
            "kodi_version",
            "capabilities",
            "build_fingerprint",
        }:
            raise ExternalBridgeError("bridge_identity_mismatch")
        try:
            raw_bridge_id = value["bridge_id"]
            if not isinstance(raw_bridge_id, str):
                raise ValueError
            bridge_id = UUID(raw_bridge_id)
            if str(bridge_id) != raw_bridge_id:
                raise ValueError
        except ValueError as error:
            raise ExternalBridgeError("bridge_identity_mismatch") from error
        capabilities = value["capabilities"]
        if value["protocol_version"] != BRIDGE_PROTOCOL_VERSION:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        if (
            value["addon_id"] != BRIDGE_ADDON_ID
            or not isinstance(value["addon_version"], str)
            or not 1 <= len(value["addon_version"]) <= 64
            or not isinstance(value["kodi_version"], str)
            or not 1 <= len(value["kodi_version"]) <= 64
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
            or len(capabilities) != len(set(capabilities))
            or not isinstance(value["build_fingerprint"], str)
            or len(value["build_fingerprint"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in value["build_fingerprint"]
            )
        ):
            raise ExternalBridgeError("bridge_identity_mismatch")
        if not frozenset(capabilities) <= BRIDGE_CAPABILITIES:
            raise ExternalBridgeError("bridge_capability_unavailable")
        return BridgeIdentity(
            bridge_id=bridge_id,
            protocol_version=BRIDGE_PROTOCOL_VERSION,
            addon_id=BRIDGE_ADDON_ID,
            addon_version=value["addon_version"],
            kodi_version=value["kodi_version"],
            capabilities=tuple(sorted(capabilities)),
            build_fingerprint=value["build_fingerprint"],
        )

    def _post_json(
        self,
        endpoint: ValidatedExternalEndpoint,
        bridge_port: int,
        body: dict[str, object],
    ) -> object:
        try:
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    self._url(endpoint, bridge_port),
                    json=body,
                    timeout=self._timeout(),
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise ExternalBridgeError("bridge_authentication_failed")
                if response.status_code == 409:
                    raise ExternalBridgeError("bridge_replay_rejected")
                if response.status_code != 200 or 300 <= response.status_code < 400:
                    raise ExternalBridgeError("bridge_unreachable")
                if (
                    response.headers.get("content-type", "").split(";", 1)[0]
                    != "application/json"
                ):
                    raise ExternalBridgeError("bridge_authentication_failed")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BRIDGE_JSON_BYTES:
                        raise ExternalBridgeError("bridge_authentication_failed")
                    chunks.append(chunk)
            return json.loads(b"".join(chunks))
        except ExternalBridgeError:
            raise
        except httpx.TransportError as error:
            raise ExternalBridgeError("bridge_unreachable") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalBridgeError("bridge_authentication_failed") from error

    @staticmethod
    def _envelope(
        *,
        target_id: UUID,
        bridge_id: UUID,
        operation: str,
        request_id: UUID,
        nonce: str,
        requested_at: datetime,
    ) -> dict[str, object]:
        if operation not in BRIDGE_OPERATIONS:
            raise ValueError("operation is outside the closed bridge protocol")
        return {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "operation": operation,
            "target_id": str(target_id),
            "bridge_id": str(bridge_id),
            "request_id": str(request_id),
            "timestamp": timestamp_text(requested_at),
            "nonce": nonce,
            "payload": {},
        }

    @staticmethod
    def _url(endpoint: ValidatedExternalEndpoint, bridge_port: int) -> str:
        scheme = "http" if endpoint.protocol_kind.value == "jsonrpc_http" else "https"
        host = f"[{endpoint.address}]" if ":" in endpoint.address else endpoint.address
        return f"{scheme}://{host}:{bridge_port}{BRIDGE_ENDPOINT_PATH}"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            transport=self._transport,
        )

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._response_timeout, connect=self._connect_timeout)

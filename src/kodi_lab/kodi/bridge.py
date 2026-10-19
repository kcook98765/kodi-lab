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
from pydantic import ValidationError

from kodi_lab.kodi.external import ValidatedExternalEndpoint
from kodi_lab.kodi.launch import BridgeLaunchIntent, BridgeLaunchReceipt
from kodi_lab.kodi.logs import KodiLogRequest, KodiLogResult
from kodi_lab.kodi.screenshots import (
    MAX_PNG_BYTES,
    PNG_MIME_TYPE,
    ScreenshotArtifactError,
    validate_external_png,
)
from kodi_lab.kodi.ui_state import UiCollection
from kodi_lab.screenshot_diagnostics import emit_screenshot_stage

BRIDGE_PROTOCOL_VERSION = "1"
BRIDGE_ENDPOINT_PATH = "/kodi-lab/bridge/v1"
BOOTSTRAP_ENDPOINT_PATH = "/kodi-lab/bridge/bootstrap/v1"
BOOTSTRAP_PROTOCOL_VERSION = "otp-1"
BRIDGE_ADDON_ID = "service.kodi_lab_bridge"
SUPPORTED_V1_BRIDGE_ADDON_VERSION = "1.1.25"
OTP_CODE_ALPHABET = frozenset("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
BRIDGE_CAPABILITIES = frozenset(
    {"screenshot", "workspace_deployment", "ui_observation", "log_read"}
)
BRIDGE_JSON_ERROR_CODES = frozenset(
    {
        "launch_dispatch_unconfirmed",
        "launch_request_rejected",
        "log_request_invalid",
        "log_read_busy",
        "log_missing",
        "log_changed",
        "log_unavailable",
        "deployment_request_invalid",
        "deployment_installation_failed",
        "deployment_cleanup_failed",
        "addon_inspection_failed",
        "bridge_operation_failed",
        "provisional_operation_not_allowed",
        "conflicting_commit",
        "bootstrap_version_mismatch",
    }
)
BRIDGE_OPERATIONS = frozenset(
    {
        "managed_launch",
        "managed_launch_receipt",
        "log_read",
        "inspect_ui_state",
        "bridge_identity",
        "bridge_capabilities",
        "capture_screenshot",
        "inspect_addon",
        "deployment_begin",
        "deployment_chunk",
        "deployment_commit",
        "deployment_receipt",
        "pairing_commit",
    }
)
PAIRING_OPERATION = "pair_bridge"
MAX_BRIDGE_JSON_BYTES = 65_536
MAX_DEPLOYMENT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DEPLOYMENT_FILE_BYTES = 2 * 1024 * 1024
DEPLOYMENT_CHUNK_BYTES = 256 * 1024
MAX_DEPLOYMENT_FILES = 25_000
MAX_DEPLOYMENT_ENTRIES = 50_000
MAX_DEPLOYMENT_BYTES = 1024 * 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 60
SCREENSHOT_READ_TIMEOUT_SECONDS = 20.0
SCREENSHOT_ERROR_CODES = frozenset(
    {
        "screenshot_capture_failed",
        "screenshot_invalid",
        "screenshot_too_large",
        "screenshot_cleanup_failed",
        "screenshot_payload_missing",
        "screenshot_payload_type_invalid",
        "screenshot_response_schema_invalid",
        "screenshot_decoder_rejected",
        "screenshot_format_invalid",
        "screenshot_dimensions_invalid",
    }
)


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
    kodi_release: str | None = None


@dataclass(frozen=True, slots=True)
class BridgePairingResult:
    identity: BridgeIdentity
    secret: bytes


@dataclass(frozen=True, slots=True)
class BridgeBootstrapInfo:
    bridge_id: UUID
    addon_version: str
    build_fingerprint: str
    bootstrap_version: str


@dataclass(frozen=True, slots=True)
class BridgeOtpBootstrapResult:
    target_id: UUID
    bridge_id: UUID
    request_id: UUID
    secret: bytes
    provisional_expires_at: float


@dataclass(frozen=True, slots=True)
class BridgeScreenshotResult:
    request_id: UUID
    bridge_id: UUID
    png: bytes
    width: int
    height: int
    digest: str
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class BridgeAddonTree:
    addon_id: str
    addon_version: str
    manifest_digest: str
    file_count: int
    total_bytes: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class BridgeDeploymentReceipt:
    receipt_id: UUID | None
    deployment_id: UUID
    request_id: UUID
    bridge_id: UUID
    addon_id: str
    addon_version: str
    generation: int
    manifest_digest: str
    file_count: int
    total_bytes: int
    state: str
    classification: str
    enabled_state: str
    inventory_verified: bool
    completed_at: datetime | None


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


def otp_bootstrap_request_id(secret: bytes, target_id: UUID) -> UUID:
    """Recover the bootstrap transaction ID from the staged random credential."""
    if len(secret) != 32:
        raise ValueError("persistent credential must be 256 bits")
    digest = hmac.new(
        secret,
        b"kodi-lab-otp-bootstrap-request-v1|" + target_id.bytes,
        hashlib.sha256,
    ).digest()
    return UUID(bytes=digest[:16], version=4)


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

    def bootstrap_info(
        self, endpoint: ValidatedExternalEndpoint, *, bridge_port: int
    ) -> BridgeBootstrapInfo:
        """Read the bounded, unpaired-only OTP bootstrap advertisement."""
        try:
            with (
                self._client() as client,
                client.stream(
                    "GET",
                    self._url(endpoint, bridge_port, BOOTSTRAP_ENDPOINT_PATH),
                    timeout=self._timeout(),
                ) as response,
            ):
                document = self._bootstrap_document(response)
        except httpx.TransportError as error:
            raise ExternalBridgeError("bridge_unreachable") from error
        if (
            not isinstance(document, dict)
            or set(document)
            != {
                "status",
                "bootstrap_version",
                "bridge_id",
                "addon_version",
                "build_fingerprint",
            }
            or document["status"] != "available"
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        if document["bootstrap_version"] != BOOTSTRAP_PROTOCOL_VERSION:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        try:
            bridge_id = UUID(document["bridge_id"])
            if str(bridge_id) != document["bridge_id"]:
                raise ValueError
        except (TypeError, ValueError, AttributeError) as error:
            raise ExternalBridgeError("bridge_identity_mismatch") from error
        version = document["addon_version"]
        fingerprint = document["build_fingerprint"]
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 64
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise ExternalBridgeError("bridge_identity_mismatch")
        return BridgeBootstrapInfo(
            bridge_id, version, fingerprint, BOOTSTRAP_PROTOCOL_VERSION
        )

    def bootstrap_otp(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        info: BridgeBootstrapInfo,
        code: str,
        secret: bytes,
        request_id: UUID,
    ) -> BridgeOtpBootstrapResult:
        """Submit one OTP attempt. The caller retains the new key on lost response."""
        if (
            len(secret) != 32
            or len(code) != 4
            or any(char not in OTP_CODE_ALPHABET for char in code)
        ):
            raise ValueError("OTP or persistent credential shape is invalid")
        nonce = b64url(os.urandom(32))
        request = {
            "bootstrap_version": BOOTSTRAP_PROTOCOL_VERSION,
            "operation": "otp_bootstrap",
            "target_id": str(target_id),
            "bridge_id": str(info.bridge_id),
            "request_id": str(request_id),
            "timestamp": timestamp_text(self._clock()),
            "nonce": nonce,
            "credential": b64url(secret),
            "otp": code,
        }
        try:
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    self._url(endpoint, bridge_port, BOOTSTRAP_ENDPOINT_PATH),
                    json=request,
                    timeout=self._timeout(),
                ) as response,
            ):
                document = self._bootstrap_document(response)
        except httpx.TransportError as error:
            raise ExternalBridgeError("bridge_unreachable") from error
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
        if (
            document["protocol_version"] != BRIDGE_PROTOCOL_VERSION
            or document["operation"] != "otp_bootstrap"
            or document["target_id"] != str(target_id)
            or document["bridge_id"] != str(info.bridge_id)
            or document["request_id"] != str(request_id)
            or document["status"] != "ok"
            or not isinstance(document["timestamp"], str)
            or not isinstance(document["nonce"], str)
            or not isinstance(document["signature"], str)
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        parse_fresh_timestamp(document["timestamp"], self._clock())
        decode_b64url(document["nonce"], size=32)
        payload = document["payload"]
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "bootstrap_version",
                "target_id",
                "bridge_id",
                "bootstrap_request_id",
                "bootstrap_nonce",
                "credential_digest",
                "provisional_expires_at",
            }
            or (
                payload["bootstrap_version"] != BOOTSTRAP_PROTOCOL_VERSION
                or payload["target_id"] != str(target_id)
                or payload["bridge_id"] != str(info.bridge_id)
                or payload["bootstrap_request_id"] != str(request_id)
                or payload["bootstrap_nonce"] != nonce
                or payload["credential_digest"] != hashlib.sha256(secret).hexdigest()
                or type(payload["provisional_expires_at"]) not in (int, float)
            )
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        expected = response_signature(
            secret,
            target_id=str(target_id),
            bridge_id=str(info.bridge_id),
            operation="otp_bootstrap",
            request_id=str(request_id),
            timestamp=document["timestamp"],
            nonce=document["nonce"],
            status="ok",
            payload_digest=_digest(canonical_json(payload)),
        )
        if not hmac.compare_digest(expected, document["signature"]):
            raise ExternalBridgeError("bridge_authentication_failed")
        expires_at = float(payload["provisional_expires_at"])
        now = self._clock().timestamp()
        if not now < expires_at <= now + 300 + MAX_CLOCK_SKEW_SECONDS:
            raise ExternalBridgeError("bridge_authentication_failed")
        return BridgeOtpBootstrapResult(
            target_id, info.bridge_id, request_id, secret, expires_at
        )

    def commit_pairing(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        transaction: BridgeOtpBootstrapResult,
    ) -> None:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=transaction.target_id,
            bridge_id=transaction.bridge_id,
            secret=transaction.secret,
            operation="pairing_commit",
            payload={
                "bootstrap_version": BOOTSTRAP_PROTOCOL_VERSION,
                "bootstrap_request_id": str(transaction.request_id),
                "credential_digest": hashlib.sha256(transaction.secret).hexdigest(),
            },
        )
        if payload != {
            "bootstrap_version": BOOTSTRAP_PROTOCOL_VERSION,
            "committed": True,
        }:
            raise ExternalBridgeError("bridge_authentication_failed")

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

    def read_log(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        request: KodiLogRequest,
    ) -> tuple[BridgeIdentity, KodiLogResult]:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="log_read",
            payload=request.model_dump(),
        )
        try:
            if not isinstance(payload, dict) or set(payload) != {"identity", "log"}:
                raise ValueError("invalid log envelope")
            result = KodiLogResult.model_validate(payload["log"])
            result.require_request(request)
            return self._identity(payload["identity"]), result
        except ValueError as error:
            raise ExternalBridgeError("bridge_operation_failed") from error

    def inspect_ui_state(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
    ) -> tuple[BridgeIdentity, UiCollection]:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="inspect_ui_state",
        )
        if not isinstance(payload, dict) or set(payload) != {"identity", "observation"}:
            raise ExternalBridgeError("bridge_operation_failed")
        try:
            return self._identity(payload["identity"]), UiCollection.model_validate(
                payload["observation"]
            )
        except ValidationError as error:
            raise ExternalBridgeError("bridge_operation_failed") from error

    def managed_launch(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        intent: BridgeLaunchIntent,
        status_only: bool = False,
    ) -> tuple[BridgeIdentity, BridgeLaunchReceipt | None]:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="managed_launch_receipt" if status_only else "managed_launch",
            payload={"launch_id": str(intent.launch_id)}
            if status_only
            else intent.model_dump(mode="json"),
        )
        try:
            if not isinstance(payload, dict) or set(payload) != {"identity", "receipt"}:
                raise ValueError("launch response invalid")
            identity = self._identity(payload["identity"])
            receipt = (
                None
                if payload["receipt"] is None
                else BridgeLaunchReceipt.model_validate(payload["receipt"])
            )
            if receipt is not None and (
                receipt.intent != intent
                or receipt.launch_id != intent.launch_id
                or (receipt.state == "accepted") != (receipt.accepted_at is not None)
            ):
                raise ValueError("launch response mismatch")
            return identity, receipt
        except (ValidationError, ValueError) as error:
            raise ExternalBridgeError("bridge_operation_failed") from error

    def inspect_addon(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        addon_id: str,
    ) -> BridgeAddonTree:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="inspect_addon",
            payload={"addon_id": addon_id},
        )
        return self._addon_tree(payload, addon_id)

    def deployment_begin(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        deployment_id: UUID,
        request_id: UUID,
        addon_id: str,
        addon_version: str,
        generation: int,
        manifest_digest: str,
        entries: list[dict[str, object]],
        file_count: int,
        total_bytes: int,
    ) -> BridgeDeploymentReceipt:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="deployment_begin",
            payload={
                "addon_id": addon_id,
                "addon_version": addon_version,
                "deployment_id": str(deployment_id),
                "deployment_request_id": str(request_id),
                "entries": entries,
                "file_count": file_count,
                "generation": generation,
                "manifest_digest": manifest_digest,
                "manifest_version": 1,
                "total_bytes": total_bytes,
            },
        )
        return self._deployment_receipt(payload)

    def deployment_chunk(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        deployment_id: UUID,
        path: str,
        file_digest: str,
        offset: int,
        content: bytes,
    ) -> BridgeDeploymentReceipt:
        if not content or len(content) > DEPLOYMENT_CHUNK_BYTES:
            raise ValueError("deployment chunk is outside the fixed bound")
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="deployment_chunk",
            payload={
                "content": b64url(content),
                "content_digest": hashlib.sha256(content).hexdigest(),
                "deployment_id": str(deployment_id),
                "file_digest": file_digest,
                "offset": offset,
                "path": path,
            },
        )
        return self._deployment_receipt(payload)

    def deployment_commit(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        deployment_id: UUID,
        manifest_digest: str,
    ) -> BridgeDeploymentReceipt:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="deployment_commit",
            payload={
                "deployment_id": str(deployment_id),
                "manifest_digest": manifest_digest,
            },
        )
        return self._deployment_receipt(payload)

    def deployment_receipt(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        deployment_id: UUID,
    ) -> BridgeDeploymentReceipt:
        payload = self._authenticated_json(
            endpoint,
            bridge_port=bridge_port,
            target_id=target_id,
            bridge_id=bridge_id,
            secret=secret,
            operation="deployment_receipt",
            payload={"deployment_id": str(deployment_id)},
        )
        return self._deployment_receipt(payload)

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
        emit_screenshot_stage(
            "bridge_request_prepared", "passed", "signed_request_prepared"
        )
        try:
            emit_screenshot_stage(
                "bridge_request_sent", "entered", "bridge_request_send_entered"
            )
            with (
                self._client() as client,
                client.stream(
                    "POST",
                    self._url(endpoint, bridge_port),
                    json=envelope,
                    timeout=self._screenshot_timeout(),
                ) as response,
            ):
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                emit_screenshot_stage(
                    "bridge_response_received",
                    "passed",
                    "http_response_received",
                    http_status=response.status_code,
                    content_type=content_type,
                )
                if response.status_code in {401, 403}:
                    emit_screenshot_stage(
                        "response_schema",
                        "failed",
                        "bridge_authentication_failed",
                        http_status=response.status_code,
                        content_type=content_type,
                    )
                    raise ExternalBridgeError("bridge_authentication_failed")
                if 300 <= response.status_code < 400:
                    raise ExternalBridgeError("bridge_unreachable")
                if response.status_code == 409:
                    raise ExternalBridgeError("bridge_replay_rejected")
                if response.status_code == 422:
                    error_body = response.read()
                    if error_body:
                        raise ExternalBridgeError("bridge_authentication_failed")
                    code = self._screenshot_error_code(
                        response.headers,
                        target_id=target_id,
                        bridge_id=bridge_id,
                        request_id=request_id,
                        secret=secret,
                    )
                    emit_screenshot_stage(
                        "bridge_operation_returned",
                        "failed",
                        code,
                        http_status=response.status_code,
                        content_type=content_type,
                    )
                    raise ExternalBridgeError(code)
                if response.status_code != 200:
                    raise ExternalBridgeError("bridge_unreachable")
                if content_type != PNG_MIME_TYPE:
                    emit_screenshot_stage(
                        "response_schema",
                        "failed",
                        "screenshot_payload_type_invalid",
                        http_status=response.status_code,
                        content_type=content_type,
                    )
                    raise ExternalBridgeError("screenshot_payload_type_invalid")
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
            emit_screenshot_stage("bridge_request_sent", "failed", "bridge_unreachable")
            raise ExternalBridgeError("bridge_unreachable") from error
        digest = _digest(png)
        if headers.get("x-kodi-lab-protocol") != BRIDGE_PROTOCOL_VERSION:
            emit_screenshot_stage(
                "response_schema", "failed", "bridge_protocol_unsupported"
            )
            raise ExternalBridgeError("bridge_protocol_unsupported")
        required = {
            "x-kodi-lab-operation": "capture_screenshot",
            "x-kodi-lab-target-id": str(target_id),
            "x-kodi-lab-bridge-id": str(bridge_id),
            "x-kodi-lab-request-id": str(request_id),
            "x-kodi-lab-status": "ok",
        }
        if any(headers.get(name) is None for name in required):
            emit_screenshot_stage(
                "response_schema", "failed", "screenshot_response_schema_invalid"
            )
            raise ExternalBridgeError("screenshot_response_schema_invalid")
        if any(headers.get(name) != value for name, value in required.items()):
            emit_screenshot_stage(
                "response_schema", "failed", "bridge_identity_mismatch"
            )
            raise ExternalBridgeError("bridge_identity_mismatch")
        if headers.get("x-kodi-lab-content-sha256") != digest:
            raise ExternalBridgeError("bridge_authentication_failed")
        response_timestamp = headers.get("x-kodi-lab-timestamp")
        response_nonce = headers.get("x-kodi-lab-nonce")
        signature = headers.get("x-kodi-lab-signature")
        if response_timestamp is None or response_nonce is None or signature is None:
            emit_screenshot_stage(
                "response_schema", "failed", "screenshot_response_schema_invalid"
            )
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
        emit_screenshot_stage(
            "response_schema", "passed", "signed_response_schema_accepted"
        )
        if not png:
            emit_screenshot_stage(
                "bridge_operation_returned",
                "failed",
                "screenshot_payload_missing",
                http_status=200,
                content_type=PNG_MIME_TYPE,
                response_byte_length=0,
                payload_present=False,
            )
            raise ExternalBridgeError("screenshot_payload_missing")
        emit_screenshot_stage(
            "bridge_operation_returned",
            "passed",
            "screenshot_payload_returned",
            http_status=200,
            content_type=PNG_MIME_TYPE,
            response_byte_length=len(png),
            payload_present=True,
        )
        emit_screenshot_stage(
            "image_payload",
            "passed",
            "raw_png_payload_received",
            decoded_byte_length=len(png),
            image_sha256=digest,
            detected_format="png"
            if png.startswith(b"\x89PNG\r\n\x1a\n")
            else "unknown",
        )
        try:
            validated = validate_external_png(png)
        except ScreenshotArtifactError as error:
            code = self._screenshot_validation_code(error)
            emit_screenshot_stage("image_validation", "failed", code)
            raise ExternalBridgeError(code) from error
        try:
            width = int(headers["x-kodi-lab-width"])
            height = int(headers["x-kodi-lab-height"])
        except (KeyError, ValueError) as error:
            emit_screenshot_stage(
                "response_schema", "failed", "screenshot_response_schema_invalid"
            )
            raise ExternalBridgeError("screenshot_response_schema_invalid") from error
        if (width, height) != (validated.width, validated.height):
            emit_screenshot_stage(
                "image_validation", "failed", "screenshot_dimensions_invalid"
            )
            raise ExternalBridgeError("screenshot_dimensions_invalid")
        emit_screenshot_stage(
            "image_validation",
            "passed",
            "png_validated_unchanged",
            decoded_byte_length=len(png),
            image_sha256=digest,
            detected_format="png",
            width=width,
            height=height,
            color_mode="rgb",
        )
        return BridgeScreenshotResult(
            request_id=request_id,
            bridge_id=bridge_id,
            png=png,
            width=width,
            height=height,
            digest=digest,
            captured_at=parse_fresh_timestamp(response_timestamp, self._clock()),
        )

    @staticmethod
    def _screenshot_validation_code(error: ScreenshotArtifactError) -> str:
        message = str(error)
        if "signature" in message or "encoding" in message or "chunk layout" in message:
            return "screenshot_format_invalid"
        if "dimension" in message or "geometry" in message:
            return "screenshot_dimensions_invalid"
        return "screenshot_decoder_rejected"

    def _screenshot_error_code(
        self,
        headers: httpx.Headers,
        *,
        target_id: UUID,
        bridge_id: UUID,
        request_id: UUID,
        secret: bytes,
    ) -> str:
        if (
            headers.get("content-type", "").split(";", 1)[0]
            != "application/octet-stream"
            or headers.get("x-kodi-lab-protocol") != BRIDGE_PROTOCOL_VERSION
            or headers.get("x-kodi-lab-operation") != "capture_screenshot"
            or headers.get("x-kodi-lab-target-id") != str(target_id)
            or headers.get("x-kodi-lab-bridge-id") != str(bridge_id)
            or headers.get("x-kodi-lab-request-id") != str(request_id)
            or headers.get("x-kodi-lab-content-sha256") != _digest(b"")
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        status = headers.get("x-kodi-lab-status")
        timestamp = headers.get("x-kodi-lab-timestamp")
        nonce = headers.get("x-kodi-lab-nonce")
        signature = headers.get("x-kodi-lab-signature")
        if (
            status not in SCREENSHOT_ERROR_CODES
            or timestamp is None
            or nonce is None
            or signature is None
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        parse_fresh_timestamp(timestamp, self._clock())
        decode_b64url(nonce, size=32)
        expected = response_signature(
            secret,
            target_id=str(target_id),
            bridge_id=str(bridge_id),
            operation="capture_screenshot",
            request_id=str(request_id),
            timestamp=timestamp,
            nonce=nonce,
            status=status,
            payload_digest=_digest(b""),
        )
        if not hmac.compare_digest(expected, signature):
            raise ExternalBridgeError("bridge_authentication_failed")
        return str(status)

    def _authenticated_json(
        self,
        endpoint: ValidatedExternalEndpoint,
        *,
        bridge_port: int,
        target_id: UUID,
        bridge_id: UUID,
        secret: bytes,
        operation: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        request_id = uuid4()
        envelope = self._envelope(
            target_id=target_id,
            bridge_id=bridge_id,
            operation=operation,
            request_id=request_id,
            nonce=b64url(os.urandom(32)),
            requested_at=self._clock(),
            payload={} if payload is None else payload,
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
            or not isinstance(document["status"], str)
            or not isinstance(document["timestamp"], str)
            or not isinstance(document["nonce"], str)
            or not isinstance(document["signature"], str)
        ):
            raise ExternalBridgeError("bridge_identity_mismatch")
        parse_fresh_timestamp(document["timestamp"], self._clock())
        decode_b64url(document["nonce"], size=32)
        status = document["status"]
        if status != "ok" and status not in BRIDGE_JSON_ERROR_CODES:
            raise ExternalBridgeError("bridge_authentication_failed")
        expected = response_signature(
            secret,
            target_id=str(target_id),
            bridge_id=str(bridge_id),
            operation=operation,
            request_id=str(request_id),
            timestamp=document["timestamp"],
            nonce=document["nonce"],
            status=status,
            payload_digest=_digest(canonical_json(document["payload"])),
        )
        if not hmac.compare_digest(expected, document["signature"]):
            raise ExternalBridgeError("bridge_authentication_failed")
        if status != "ok":
            raise ExternalBridgeError(status)
        return document["payload"]

    @staticmethod
    def _identity(value: object) -> BridgeIdentity:
        if not isinstance(value, dict) or set(value) - {"kodi_release"} != {
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
        release = value.get("kodi_release")
        if release is not None:
            from kodi_lab.domain import KodiReleaseIdentity

            try:
                parsed = KodiReleaseIdentity.parse(release)
                if (
                    release != parsed.canonical
                    or value["kodi_version"] != f"{parsed.major}.{parsed.minor}"
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise ExternalBridgeError("bridge_identity_mismatch") from None
        return BridgeIdentity(
            bridge_id=bridge_id,
            protocol_version=BRIDGE_PROTOCOL_VERSION,
            addon_id=BRIDGE_ADDON_ID,
            addon_version=value["addon_version"],
            kodi_version=value["kodi_version"],
            capabilities=tuple(sorted(capabilities)),
            build_fingerprint=value["build_fingerprint"],
            kodi_release=release,
        )

    @staticmethod
    def _addon_tree(value: object, addon_id: str) -> BridgeAddonTree:
        if not isinstance(value, dict) or set(value) != {
            "addon_id",
            "addon_version",
            "enabled",
            "file_count",
            "manifest_digest",
            "total_bytes",
        }:
            raise ExternalBridgeError("target_provenance_unknown")
        if (
            value["addon_id"] != addon_id
            or not isinstance(value["addon_version"], str)
            or not 1 <= len(value["addon_version"]) <= 128
            or type(value["enabled"]) is not bool
            or type(value["file_count"]) is not int
            or not 1 <= value["file_count"] <= MAX_DEPLOYMENT_FILES
            or type(value["total_bytes"]) is not int
            or not 1 <= value["total_bytes"] <= MAX_DEPLOYMENT_BYTES
            or not isinstance(value["manifest_digest"], str)
            or len(value["manifest_digest"]) != 71
            or not value["manifest_digest"].startswith("sha256:")
        ):
            raise ExternalBridgeError("target_provenance_unknown")
        return BridgeAddonTree(
            addon_id=addon_id,
            addon_version=value["addon_version"],
            manifest_digest=value["manifest_digest"],
            file_count=value["file_count"],
            total_bytes=value["total_bytes"],
            enabled=value["enabled"],
        )

    @staticmethod
    def _deployment_receipt(value: object) -> BridgeDeploymentReceipt:
        expected = {
            "addon_id",
            "addon_version",
            "bridge_id",
            "classification",
            "completed_at",
            "deployment_id",
            "deployment_request_id",
            "enabled_state",
            "file_count",
            "generation",
            "inventory_verified",
            "manifest_digest",
            "receipt_id",
            "state",
            "total_bytes",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ExternalBridgeError("deployment_ambiguous")
        try:
            deployment_id = UUID(str(value["deployment_id"]))
            request_id = UUID(str(value["deployment_request_id"]))
            bridge_id = UUID(str(value["bridge_id"]))
            receipt_id = (
                None if value["receipt_id"] is None else UUID(str(value["receipt_id"]))
            )
            completed_at = (
                None
                if value["completed_at"] is None
                else datetime.fromisoformat(
                    str(value["completed_at"]).replace("Z", "+00:00")
                )
            )
        except (TypeError, ValueError) as error:
            raise ExternalBridgeError("deployment_ambiguous") from error
        if (
            value["state"]
            not in {"staging", "ready", "succeeded", "failed", "ambiguous"}
            or not isinstance(value["addon_id"], str)
            or not isinstance(value["addon_version"], str)
            or type(value["generation"]) is not int
            or value["generation"] < 0
            or not isinstance(value["manifest_digest"], str)
            or len(value["manifest_digest"]) != 71
            or type(value["file_count"]) is not int
            or not 1 <= value["file_count"] <= MAX_DEPLOYMENT_FILES
            or type(value["total_bytes"]) is not int
            or not 1 <= value["total_bytes"] <= MAX_DEPLOYMENT_BYTES
            or not isinstance(value["classification"], str)
            or not 1 <= len(value["classification"]) <= 64
            or value["enabled_state"] not in {"enabled", "disabled", "unknown"}
            or type(value["inventory_verified"]) is not bool
        ):
            raise ExternalBridgeError("deployment_ambiguous")
        return BridgeDeploymentReceipt(
            receipt_id=receipt_id,
            deployment_id=deployment_id,
            request_id=request_id,
            bridge_id=bridge_id,
            addon_id=value["addon_id"],
            addon_version=value["addon_version"],
            generation=value["generation"],
            manifest_digest=value["manifest_digest"],
            file_count=value["file_count"],
            total_bytes=value["total_bytes"],
            state=value["state"],
            classification=value["classification"],
            enabled_state=value["enabled_state"],
            inventory_verified=value["inventory_verified"],
            completed_at=completed_at,
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
                if response.status_code == 410:
                    self._bootstrap_document(response)
                    raise ExternalBridgeError("bridge_authentication_failed")
                if response.status_code not in {200, 422}:
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
    def _bootstrap_document(response: httpx.Response) -> object:
        if response.status_code == 404:
            raise ExternalBridgeError("bridge_protocol_unsupported")
        if response.status_code not in {200, 400, 403, 409, 410, 429, 503}:
            raise ExternalBridgeError("bridge_unreachable")
        if (
            response.headers.get("content-type", "").split(";", 1)[0]
            != "application/json"
        ):
            raise ExternalBridgeError("bridge_authentication_failed")
        parts: list[bytes] = []
        total = 0
        for part in response.iter_bytes():
            total += len(part)
            if total > 4096:
                raise ExternalBridgeError("bridge_authentication_failed")
            parts.append(part)
        try:
            document = json.loads(b"".join(parts))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalBridgeError("bridge_authentication_failed") from error
        if response.status_code != 200:
            allowed = {
                "pairing_request_invalid",
                "bootstrap_version_mismatch",
                "bridge_identity_mismatch",
                "wrong_code",
                "pairing_code_expired",
                "pairing_code_unavailable",
                "pairing_temporarily_locked",
                "pairing_in_progress",
                "bootstrap_replay_rejected",
                "already_paired",
                "bridge_disabled",
                "pairing_unavailable",
                "provisional_expired",
            }
            if (
                not isinstance(document, dict)
                or set(document) != {"status", "code"}
                or (document["status"] != "error" or document["code"] not in allowed)
            ):
                raise ExternalBridgeError("bridge_authentication_failed")
            raise ExternalBridgeError(document["code"])
        return document

    @staticmethod
    def _envelope(
        *,
        target_id: UUID,
        bridge_id: UUID,
        operation: str,
        request_id: UUID,
        nonce: str,
        requested_at: datetime,
        payload: dict[str, object] | None = None,
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
            "payload": {} if payload is None else payload,
        }

    @staticmethod
    def _url(
        endpoint: ValidatedExternalEndpoint,
        bridge_port: int,
        path: str = BRIDGE_ENDPOINT_PATH,
    ) -> str:
        scheme = "http" if endpoint.protocol_kind.value == "jsonrpc_http" else "https"
        host = f"[{endpoint.address}]" if ":" in endpoint.address else endpoint.address
        return f"{scheme}://{host}:{bridge_port}{path}"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            transport=self._transport,
        )

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._response_timeout, connect=self._connect_timeout)

    def _screenshot_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            self._response_timeout,
            connect=self._connect_timeout,
            read=SCREENSHOT_READ_TIMEOUT_SECONDS,
        )

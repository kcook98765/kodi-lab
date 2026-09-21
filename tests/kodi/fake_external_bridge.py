"""Deterministic fixed-endpoint fake for Phase 13 bridge tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import zlib
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

from kodi_lab.kodi.bridge import (
    BRIDGE_ADDON_ID,
    BRIDGE_ENDPOINT_PATH,
    BRIDGE_PROTOCOL_VERSION,
    b64url,
    canonical_json,
    decode_b64url,
    derive_bridge_secret,
    derive_pairing_key,
    parse_fresh_timestamp,
    request_signature,
    response_signature,
    timestamp_text,
)
from kodi_lab.kodi.screenshots import (
    EXPECTED_HEIGHT,
    EXPECTED_WIDTH,
    MAX_PNG_BYTES,
    PNG_SIGNATURE,
)


def _chunk(kind: bytes, data: bytes) -> bytes:
    import binascii

    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def fake_png(*, width: int = EXPECTED_WIDTH, height: int = EXPECTED_HEIGHT) -> bytes:
    row = b"\x00" + (b"\x10\x20\x30" * width)
    raw = row * height
    return b"".join(
        (
            PNG_SIGNATURE,
            _chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            ),
            _chunk(b"IDAT", zlib.compress(raw)),
            _chunk(b"IEND", b""),
        )
    )


class FakeExternalBridge:
    def __init__(self, *, target_id: UUID, token: bytes) -> None:
        self.target_id = target_id
        self.token = token
        self.bridge_id = uuid4()
        self.secret: bytes | None = None
        self.protocol_version = BRIDGE_PROTOCOL_VERSION
        self.kodi_version = "21.3"
        self.addon_version = "1.0.0"
        self.build_fingerprint = hashlib.sha256(b"fake-bridge-build").hexdigest()
        self.capabilities = ["screenshot", "workspace_deployment"]
        self.requests: dict[str, tuple[bytes, int, dict[str, str], bytes]] = {}
        self.nonces: set[str] = set()
        self.capture_count = 0
        self.replacement_count = 0
        self.addon_id = "plugin.video.fixture"
        self.addon_version = "1.0.0"
        self.addon_manifest_digest = (
            "sha256:" + hashlib.sha256(b"initial-tree").hexdigest()
        )
        self.addon_file_count = 1
        self.addon_total_bytes = 1
        self.addon_enabled = True
        self.deployments: dict[str, dict[str, object]] = {}
        self.tamper: str | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_port

    def __enter__(self) -> FakeExternalBridge:
        self._thread.start()
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def identity(self) -> dict[str, object]:
        return {
            "bridge_id": str(self.bridge_id),
            "protocol_version": self.protocol_version,
            "addon_id": BRIDGE_ADDON_ID,
            "addon_version": self.addon_version,
            "kodi_version": self.kodi_version,
            "capabilities": self.capabilities,
            "build_fingerprint": self.build_fingerprint,
        }

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                if owner.tamper == "redirect":
                    self._send(
                        307,
                        {"Location": "http://169.254.169.254/latest/meta-data"},
                        b"",
                    )
                    return
                if self.path != BRIDGE_ENDPOINT_PATH:
                    self._send(404, {}, b"")
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                digest = hashlib.sha256(raw).digest()
                try:
                    document = json.loads(raw)
                    request_id = document["request_id"]
                    cached = owner.requests.get(request_id)
                    if cached is not None:
                        if cached[0] != digest:
                            self._send(409, {}, b"")
                        else:
                            self._send(cached[1], cached[2], cached[3])
                        return
                    if document["operation"] == "pair_bridge":
                        self._pair(document, digest)
                    else:
                        self._authenticated(document, digest)
                except Exception:
                    self._send(401, {}, b"")

            def _pair(self, document: dict[str, object], digest: bytes) -> None:
                if set(document) != {
                    "protocol_version",
                    "operation",
                    "target_id",
                    "request_id",
                    "timestamp",
                    "server_nonce",
                    "pairing_proof",
                }:
                    raise ValueError
                parse_fresh_timestamp(document["timestamp"], datetime.now(UTC))
                if document["protocol_version"] != BRIDGE_PROTOCOL_VERSION or document[
                    "target_id"
                ] != str(owner.target_id):
                    raise ValueError
                proof_fields = dict(document)
                provided_proof = proof_fields.pop("pairing_proof")
                expected_proof = b64url(
                    hmac.new(
                        derive_pairing_key(owner.token),
                        canonical_json(proof_fields),
                        hashlib.sha256,
                    ).digest()
                )
                if not isinstance(provided_proof, str) or not hmac.compare_digest(
                    provided_proof, expected_proof
                ):
                    raise ValueError
                server_nonce = str(document["server_nonce"])
                decode_b64url(server_nonce, size=32)
                bridge_nonce = b64url(os.urandom(32))
                response: dict[str, object] = {
                    "protocol_version": owner.protocol_version,
                    "operation": "pair_bridge",
                    "target_id": document["target_id"],
                    "request_id": document["request_id"],
                    "timestamp": timestamp_text(datetime.now(UTC)),
                    "bridge_nonce": bridge_nonce,
                    "identity": owner.identity(),
                }
                response["pairing_proof"] = b64url(
                    hmac.new(
                        derive_pairing_key(owner.token),
                        canonical_json(response),
                        hashlib.sha256,
                    ).digest()
                )
                owner.secret = derive_bridge_secret(
                    owner.token,
                    target_id=owner.target_id,
                    bridge_id=owner.bridge_id,
                    server_nonce=server_nonce,
                    bridge_nonce=bridge_nonce,
                )
                self._cache(document, digest, response)

            def _authenticated(
                self, document: dict[str, object], digest: bytes
            ) -> None:
                if owner.secret is None:
                    raise ValueError
                parse_fresh_timestamp(document["timestamp"], datetime.now(UTC))
                if (
                    document["protocol_version"] != owner.protocol_version
                    or document["operation"]
                    not in {
                        "bridge_identity",
                        "bridge_capabilities",
                        "capture_screenshot",
                        "inspect_addon",
                        "deployment_begin",
                        "deployment_chunk",
                        "deployment_commit",
                        "deployment_receipt",
                    }
                    or document["target_id"] != str(owner.target_id)
                    or document["bridge_id"] != str(owner.bridge_id)
                    or document["signature"]
                    != request_signature(owner.secret, document)
                    or document["nonce"] in owner.nonces
                ):
                    raise ValueError
                owner.nonces.add(str(document["nonce"]))
                if document["operation"] == "bridge_identity":
                    self._cache(document, digest, owner.identity())
                elif document["operation"] == "bridge_capabilities":
                    self._cache(document, digest, {"capabilities": owner.capabilities})
                elif document["operation"] == "capture_screenshot":
                    self._screenshot(document, digest)
                elif document["operation"] == "inspect_addon":
                    self._inspect_addon(document, digest)
                elif document["operation"] == "deployment_begin":
                    self._deployment_begin(document, digest)
                elif document["operation"] == "deployment_chunk":
                    self._deployment_chunk(document, digest)
                elif document["operation"] == "deployment_commit":
                    self._deployment_commit(document, digest)
                elif document["operation"] == "deployment_receipt":
                    self._deployment_receipt(document, digest)
                else:
                    raise ValueError

            def _inspect_addon(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                payload = request["payload"]
                if not isinstance(payload, dict) or payload != {
                    "addon_id": owner.addon_id
                }:
                    raise ValueError
                self._cache(
                    request,
                    request_digest,
                    {
                        "addon_id": owner.addon_id,
                        "addon_version": owner.addon_version,
                        "enabled": owner.addon_enabled,
                        "file_count": owner.addon_file_count,
                        "manifest_digest": owner.addon_manifest_digest,
                        "total_bytes": owner.addon_total_bytes,
                    },
                )

            def _receipt(self, deployment: dict[str, object]) -> dict[str, object]:
                return {
                    "addon_id": deployment["addon_id"],
                    "addon_version": deployment["addon_version"],
                    "bridge_id": str(owner.bridge_id),
                    "classification": deployment["classification"],
                    "completed_at": deployment["completed_at"],
                    "deployment_id": deployment["deployment_id"],
                    "deployment_request_id": deployment["deployment_request_id"],
                    "enabled_state": ("enabled" if owner.addon_enabled else "disabled"),
                    "file_count": deployment["file_count"],
                    "generation": deployment["generation"],
                    "inventory_verified": deployment["inventory_verified"],
                    "manifest_digest": deployment["manifest_digest"],
                    "receipt_id": deployment["receipt_id"],
                    "state": deployment["state"],
                    "total_bytes": deployment["total_bytes"],
                }

            def _deployment_begin(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                payload = request["payload"]
                if not isinstance(payload, dict):
                    raise ValueError
                deployment_id = str(payload["deployment_id"])
                existing = owner.deployments.get(deployment_id)
                identity = hashlib.sha256(canonical_json(payload)).hexdigest()
                if existing is not None:
                    if existing["begin_identity"] != identity:
                        self._send(409, {}, b"")
                    else:
                        self._cache(request, request_digest, self._receipt(existing))
                    return
                if (
                    payload.get("addon_id") != owner.addon_id
                    or payload.get("manifest_version") != 1
                    or not isinstance(payload.get("entries"), list)
                ):
                    raise ValueError
                deployment: dict[str, object] = {
                    **payload,
                    "begin_identity": identity,
                    "classification": "staging",
                    "completed_at": None,
                    "inventory_verified": False,
                    "receipt_id": None,
                    "state": "staging",
                    "chunks": {},
                }
                owner.deployments[deployment_id] = deployment
                self._cache(request, request_digest, self._receipt(deployment))

            def _deployment_chunk(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                payload = request["payload"]
                if not isinstance(payload, dict):
                    raise ValueError
                deployment = owner.deployments[str(payload["deployment_id"])]
                if deployment["state"] != "staging":
                    raise ValueError
                encoded = str(payload["content"])
                content = base64.b64decode(
                    encoded.encode("ascii") + b"=" * (-len(encoded) % 4),
                    altchars=b"-_",
                    validate=True,
                )
                if hashlib.sha256(content).hexdigest() != payload["content_digest"]:
                    raise ValueError
                chunks = deployment["chunks"]
                assert isinstance(chunks, dict)
                key = (str(payload["path"]), int(payload["offset"]))
                prior = chunks.get(key)
                if prior is not None and prior != content:
                    self._send(409, {}, b"")
                    return
                chunks[key] = content
                self._cache(request, request_digest, self._receipt(deployment))

            def _deployment_commit(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                payload = request["payload"]
                if not isinstance(payload, dict):
                    raise ValueError
                deployment = owner.deployments[str(payload["deployment_id"])]
                if payload["manifest_digest"] != deployment["manifest_digest"]:
                    raise ValueError
                if deployment["state"] == "staging":
                    if owner.tamper == "replacement_failure_before":
                        deployment["state"] = "failed"
                        deployment["classification"] = "replacement_failed"
                    else:
                        owner.replacement_count += 1
                        owner.addon_version = str(deployment["addon_version"])
                        owner.addon_manifest_digest = str(deployment["manifest_digest"])
                        owner.addon_file_count = cast(int, deployment["file_count"])
                        owner.addon_total_bytes = cast(int, deployment["total_bytes"])
                        deployment["state"] = "succeeded"
                        deployment["classification"] = "deployed"
                        deployment["inventory_verified"] = True
                    deployment["receipt_id"] = str(uuid4())
                    deployment["completed_at"] = timestamp_text(datetime.now(UTC))
                receipt = self._receipt(deployment)
                self._cache(request, request_digest, receipt)

            def _deployment_receipt(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                payload = request["payload"]
                if not isinstance(payload, dict):
                    raise ValueError
                deployment = owner.deployments.get(str(payload["deployment_id"]))
                if deployment is None:
                    raise ValueError
                self._cache(request, request_digest, self._receipt(deployment))

            def _cache(
                self,
                request: dict[str, object],
                request_digest: bytes,
                payload: dict[str, object],
            ) -> None:
                assert owner.secret is not None or request["operation"] == "pair_bridge"
                if request["operation"] == "pair_bridge":
                    body = canonical_json(payload)
                else:
                    secret = owner.secret
                    assert secret is not None
                    timestamp = timestamp_text(datetime.now(UTC))
                    nonce = b64url(os.urandom(32))
                    bridge_id = str(owner.bridge_id)
                    request_id = str(request["request_id"])
                    status = "ok"
                    if owner.tamper == "wrong_request_id":
                        request_id = str(uuid4())
                    if owner.tamper == "wrong_bridge_id":
                        bridge_id = str(uuid4())
                    if owner.tamper == "altered_status":
                        status = "failed"
                    response: dict[str, object] = {
                        "protocol_version": owner.protocol_version,
                        "operation": request["operation"],
                        "target_id": request["target_id"],
                        "bridge_id": bridge_id,
                        "request_id": request_id,
                        "timestamp": timestamp,
                        "nonce": nonce,
                        "status": status,
                        "payload": payload,
                    }
                    response["signature"] = response_signature(
                        secret,
                        target_id=str(request["target_id"]),
                        bridge_id=str(owner.bridge_id),
                        operation=str(request["operation"]),
                        request_id=str(request["request_id"]),
                        timestamp=timestamp,
                        nonce=nonce,
                        status="ok",
                        payload_digest=hashlib.sha256(
                            canonical_json(payload)
                        ).hexdigest(),
                    )
                    if owner.tamper == "bad_signature":
                        response["signature"] = b64url(b"x" * 32)
                    body = canonical_json(response)
                headers = {"Content-Type": "application/json"}
                item = (request_digest, 200, headers, body)
                owner.requests[str(request["request_id"])] = item
                drop_once = {
                    "deployment_begin": "drop_deployment_begin_once",
                    "deployment_chunk": "drop_deployment_chunk_once",
                    "deployment_commit": "drop_deployment_commit_once",
                }.get(str(request["operation"]))
                if drop_once is not None and owner.tamper == drop_once:
                    owner.tamper = None
                    self.close_connection = True
                    return
                self._send(200, headers, body)

            def _screenshot(
                self, request: dict[str, object], request_digest: bytes
            ) -> None:
                assert owner.secret is not None
                owner.capture_count += 1
                png = fake_png()
                if owner.tamper == "invalid_png":
                    png = b"not-a-png"
                elif owner.tamper == "wrong_dimensions":
                    png = fake_png(width=1, height=1)
                elif owner.tamper == "oversize":
                    png = b"x" * (MAX_PNG_BYTES + 1)
                digest = hashlib.sha256(png).hexdigest()
                timestamp = timestamp_text(datetime.now(UTC))
                nonce = b64url(os.urandom(32))
                bridge_id = str(owner.bridge_id)
                request_id = str(request["request_id"])
                status = "ok"
                if owner.tamper == "wrong_request_id":
                    request_id = str(uuid4())
                if owner.tamper == "wrong_bridge_id":
                    bridge_id = str(uuid4())
                if owner.tamper == "altered_status":
                    status = "failed"
                headers = {
                    "Content-Type": "text/plain"
                    if owner.tamper == "media_type"
                    else "image/png",
                    "X-Kodi-Lab-Protocol": owner.protocol_version,
                    "X-Kodi-Lab-Operation": "capture_screenshot",
                    "X-Kodi-Lab-Target-Id": str(owner.target_id),
                    "X-Kodi-Lab-Bridge-Id": bridge_id,
                    "X-Kodi-Lab-Request-Id": request_id,
                    "X-Kodi-Lab-Timestamp": timestamp,
                    "X-Kodi-Lab-Nonce": nonce,
                    "X-Kodi-Lab-Status": status,
                    "X-Kodi-Lab-Content-SHA256": digest,
                    "X-Kodi-Lab-Width": str(EXPECTED_WIDTH),
                    "X-Kodi-Lab-Height": str(EXPECTED_HEIGHT),
                    "X-Kodi-Lab-Signature": response_signature(
                        owner.secret,
                        target_id=str(owner.target_id),
                        bridge_id=str(owner.bridge_id),
                        operation="capture_screenshot",
                        request_id=str(request["request_id"]),
                        timestamp=timestamp,
                        nonce=nonce,
                        status="ok",
                        payload_digest=digest,
                    ),
                }
                if owner.tamper == "bad_signature":
                    headers["X-Kodi-Lab-Signature"] = b64url(b"x" * 32)
                if owner.tamper == "image":
                    png = png[:-1] + bytes([png[-1] ^ 1])
                item = (request_digest, 200, headers, png)
                owner.requests[str(request["request_id"])] = item
                if owner.tamper == "drop_screenshot_once":
                    owner.tamper = None
                    self.close_connection = True
                    return
                self._send(200, headers, png)

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

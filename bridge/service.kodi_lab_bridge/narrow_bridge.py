"""Standard-library-only closed Kodi Lab bridge protocol implementation."""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import uuid
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbmc
import xbmcaddon
import xbmcvfs

PROTOCOL = "1"
ENDPOINT = "/kodi-lab/bridge/v1"
ADDON_ID = "service.kodi_lab_bridge"
ADDON_VERSION = "1.0.0"
CAPABILITIES = ["screenshot"]
BUILD_FINGERPRINT = hashlib.sha256(
    b"kodi-lab-narrow-bridge|1.0.0|protocol-1|identity,capabilities,screenshot"
).hexdigest()
STATE_PATH = "special://profile/addon_data/service.kodi_lab_bridge/state.json"
MAX_REQUEST = 16384
MAX_PNG = 8 * 1024 * 1024
WIDTH = 1280
HEIGHT = 720
MAX_SKEW = 60
MAX_REPLAY = 256
MAX_RESPONSE_CACHE = 4
REPLAY_TTL = 86400
RESPONSE_CACHE_TTL = 120
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
OPERATIONS = {"bridge_identity", "bridge_capabilities", "capture_screenshot"}
_STATE_LOCK = threading.Lock()
_RESPONSE_LOCK = threading.Lock()
_CAPTURE_LOCK = threading.Lock()
_RESPONSES = OrderedDict()


class AuthenticationError(Exception):
    pass


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_b64(value, size):
    try:
        raw = base64.b64decode(
            value.encode("ascii") + b"=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except Exception:
        raise ValueError("invalid base64url") from None
    if len(raw) != size or _b64(raw) != value:
        raise ValueError("invalid base64url size")
    return raw


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("UUID is not canonical")
    return value


def _timestamp():
    return (
        datetime.now(timezone.utc)  # noqa: UP017 - Kodi 19 supports Python 3.8.
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _fresh(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return False
    # datetime.UTC is unavailable in Kodi 19's Python 3.8 runtime.
    return abs((datetime.now(timezone.utc) - parsed).total_seconds()) <= MAX_SKEW  # noqa: UP017


def _pairing_key(token):
    return hmac.new(token, b"kodi-lab-bridge-pairing-key-v1", hashlib.sha256).digest()


def _derive_secret(token, target_id, bridge_id, server_nonce, bridge_nonce):
    context = _canonical(
        {
            "bridge_id": bridge_id,
            "bridge_nonce": bridge_nonce,
            "label": "kodi-lab-bridge-request-secret-v1",
            "server_nonce": server_nonce,
            "target_id": target_id,
        }
    )
    return hmac.new(_pairing_key(token), context, hashlib.sha256).digest()


def _request_key(secret):
    return hmac.new(
        secret, b"kodi-lab-bridge-request-signing-v1", hashlib.sha256
    ).digest()


def _response_key(secret):
    return hmac.new(
        secret, b"kodi-lab-bridge-response-signing-v1", hashlib.sha256
    ).digest()


def _request_signature(secret, document):
    fields = {
        "bridge_id": document["bridge_id"],
        "nonce": document["nonce"],
        "operation": document["operation"],
        "payload_digest": hashlib.sha256(_canonical(document["payload"])).hexdigest(),
        "protocol_version": document["protocol_version"],
        "request_id": document["request_id"],
        "target_id": document["target_id"],
        "timestamp": document["timestamp"],
    }
    return _b64(
        hmac.new(_request_key(secret), _canonical(fields), hashlib.sha256).digest()
    )


def _response_signature(
    secret,
    target_id,
    bridge_id,
    operation,
    request_id,
    timestamp,
    nonce,
    status,
    digest,
):
    fields = {
        "bridge_id": bridge_id,
        "nonce": nonce,
        "operation": operation,
        "payload_digest": digest,
        "protocol_version": PROTOCOL,
        "request_id": request_id,
        "status": status,
        "target_id": target_id,
        "timestamp": timestamp,
    }
    return _b64(
        hmac.new(_response_key(secret), _canonical(fields), hashlib.sha256).digest()
    )


def _state_default():
    return {
        "format": 1,
        "bridge_id": str(uuid.uuid4()),
        "target_id": None,
        "secret": None,
        "replay": [],
    }


def _load_state():
    path = xbmcvfs.translatePath(STATE_PATH)
    if not xbmcvfs.exists(path):
        state = _state_default()
        _save_state(state)
        return state
    handle = xbmcvfs.File(path)
    try:
        raw = handle.read()
    finally:
        handle.close()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        state = json.loads(raw)
        _uuid(state["bridge_id"])
        if state["target_id"] is not None:
            _uuid(state["target_id"])
        if set(state) != {"format", "bridge_id", "target_id", "secret", "replay"}:
            raise ValueError
        if state["format"] != 1 or not isinstance(state["replay"], list):
            raise ValueError
        return state
    except Exception:
        raise RuntimeError(
            "Kodi Lab bridge state is invalid; refusing replacement"
        ) from None


def _save_state(state):
    path = xbmcvfs.translatePath(STATE_PATH)
    parent = os.path.dirname(path)
    if not xbmcvfs.exists(parent):
        xbmcvfs.mkdirs(parent)
    temporary = path + ".tmp"
    handle = xbmcvfs.File(temporary, "wb")
    try:
        handle.write(_canonical(state))
    finally:
        handle.close()
    try:
        os.replace(temporary, path)
    except OSError as error:
        raise RuntimeError("Kodi Lab bridge state publication failed") from error


def _kodi_version():
    raw = xbmc.getInfoLabel("System.BuildVersion") or ""
    match = re.match(r"^([0-9]+)\.([0-9]+)", raw)
    if not match:
        return "unknown"
    return f"{match.group(1)}.{match.group(2)}"


def _identity(state):
    return {
        "bridge_id": state["bridge_id"],
        "protocol_version": PROTOCOL,
        "addon_id": ADDON_ID,
        "addon_version": ADDON_VERSION,
        "kodi_version": _kodi_version(),
        "capabilities": CAPABILITIES,
        "build_fingerprint": BUILD_FINGERPRINT,
    }


def _png_chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def _capture_png():
    capture = xbmc.RenderCapture()
    capture.capture(WIDTH, HEIGHT)
    pixels = bytes(capture.getImage(5000))
    if len(pixels) != WIDTH * HEIGHT * 4:
        raise RuntimeError("capture geometry invalid")
    raw = bytearray()
    row_size = WIDTH * 4
    for row_number in range(HEIGHT):
        raw.append(0)
        row = pixels[row_number * row_size : (row_number + 1) * row_size]
        for offset in range(0, len(row), 4):
            blue, green, red, _alpha = row[offset : offset + 4]
            raw.extend((red, green, blue))
    png = bytearray(PNG_SIGNATURE)
    png.extend(
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    )
    png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
    png.extend(_png_chunk(b"IEND", b""))
    result = bytes(png)
    if len(result) > MAX_PNG:
        raise RuntimeError("capture too large")
    return result


def _cache_get(request_id, request_hash):
    with _RESPONSE_LOCK:
        item = _RESPONSES.get(request_id)
        if item is None:
            return None
        if time.time() - item[0] > RESPONSE_CACHE_TTL:
            del _RESPONSES[request_id]
            return None
        if item[1] != request_hash:
            raise ValueError("request conflict")
        _RESPONSES.move_to_end(request_id)
        return item[2:]


def _cache_put(request_id, request_hash, status, headers, body):
    with _RESPONSE_LOCK:
        _RESPONSES[request_id] = (time.time(), request_hash, status, headers, body)
        _RESPONSES.move_to_end(request_id)
        while len(_RESPONSES) > MAX_RESPONSE_CACHE:
            _RESPONSES.popitem(last=False)


def _claim_replay(state, request_id, nonce):
    now = int(time.time())
    replay = [item for item in state["replay"] if now - item[2] <= REPLAY_TTL]
    if any(item[0] == request_id or item[1] == nonce for item in replay):
        raise ValueError("replay")
    replay.append([request_id, nonce, now])
    state["replay"] = replay[-MAX_REPLAY:]
    _save_state(state)


class Handler(BaseHTTPRequestHandler):
    server_version = "KodiLabNarrowBridge/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format_value, *args):
        xbmc.log("[service.kodi_lab_bridge] " + (format_value % args), xbmc.LOGDEBUG)

    def do_POST(self):
        if self.path != ENDPOINT:
            self._empty(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._empty(400)
            return
        if (
            not 1 <= length <= MAX_REQUEST
            or self.headers.get("Content-Type", "").split(";", 1)[0]
            != "application/json"
        ):
            self._empty(400)
            return
        raw = self.rfile.read(length)
        request_hash = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw.decode("utf-8"))
            request_id = document["request_id"]
            _uuid(request_id)
            cached = _cache_get(request_id, request_hash)
            if cached is not None:
                self._send(*cached)
                return
            if document.get("operation") == "pair_bridge":
                self._pair(document, request_hash)
            else:
                self._authenticated(document, request_hash)
        except AuthenticationError:
            self._empty(401)
        except ValueError:
            self._empty(409)
        except Exception:
            self._empty(401)

    def _pair(self, document, request_hash):
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
        _uuid(document["target_id"])
        _uuid(document["request_id"])
        _decode_b64(document["server_nonce"], 32)
        if document["protocol_version"] != PROTOCOL or not _fresh(
            document["timestamp"]
        ):
            raise ValueError
        addon = xbmcaddon.Addon(ADDON_ID)
        token_text = str(addon.getSetting("pairing_token") or "").strip()
        try:
            token = _decode_b64(token_text, 32)
        except ValueError as error:
            raise AuthenticationError from error
        proof_fields = dict(document)
        provided_proof = proof_fields.pop("pairing_proof")
        expected_proof = _b64(
            hmac.new(
                _pairing_key(token), _canonical(proof_fields), hashlib.sha256
            ).digest()
        )
        if not isinstance(provided_proof, str) or not hmac.compare_digest(
            expected_proof, provided_proof
        ):
            raise AuthenticationError
        bridge_nonce = _b64(os.urandom(32))
        with _STATE_LOCK:
            state = _load_state()
            response = {
                "protocol_version": PROTOCOL,
                "operation": "pair_bridge",
                "target_id": document["target_id"],
                "request_id": document["request_id"],
                "timestamp": _timestamp(),
                "bridge_nonce": bridge_nonce,
                "identity": _identity(state),
            }
            response["pairing_proof"] = _b64(
                hmac.new(
                    _pairing_key(token), _canonical(response), hashlib.sha256
                ).digest()
            )
            secret = _derive_secret(
                token,
                document["target_id"],
                state["bridge_id"],
                document["server_nonce"],
                bridge_nonce,
            )
            state["target_id"] = document["target_id"]
            state["secret"] = _b64(secret)
            state["replay"] = []
            _save_state(state)
            addon.setSetting("pairing_token", "")
        body = _canonical(response)
        headers = {"Content-Type": "application/json"}
        _cache_put(document["request_id"], request_hash, 200, headers, body)
        self._send(200, headers, body)

    def _authenticated(self, document, request_hash):
        if set(document) != {
            "protocol_version",
            "operation",
            "target_id",
            "bridge_id",
            "request_id",
            "timestamp",
            "nonce",
            "payload",
            "signature",
        }:
            raise ValueError
        if document["operation"] not in OPERATIONS or document["payload"] != {}:
            raise ValueError
        _uuid(document["target_id"])
        _uuid(document["bridge_id"])
        _uuid(document["request_id"])
        _decode_b64(document["nonce"], 32)
        if document["protocol_version"] != PROTOCOL or not _fresh(
            document["timestamp"]
        ):
            raise ValueError
        with _STATE_LOCK:
            state = _load_state()
            if (
                state["target_id"] != document["target_id"]
                or state["bridge_id"] != document["bridge_id"]
                or state["secret"] is None
            ):
                raise AuthenticationError
            secret = _decode_b64(state["secret"], 32)
            expected = _request_signature(secret, document)
            if not hmac.compare_digest(expected, document["signature"]):
                raise AuthenticationError
            _claim_replay(state, document["request_id"], document["nonce"])
        if document["operation"] == "bridge_identity":
            payload = _identity(state)
            self._json_response(document, request_hash, secret, payload)
        elif document["operation"] == "bridge_capabilities":
            self._json_response(
                document, request_hash, secret, {"capabilities": CAPABILITIES}
            )
        else:
            self._screenshot_response(document, request_hash, secret)

    def _json_response(self, request, request_hash, secret, payload):
        timestamp = _timestamp()
        nonce = _b64(os.urandom(32))
        response = {
            "protocol_version": PROTOCOL,
            "operation": request["operation"],
            "target_id": request["target_id"],
            "bridge_id": request["bridge_id"],
            "request_id": request["request_id"],
            "timestamp": timestamp,
            "nonce": nonce,
            "status": "ok",
            "payload": payload,
        }
        response["signature"] = _response_signature(
            secret,
            request["target_id"],
            request["bridge_id"],
            request["operation"],
            request["request_id"],
            timestamp,
            nonce,
            "ok",
            hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        body = _canonical(response)
        headers = {"Content-Type": "application/json"}
        _cache_put(request["request_id"], request_hash, 200, headers, body)
        self._send(200, headers, body)

    def _screenshot_response(self, request, request_hash, secret):
        if not _CAPTURE_LOCK.acquire(False):
            raise ValueError("capture already in progress")
        try:
            png = _capture_png()
        finally:
            _CAPTURE_LOCK.release()
        digest = hashlib.sha256(png).hexdigest()
        timestamp = _timestamp()
        nonce = _b64(os.urandom(32))
        headers = {
            "Content-Type": "image/png",
            "X-Kodi-Lab-Protocol": PROTOCOL,
            "X-Kodi-Lab-Operation": "capture_screenshot",
            "X-Kodi-Lab-Target-Id": request["target_id"],
            "X-Kodi-Lab-Bridge-Id": request["bridge_id"],
            "X-Kodi-Lab-Request-Id": request["request_id"],
            "X-Kodi-Lab-Timestamp": timestamp,
            "X-Kodi-Lab-Nonce": nonce,
            "X-Kodi-Lab-Status": "ok",
            "X-Kodi-Lab-Content-SHA256": digest,
            "X-Kodi-Lab-Width": str(WIDTH),
            "X-Kodi-Lab-Height": str(HEIGHT),
            "X-Kodi-Lab-Signature": _response_signature(
                secret,
                request["target_id"],
                request["bridge_id"],
                "capture_screenshot",
                request["request_id"],
                timestamp,
                nonce,
                "ok",
                digest,
            ),
        }
        _cache_put(request["request_id"], request_hash, 200, headers, png)
        self._send(200, headers, png)

    def _send(self, status, headers, body):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4


def run():
    monitor = xbmc.Monitor()
    addon = xbmcaddon.Addon(ADDON_ID)
    if not addon.getSettingBool("enabled"):
        xbmc.log("[service.kodi_lab_bridge] disabled", xbmc.LOGINFO)
        return
    port = addon.getSettingInt("bridge_port")
    if not 1 <= port <= 65535:
        xbmc.log("[service.kodi_lab_bridge] invalid port", xbmc.LOGERROR)
        return
    server = Server(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="kodi-lab-bridge")
    thread.daemon = True
    thread.start()
    try:
        while not monitor.waitForAbort(1.0):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

"""Standard-library-only closed Kodi Lab bridge protocol implementation."""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbmc
import xbmcaddon
import xbmcvfs

PROTOCOL = "1"
ENDPOINT = "/kodi-lab/bridge/v1"
ADDON_ID = "service.kodi_lab_bridge"
ADDON_VERSION = "1.1.7"
CAPABILITIES = ["screenshot", "workspace_deployment"]
BUILD_FINGERPRINT = hashlib.sha256(
    b"kodi-lab-narrow-bridge|1.1.7|protocol-1|identity,capabilities,gui-screenshot,workspace-deployment"
).hexdigest()
STATE_PATH = "special://profile/addon_data/service.kodi_lab_bridge/state.json"
ADDON_ROOT = "special://home/addons"
SCREENSHOT_ROOT = "special://temp/kodi-lab-bridge"
MAX_REQUEST = 17 * 1024 * 1024
MAX_PNG = 8 * 1024 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_CHUNK = 256 * 1024
MAX_FILES = 25000
MAX_ENTRIES = 50000
MAX_TOTAL = 1024 * 1024 * 1024
MAX_BYTECODE_CACHE_FILES = 4096
MAX_BYTECODE_CACHE_BYTES = 64 * 1024 * 1024
MAX_DEPLOYMENTS = 64
DEPLOYMENT_TTL = 86400
MAX_SKEW = 60
MAX_REPLAY = 256
MAX_RESPONSE_CACHE = 4
REPLAY_TTL = 86400
RESPONSE_CACHE_TTL = 120
SCREENSHOT_STALE_TTL = 300
MAX_SCREENSHOT_SCAN = 64
MAX_SCREENSHOT_CLEANUP = 16
MAX_SCREENSHOT_WIDTH = 4096
MAX_SCREENSHOT_HEIGHT = 2160
MAX_SCREENSHOT_DECODED = MAX_SCREENSHOT_WIDTH * MAX_SCREENSHOT_HEIGHT * 4
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
OPERATIONS = {
    "bridge_identity",
    "bridge_capabilities",
    "capture_screenshot",
    "inspect_addon",
    "deployment_begin",
    "deployment_chunk",
    "deployment_commit",
    "deployment_receipt",
}
REPLAY_EVICTABLE_OPERATIONS = OPERATIONS - {"capture_screenshot"}
_STATE_LOCK = threading.Lock()
_RESPONSE_LOCK = threading.Lock()
_CAPTURE_LOCK = threading.Lock()
_DEPLOYMENT_LOCK = threading.Lock()
_RESPONSES = OrderedDict()


class AuthenticationError(Exception):
    pass


class ScreenshotError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class OperationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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
        "format": 2,
        "bridge_id": str(uuid.uuid4()),
        "target_id": None,
        "secret": None,
        "replay": [],
        "deployments": {},
        "rollback": None,
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
        if set(state) == {"format", "bridge_id", "target_id", "secret", "replay"}:
            if state["format"] != 1:
                raise ValueError
            state["format"] = 2
            state["deployments"] = {}
            state["rollback"] = None
            _save_state(state)
        _uuid(state["bridge_id"])
        if state["target_id"] is not None:
            _uuid(state["target_id"])
        if set(state) != {
            "format",
            "bridge_id",
            "target_id",
            "secret",
            "replay",
            "deployments",
            "rollback",
        }:
            raise ValueError
        if (
            state["format"] != 2
            or not isinstance(state["replay"], list)
            or not isinstance(state["deployments"], dict)
            or len(state["deployments"]) > MAX_DEPLOYMENTS
        ):
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


def _unsafe_file(metadata):
    if stat.S_ISLNK(metadata.st_mode):
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and getattr(metadata, "st_file_attributes", 0) & reparse)


def _screenshot_root():
    temporary = os.path.abspath(xbmcvfs.translatePath("special://temp"))
    root = os.path.abspath(xbmcvfs.translatePath(SCREENSHOT_ROOT))
    try:
        if os.path.commonpath((temporary, root)) != temporary or root == temporary:
            raise ScreenshotError("screenshot_capture_failed")
    except ValueError as error:
        raise ScreenshotError("screenshot_capture_failed") from error
    try:
        if not os.path.exists(root):
            os.mkdir(root, 0o700)
        metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode) or _unsafe_file(metadata):
            raise ScreenshotError("screenshot_capture_failed")
        real_temporary = os.path.realpath(temporary)
        if (
            os.path.commonpath((real_temporary, os.path.realpath(root)))
            != real_temporary
        ):
            raise ScreenshotError("screenshot_capture_failed")
        with suppress(OSError):
            os.chmod(root, 0o700)
    except ScreenshotError:
        raise
    except OSError as error:
        raise ScreenshotError("screenshot_capture_failed") from error
    return root


def _cleanup_stale_screenshots(root, now=None):
    current = time.time() if now is None else now
    scanned = 0
    removed = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if scanned >= MAX_SCREENSHOT_SCAN or removed >= MAX_SCREENSHOT_CLEANUP:
                    break
                scanned += 1
                if re.fullmatch(r"capture-[0-9a-f]{32}\.png", entry.name) is None:
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                    if current - metadata.st_mtime < SCREENSHOT_STALE_TTL:
                        continue
                    os.unlink(entry.path)
                    removed += 1
                except OSError:
                    xbmc.log(
                        "[service.kodi_lab_bridge] stale screenshot cleanup failed",
                        xbmc.LOGWARNING,
                    )
    except OSError:
        xbmc.log(
            "[service.kodi_lab_bridge] stale screenshot scan failed", xbmc.LOGWARNING
        )
    return scanned, removed


def _paeth(left, above, upper_left):
    prediction = left + above - upper_left
    left_distance = abs(prediction - left)
    above_distance = abs(prediction - above)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _normalize_png(payload):
    if not len(PNG_SIGNATURE) < len(payload) <= MAX_PNG or not payload.startswith(
        PNG_SIGNATURE
    ):
        raise ScreenshotError("screenshot_invalid")
    offset = len(PNG_SIGNATURE)
    header = None
    compressed = bytearray()
    ended = False
    seen_idat = False
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise ScreenshotError("screenshot_invalid")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ScreenshotError("screenshot_invalid")
        data = payload[offset + 8 : offset + 8 + length]
        checksum = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if binascii.crc32(kind + data) & 0xFFFFFFFF != checksum:
            raise ScreenshotError("screenshot_invalid")
        if kind == b"IHDR" and header is None and offset == len(PNG_SIGNATURE):
            header = data
        elif kind == b"IDAT" and header is not None and not ended:
            seen_idat = True
            compressed.extend(data)
            if len(compressed) > MAX_PNG:
                raise ScreenshotError("screenshot_too_large")
        elif kind == b"IEND" and data == b"" and seen_idat:
            ended = True
            offset = end
            break
        elif kind[:1].isupper():
            raise ScreenshotError("screenshot_invalid")
        offset = end
    if not ended or offset != len(payload) or header is None or len(header) != 13:
        raise ScreenshotError("screenshot_invalid")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if (
        not 1 <= width <= MAX_SCREENSHOT_WIDTH
        or not 1 <= height <= MAX_SCREENSHOT_HEIGHT
        or depth != 8
        or color not in (2, 6)
        or (compression, filtering, interlace) != (0, 0, 0)
    ):
        raise ScreenshotError("screenshot_invalid")
    channels = 3 if color == 2 else 4
    stride = width * channels
    expected = height * (stride + 1)
    if expected > MAX_SCREENSHOT_DECODED:
        raise ScreenshotError("screenshot_too_large")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), expected + 1)
        if len(decoded) > expected or decompressor.unconsumed_tail:
            raise ScreenshotError("screenshot_too_large")
        decoded += decompressor.flush(expected + 1 - len(decoded))
    except zlib.error as error:
        raise ScreenshotError("screenshot_invalid") from error
    if (
        len(decoded) != expected
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ScreenshotError("screenshot_invalid")
    raw = bytearray()
    previous = bytearray(stride)
    source_offset = 0
    for _row_number in range(height):
        filter_kind = decoded[source_offset]
        source_offset += 1
        filtered = decoded[source_offset : source_offset + stride]
        source_offset += stride
        row = bytearray(stride)
        for index, value in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_kind == 0:
                decoded_value = value
            elif filter_kind == 1:
                decoded_value = value + left
            elif filter_kind == 2:
                decoded_value = value + above
            elif filter_kind == 3:
                decoded_value = value + ((left + above) // 2)
            elif filter_kind == 4:
                decoded_value = value + _paeth(left, above, upper_left)
            else:
                raise ScreenshotError("screenshot_invalid")
            row[index] = decoded_value & 0xFF
        raw.append(0)
        if channels == 3:
            raw.extend(row)
        else:
            for index in range(0, len(row), 4):
                raw.extend(row[index : index + 3])
        previous = row
    png = bytearray(PNG_SIGNATURE)
    png.extend(
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    )
    png.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
    png.extend(_png_chunk(b"IEND", b""))
    result = bytes(png)
    if len(result) > MAX_PNG:
        raise ScreenshotError("screenshot_too_large")
    return result


def _read_screenshot(path):
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _unsafe_file(metadata)
            or metadata.st_nlink != 1
        ):
            raise ScreenshotError("screenshot_invalid")
        if metadata.st_size > MAX_PNG:
            raise ScreenshotError("screenshot_too_large")
        if metadata.st_size <= len(PNG_SIGNATURE):
            raise ScreenshotError("screenshot_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_nlink != 1
            ):
                raise ScreenshotError("screenshot_invalid")
            chunks = []
            remaining = MAX_PNG + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except ScreenshotError:
        raise
    except OSError as error:
        raise ScreenshotError("screenshot_capture_failed") from error
    payload = b"".join(chunks)
    if len(payload) > MAX_PNG:
        raise ScreenshotError("screenshot_too_large")
    if len(payload) != metadata.st_size:
        raise ScreenshotError("screenshot_invalid")
    return _normalize_png(payload)


def _capture_png():
    root = _screenshot_root()
    _cleanup_stale_screenshots(root)
    filename = "capture-" + uuid.uuid4().hex + ".png"
    native_path = os.path.join(root, filename)
    special_path = SCREENSHOT_ROOT + "/" + filename
    if os.path.lexists(native_path):
        raise ScreenshotError("screenshot_capture_failed")
    cleanup_failed = False
    try:
        try:
            xbmc.executebuiltin("TakeScreenshot(" + special_path + ",sync)")
        except Exception as error:
            raise ScreenshotError("screenshot_capture_failed") from error
        deadline = time.monotonic() + 2.0
        last_error = ScreenshotError("screenshot_capture_failed")
        while time.monotonic() < deadline:
            if os.path.lexists(native_path):
                try:
                    return _read_screenshot(native_path)
                except ScreenshotError as error:
                    if error.code == "screenshot_too_large":
                        raise
                    last_error = error
            time.sleep(0.02)
        raise last_error
    finally:
        try:
            if os.path.lexists(native_path):
                os.unlink(native_path)
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise ScreenshotError("screenshot_cleanup_failed")


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


def _claim_replay(state, request_id, nonce, operation):
    now = int(time.time())
    replay = [item for item in state["replay"] if now - item[2] <= REPLAY_TTL]
    if any(item[0] == request_id or item[1] == nonce for item in replay):
        raise ValueError("replay")
    if len(replay) >= MAX_REPLAY:
        evictable = next(
            (
                index
                for index, item in enumerate(replay)
                if len(item) == 4 and item[3] in REPLAY_EVICTABLE_OPERATIONS
            ),
            None,
        )
        if evictable is None:
            raise ValueError("replay capacity")
        del replay[evictable]
    replay.append([request_id, nonce, now, operation])
    state["replay"] = replay
    _save_state(state)


def _cleanup_staging(state):
    """Bound abandoned bytes while retaining every receipt identity."""

    now = int(time.time())
    changed = False
    for deployment_id, summary in state["deployments"].items():
        if summary.get("state") not in {"staging", "ready"}:
            continue
        if now - int(summary.get("created", now)) <= DEPLOYMENT_TTL:
            continue
        operation = _load_operation(deployment_id)
        stage = _stage_path(deployment_id)
        if os.path.isdir(stage) and not os.path.islink(stage):
            shutil.rmtree(stage)
        operation["state"] = "failed"
        operation["classification"] = "staging_expired"
        operation["receipt_id"] = str(uuid.uuid4())
        operation["completed_at"] = _timestamp()
        _save_operation(operation)
        summary["state"] = "failed"
        changed = True
    if changed:
        _save_state(state)


def _addon_id(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value != value.lower()
        or ".." in value
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value) is None
    ):
        raise ValueError("invalid add-on ID")
    return value


def _relative_path(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ValueError("invalid relative path")
    parts = value.split("/")
    if len(parts) > 32:
        raise ValueError("invalid relative path")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or "\\" in part
            or len(part.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or part in {".git", ".hg", ".svn", "__pycache__"}
        ):
            raise ValueError("invalid relative path")
    if value.endswith(".pyc") or "/__pycache__/" in f"/{value}/":
        raise ValueError("bytecode cache is not deployable")
    return value


def _manifest_bytes(entries):
    document = {
        "entries": entries,
        "exclusion_policy_version": 1,
        "manifest_version": 1,
    }
    result = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(result) > MAX_MANIFEST:
        raise ValueError("manifest too large")
    return result


def _validate_manifest(payload):
    entries = payload.get("entries")
    if (
        payload.get("manifest_version") != 1
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_ENTRIES
    ):
        raise ValueError("invalid manifest")
    prior = None
    files = 0
    total = 0
    normalized = []
    seen = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("invalid manifest entry")
        path = _relative_path(raw.get("path"))
        if path in seen or (prior is not None and path.encode("utf-8") <= prior):
            raise ValueError("manifest order is invalid")
        prior = path.encode("utf-8")
        seen.add(path)
        kind = raw.get("type")
        mode = raw.get("mode")
        size = raw.get("size")
        if kind == "directory":
            if (
                set(raw) != {"mode", "path", "size", "type"}
                or mode != 0o755
                or size != 0
            ):
                raise ValueError("invalid directory entry")
            normalized.append({"mode": mode, "path": path, "size": 0, "type": kind})
        elif kind == "file":
            digest = raw.get("sha256")
            if (
                set(raw) != {"mode", "path", "sha256", "size", "type"}
                or mode not in {0o644, 0o755}
                or type(size) is not int
                or not 0 <= size <= MAX_FILE
                or not isinstance(digest, str)
                or len(digest) != 64
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValueError("invalid file entry")
            files += 1
            total += size
            if files > MAX_FILES or total > MAX_TOTAL:
                raise ValueError("manifest bounds exceeded")
            normalized.append(
                {
                    "mode": mode,
                    "path": path,
                    "sha256": digest,
                    "size": size,
                    "type": kind,
                }
            )
        else:
            raise ValueError("unsupported manifest type")
    encoded = _manifest_bytes(normalized)
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if (
        payload.get("manifest_digest") != digest
        or payload.get("file_count") != files
        or payload.get("total_bytes") != total
        or not 1 <= files <= MAX_FILES
        or not 1 <= total <= MAX_TOTAL
    ):
        raise ValueError("manifest summary mismatch")
    return normalized, digest, files, total


def _tree_manifest(root):
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError("add-on tree unavailable")
    entries = []
    files = 0
    total = 0
    cache_files = 0
    cache_bytes = 0
    root_real = os.path.realpath(root)
    for current, directories, names in os.walk(root, topdown=True, followlinks=False):
        if os.path.realpath(current) == root_real:
            prefix = ""
        else:
            prefix = os.path.relpath(current, root).replace(os.sep, "/")
        directories.sort(key=lambda item: item.encode("utf-8"))
        names.sort(key=lambda item: item.encode("utf-8"))
        for name in list(directories):
            if name == "__pycache__":
                absolute = os.path.join(current, name)
                ignored_files, ignored_bytes = _validate_bytecode_cache(absolute)
                cache_files += ignored_files
                cache_bytes += ignored_bytes
                if (
                    cache_files > MAX_BYTECODE_CACHE_FILES
                    or cache_bytes > MAX_BYTECODE_CACHE_BYTES
                    or files + cache_files > MAX_FILES
                    or total + cache_bytes > MAX_TOTAL
                ):
                    raise ValueError("bytecode cache bounds exceeded")
                directories.remove(name)
                continue
            path = name if not prefix else prefix + "/" + name
            _relative_path(path)
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                raise ValueError("linked directory")
            entries.append(
                {"mode": 0o755, "path": path, "size": 0, "type": "directory"}
            )
            if len(entries) > MAX_ENTRIES:
                raise ValueError("tree bounds exceeded")
        for name in names:
            path = name if not prefix else prefix + "/" + name
            _relative_path(path)
            absolute = os.path.join(current, name)
            metadata = os.lstat(absolute)
            if (
                not os.path.isfile(absolute)
                or os.path.islink(absolute)
                or metadata.st_nlink != 1
            ):
                raise ValueError("unsafe file")
            if metadata.st_size > MAX_FILE:
                raise ValueError("file too large")
            digest = hashlib.sha256()
            with open(absolute, "rb") as stream:
                while True:
                    chunk = stream.read(MAX_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
            files += 1
            total += metadata.st_size
            if (
                files + cache_files > MAX_FILES
                or len(entries) >= MAX_ENTRIES
                or total + cache_bytes > MAX_TOTAL
            ):
                raise ValueError("tree bounds exceeded")
            mode = 0o755 if metadata.st_mode & 0o111 else 0o644
            entries.append(
                {
                    "mode": mode,
                    "path": path,
                    "sha256": digest.hexdigest(),
                    "size": metadata.st_size,
                    "type": "file",
                }
            )
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    encoded = _manifest_bytes(entries)
    return entries, "sha256:" + hashlib.sha256(encoded).hexdigest(), files, total


def _validate_bytecode_cache(root):
    try:
        metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("unsafe bytecode cache")
        files = 0
        total = 0
        with os.scandir(root) as stream:
            for entry in stream:
                name = entry.name
                item = entry.stat(follow_symlinks=False)
                if (
                    not name
                    or not name.endswith(".pyc")
                    or len(name.encode("utf-8")) > 255
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in name
                    )
                    or not stat.S_ISREG(item.st_mode)
                    or stat.S_ISLNK(item.st_mode)
                    or item.st_nlink != 1
                    or item.st_size > MAX_FILE
                ):
                    raise ValueError("unsafe bytecode cache")
                files += 1
                total += item.st_size
                if files > MAX_BYTECODE_CACHE_FILES or total > MAX_BYTECODE_CACHE_BYTES:
                    raise ValueError("bytecode cache bounds exceeded")
        return files, total
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("unsafe bytecode cache") from error


def _addon_descriptor(root):
    descriptor = os.path.join(root, "addon.xml")
    if not os.path.isfile(descriptor) or os.path.getsize(descriptor) > 1024 * 1024:
        raise ValueError("descriptor unavailable")
    parsed = ET.parse(descriptor).getroot()
    addon_id = _addon_id(parsed.attrib.get("id"))
    version = parsed.attrib.get("version")
    if not isinstance(version, str) or not 1 <= len(version) <= 128:
        raise ValueError("descriptor version invalid")
    video_plugins = [
        child
        for child in parsed.findall("extension")
        if child.attrib.get("point") == "xbmc.python.pluginsource"
        and "video" in {item.text for item in child.findall("provides") if item.text}
    ]
    return addon_id, version, len(video_plugins) == 1


def _jsonrpc(method, params):
    request = {"jsonrpc": "2.0", "id": "kodi-lab-bridge", "method": method}
    if params is not None:
        request["params"] = params
    result = json.loads(xbmc.executeJSONRPC(json.dumps(request, separators=(",", ":"))))
    if (
        not isinstance(result, dict)
        or result.get("id") != "kodi-lab-bridge"
        or "result" not in result
    ):
        raise OperationError("addon_inspection_failed")
    return result["result"]


def _addon_enabled(addon_id):
    result = _jsonrpc(
        "Addons.GetAddonDetails",
        {"addonid": addon_id, "properties": ["enabled", "version"]},
    )
    addon = result.get("addon") if isinstance(result, dict) else None
    if not isinstance(addon, dict) or addon.get("addonid") != addon_id:
        raise OperationError("addon_inspection_failed")
    return bool(addon.get("enabled")), addon


def _set_enabled(addon_id, enabled):
    if (
        _jsonrpc("Addons.SetAddonEnabled", {"addonid": addon_id, "enabled": enabled})
        != "OK"
    ):
        raise ValueError("add-on enablement unavailable")


def _stage_path(deployment_id):
    return os.path.join(
        xbmcvfs.translatePath(ADDON_ROOT), ".kodi-lab-stage-" + deployment_id
    )


def _backup_path(deployment_id):
    return os.path.join(
        xbmcvfs.translatePath(ADDON_ROOT), ".kodi-lab-backup-" + deployment_id
    )


def _operation_path(deployment_id):
    root = os.path.dirname(xbmcvfs.translatePath(STATE_PATH))
    return os.path.join(root, "deployment-" + deployment_id + ".json")


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some Kodi platforms do not expose directory fsync.
        return


def _save_operation(operation):
    path = _operation_path(operation["deployment_id"])
    temporary = path + ".tmp"
    with open(temporary, "wb") as stream:
        stream.write(_canonical(operation))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_operation(deployment_id):
    with open(_operation_path(deployment_id), "rb") as stream:
        return json.loads(stream.read().decode("utf-8"))


def _receipt(operation, bridge_id):
    state = operation["state"]
    projected = "ambiguous" if state == "replacing" else state
    return {
        "addon_id": operation["addon_id"],
        "addon_version": operation["addon_version"],
        "bridge_id": bridge_id,
        "classification": operation["classification"],
        "completed_at": operation.get("completed_at"),
        "deployment_id": operation["deployment_id"],
        "deployment_request_id": operation["deployment_request_id"],
        "enabled_state": operation.get("enabled_state", "unknown"),
        "file_count": operation["file_count"],
        "generation": operation["generation"],
        "inventory_verified": bool(operation.get("inventory_verified", False)),
        "manifest_digest": operation["manifest_digest"],
        "receipt_id": operation.get("receipt_id"),
        "state": projected,
        "total_bytes": operation["total_bytes"],
    }


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
        if document["operation"] not in OPERATIONS or not isinstance(
            document["payload"], dict
        ):
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
            _claim_replay(
                state,
                document["request_id"],
                document["nonce"],
                document["operation"],
            )
        if document["operation"] == "bridge_identity":
            if document["payload"] != {}:
                raise ValueError
            payload = _identity(state)
            self._json_response(document, request_hash, secret, payload)
        elif document["operation"] == "bridge_capabilities":
            if document["payload"] != {}:
                raise ValueError
            self._json_response(
                document, request_hash, secret, {"capabilities": CAPABILITIES}
            )
        elif document["operation"] == "capture_screenshot":
            if document["payload"] != {}:
                raise ValueError
            self._screenshot_response(document, request_hash, secret)
        else:
            self._deployment_response(document, request_hash, secret, state)

    def _deployment_response(self, request, request_hash, secret, state):
        operation = request["operation"]
        payload = request["payload"]
        with _DEPLOYMENT_LOCK, _STATE_LOCK:
            state = _load_state()
            if operation == "inspect_addon":
                addon_id = _addon_id(payload.get("addon_id"))
                if set(payload) != {"addon_id"}:
                    raise ValueError
                root = os.path.join(xbmcvfs.translatePath(ADDON_ROOT), addon_id)
                try:
                    _entries, digest, files, total = _tree_manifest(root)
                    observed_id, version, _eligible = _addon_descriptor(root)
                    if observed_id != addon_id:
                        raise ValueError
                    enabled, _details = _addon_enabled(addon_id)
                except (OSError, ET.ParseError, OperationError, ValueError):
                    self._json_error_response(
                        request,
                        request_hash,
                        secret,
                        "addon_inspection_failed",
                    )
                    return
                result = {
                    "addon_id": addon_id,
                    "addon_version": version,
                    "enabled": enabled,
                    "file_count": files,
                    "manifest_digest": digest,
                    "total_bytes": total,
                }
            elif operation == "deployment_begin":
                result = self._deployment_begin(payload, state)
            elif operation == "deployment_chunk":
                result = self._deployment_chunk(payload, state)
            elif operation == "deployment_commit":
                result = self._deployment_commit(payload, state)
            elif operation == "deployment_receipt":
                if set(payload) != {"deployment_id"}:
                    raise ValueError
                deployment_id = _uuid(payload.get("deployment_id"))
                result = _receipt(_load_operation(deployment_id), state["bridge_id"])
            else:
                raise ValueError
        self._json_response(request, request_hash, secret, result)

    def _deployment_begin(self, payload, state):
        _cleanup_staging(state)
        required = {
            "addon_id",
            "addon_version",
            "deployment_id",
            "deployment_request_id",
            "entries",
            "file_count",
            "generation",
            "manifest_digest",
            "manifest_version",
            "total_bytes",
        }
        if set(payload) != required:
            raise ValueError
        deployment_id = _uuid(payload["deployment_id"])
        request_id = _uuid(payload["deployment_request_id"])
        addon_id = _addon_id(payload["addon_id"])
        addon_version = payload["addon_version"]
        generation = payload["generation"]
        if (
            not isinstance(addon_version, str)
            or not 1 <= len(addon_version) <= 128
            or type(generation) is not int
            or generation < 0
        ):
            raise ValueError
        entries, digest, files, total = _validate_manifest(payload)
        fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
        try:
            existing = _load_operation(deployment_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ValueError("deployment request conflict")
            return _receipt(existing, state["bridge_id"])
        unresolved = [
            item
            for item in state["deployments"].values()
            if item.get("state") in {"staging", "ready", "replacing"}
        ]
        if unresolved or len(state["deployments"]) >= MAX_DEPLOYMENTS:
            raise ValueError("deployment capacity unavailable")
        stage = _stage_path(deployment_id)
        if os.path.lexists(stage):
            raise ValueError("staging identity collision")
        os.mkdir(stage, 0o700)
        try:
            for entry in entries:
                target = os.path.join(stage, *entry["path"].split("/"))
                if entry["type"] == "directory":
                    os.mkdir(target, 0o755)
                else:
                    parent = os.path.dirname(target)
                    if not os.path.isdir(parent):
                        raise ValueError("manifest parent unavailable")
                    descriptor = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        entry["mode"],
                    )
                    os.close(descriptor)
                    os.chmod(target, entry["mode"])
            operation = {
                "addon_id": addon_id,
                "addon_version": addon_version,
                "classification": "transfer_pending",
                "completed_at": None,
                "created_at_epoch": int(time.time()),
                "deployment_id": deployment_id,
                "deployment_request_id": request_id,
                "enabled_state": "unknown",
                "entries": entries,
                "file_count": files,
                "fingerprint": fingerprint,
                "generation": generation,
                "inventory_verified": False,
                "manifest_digest": digest,
                "receipt_id": None,
                "state": "staging",
                "total_bytes": total,
            }
            _save_operation(operation)
            state["deployments"][deployment_id] = {
                "created": operation["created_at_epoch"],
                "state": "staging",
            }
            _save_state(state)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return _receipt(operation, state["bridge_id"])

    def _deployment_chunk(self, payload, state):
        if set(payload) != {
            "content",
            "content_digest",
            "deployment_id",
            "file_digest",
            "offset",
            "path",
        }:
            raise ValueError
        deployment_id = _uuid(payload["deployment_id"])
        operation = _load_operation(deployment_id)
        if operation["state"] not in {"staging", "ready"}:
            return _receipt(operation, state["bridge_id"])
        path = _relative_path(payload["path"])
        matching = [
            item
            for item in operation["entries"]
            if item["path"] == path and item["type"] == "file"
        ]
        if len(matching) != 1 or matching[0]["sha256"] != payload["file_digest"]:
            raise ValueError("chunk is not referenced by manifest")
        raw_text = payload["content"]
        if not isinstance(raw_text, str):
            raise ValueError
        try:
            content = base64.b64decode(
                raw_text.encode("ascii") + b"=" * (-len(raw_text) % 4),
                altchars=b"-_",
                validate=True,
            )
        except Exception:
            raise ValueError("chunk encoding invalid") from None
        if (
            not 1 <= len(content) <= MAX_CHUNK
            or _b64(content) != raw_text
            or hashlib.sha256(content).hexdigest() != payload["content_digest"]
            or type(payload["offset"]) is not int
            or payload["offset"] < 0
        ):
            raise ValueError("chunk invalid")
        target = os.path.join(_stage_path(deployment_id), *path.split("/"))
        current = os.path.getsize(target)
        offset = payload["offset"]
        if offset < current:
            with open(target, "rb") as stream:
                stream.seek(offset)
                if stream.read(len(content)) != content:
                    raise ValueError("chunk conflict")
        elif offset == current and current + len(content) <= matching[0]["size"]:
            with open(target, "ab") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            raise ValueError("chunk offset invalid")
        complete = True
        for entry in operation["entries"]:
            if entry["type"] == "file":
                candidate = os.path.join(
                    _stage_path(deployment_id), *entry["path"].split("/")
                )
                if os.path.getsize(candidate) != entry["size"]:
                    complete = False
                    break
        if complete:
            operation["state"] = "ready"
            operation["classification"] = "transfer_complete"
            state["deployments"][deployment_id]["state"] = "ready"
            _save_operation(operation)
            _save_state(state)
        return _receipt(operation, state["bridge_id"])

    def _deployment_commit(self, payload, state):
        if set(payload) != {"deployment_id", "manifest_digest"}:
            raise ValueError
        deployment_id = _uuid(payload["deployment_id"])
        operation = _load_operation(deployment_id)
        if payload["manifest_digest"] != operation["manifest_digest"]:
            raise ValueError("manifest mismatch")
        if operation["state"] in {"succeeded", "failed", "replacing"}:
            return _receipt(operation, state["bridge_id"])
        if operation["state"] != "ready":
            raise ValueError("transfer incomplete")
        stage = _stage_path(deployment_id)
        scanned, digest, files, total = _tree_manifest(stage)
        observed_id, observed_version, eligible = _addon_descriptor(stage)
        if (
            scanned != operation["entries"]
            or digest != operation["manifest_digest"]
            or files != operation["file_count"]
            or total != operation["total_bytes"]
            or observed_id != operation["addon_id"]
            or observed_version != operation["addon_version"]
            or not eligible
        ):
            operation["state"] = "failed"
            operation["classification"] = "deployment_verification_failed"
            operation["receipt_id"] = str(uuid.uuid4())
            operation["completed_at"] = _timestamp()
            _save_operation(operation)
            state["deployments"][deployment_id]["state"] = "failed"
            _save_state(state)
            return _receipt(operation, state["bridge_id"])
        destination = os.path.join(
            xbmcvfs.translatePath(ADDON_ROOT), operation["addon_id"]
        )
        backup = _backup_path(deployment_id)
        enabled, _before = _addon_enabled(operation["addon_id"])
        operation["enabled_state"] = "enabled" if enabled else "disabled"
        operation["state"] = "replacing"
        operation["classification"] = "replacement_started"
        _save_operation(operation)
        state["deployments"][deployment_id]["state"] = "replacing"
        _save_state(state)
        moved_old = False
        moved_new = False
        try:
            if enabled:
                _set_enabled(operation["addon_id"], False)
            previous = state.get("rollback")
            if previous is not None:
                prior_path = _backup_path(previous)
                if os.path.isdir(prior_path) and not os.path.islink(prior_path):
                    shutil.rmtree(prior_path)
            if not os.path.isdir(destination) or os.path.islink(destination):
                raise ValueError("destination identity unavailable")
            os.replace(destination, backup)
            moved_old = True
            os.replace(stage, destination)
            moved_new = True
            _fsync_directory(os.path.dirname(destination))
            if enabled:
                _set_enabled(operation["addon_id"], True)
            after_entries, after_digest, after_files, after_total = _tree_manifest(
                destination
            )
            after_id, after_version, _after_eligible = _addon_descriptor(destination)
            after_enabled, details = _addon_enabled(operation["addon_id"])
            if (
                after_entries != operation["entries"]
                or after_digest != operation["manifest_digest"]
                or after_files != operation["file_count"]
                or after_total != operation["total_bytes"]
                or after_id != operation["addon_id"]
                or after_version != operation["addon_version"]
                or str(details.get("version")) != operation["addon_version"]
                or after_enabled != enabled
            ):
                raise ValueError("final inventory verification failed")
            operation["state"] = "succeeded"
            operation["classification"] = "deployed_and_verified"
            operation["inventory_verified"] = True
            operation["receipt_id"] = str(uuid.uuid4())
            operation["completed_at"] = _timestamp()
            state["rollback"] = deployment_id
        except Exception:
            rollback_ok = False
            restored_before_mutation = False
            try:
                if moved_new and os.path.isdir(destination):
                    failed = stage + ".failed"
                    if os.path.lexists(failed):
                        shutil.rmtree(failed, ignore_errors=True)
                    os.replace(destination, failed)
                if moved_old:
                    os.replace(backup, destination)
                    _fsync_directory(os.path.dirname(destination))
                if enabled:
                    _set_enabled(operation["addon_id"], True)
                restored_before_mutation = not moved_old and not moved_new
                rollback_ok = moved_old or restored_before_mutation
            except Exception:
                rollback_ok = False
            operation["state"] = "failed" if rollback_ok else "replacing"
            operation["classification"] = (
                "replacement_failed_before_mutation"
                if restored_before_mutation
                else (
                    "replacement_failed_rolled_back"
                    if rollback_ok
                    else "rollback_failed_reconciliation_required"
                )
            )
            operation["receipt_id"] = str(uuid.uuid4()) if rollback_ok else None
            operation["completed_at"] = _timestamp() if rollback_ok else None
        _save_operation(operation)
        state["deployments"][deployment_id]["state"] = operation["state"]
        _save_state(state)
        return _receipt(operation, state["bridge_id"])

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

    def _json_error_response(self, request, request_hash, secret, code):
        if code != "addon_inspection_failed":
            code = "bridge_operation_failed"
        payload = {}
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
            "status": code,
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
            code,
            hashlib.sha256(_canonical(payload)).hexdigest(),
        )
        body = _canonical(response)
        headers = {"Content-Type": "application/json"}
        _cache_put(request["request_id"], request_hash, 422, headers, body)
        self._send(422, headers, body)

    def _screenshot_response(self, request, request_hash, secret):
        if not _CAPTURE_LOCK.acquire(False):
            raise ValueError("capture already in progress")
        try:
            try:
                png = _capture_png()
            except ScreenshotError as error:
                self._screenshot_error_response(
                    request, request_hash, secret, error.code
                )
                return
            except Exception:
                xbmc.log(
                    "[service.kodi_lab_bridge] screenshot capture failed",
                    xbmc.LOGERROR,
                )
                self._screenshot_error_response(
                    request, request_hash, secret, "screenshot_capture_failed"
                )
                return
        finally:
            _CAPTURE_LOCK.release()
        digest = hashlib.sha256(png).hexdigest()
        width, height = struct.unpack(">II", png[16:24])
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
            "X-Kodi-Lab-Width": str(width),
            "X-Kodi-Lab-Height": str(height),
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

    def _screenshot_error_response(self, request, request_hash, secret, code):
        if code not in {
            "screenshot_capture_failed",
            "screenshot_invalid",
            "screenshot_too_large",
            "screenshot_cleanup_failed",
        }:
            code = "screenshot_capture_failed"
        body = b""
        digest = hashlib.sha256(body).hexdigest()
        timestamp = _timestamp()
        nonce = _b64(os.urandom(32))
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Kodi-Lab-Protocol": PROTOCOL,
            "X-Kodi-Lab-Operation": "capture_screenshot",
            "X-Kodi-Lab-Target-Id": request["target_id"],
            "X-Kodi-Lab-Bridge-Id": request["bridge_id"],
            "X-Kodi-Lab-Request-Id": request["request_id"],
            "X-Kodi-Lab-Timestamp": timestamp,
            "X-Kodi-Lab-Nonce": nonce,
            "X-Kodi-Lab-Status": code,
            "X-Kodi-Lab-Content-SHA256": digest,
            "X-Kodi-Lab-Signature": _response_signature(
                secret,
                request["target_id"],
                request["bridge_id"],
                "capture_screenshot",
                request["request_id"],
                timestamp,
                nonce,
                code,
                digest,
            ),
        }
        _cache_put(request["request_id"], request_hash, 422, headers, body)
        self._send(422, headers, body)

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

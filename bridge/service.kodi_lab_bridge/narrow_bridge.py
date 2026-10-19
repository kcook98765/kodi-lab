"""Standard-library-only closed Kodi Lab bridge protocol implementation."""

import base64
import binascii
import errno
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
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xbmc
import xbmcaddon
import xbmcvfs
from onboarding import BOOTSTRAP_VERSION, OnboardingError, OnboardingStore, _file_lock

PROTOCOL = "1"
ENDPOINT = "/kodi-lab/bridge/v1"
BOOTSTRAP_ENDPOINT = "/kodi-lab/bridge/bootstrap/v1"
ADDON_ID = "service.kodi_lab_bridge"
ADDON_VERSION = "1.1.25"
CAPABILITIES = ["screenshot", "workspace_deployment", "ui_observation", "log_read"]
BUILD_FINGERPRINT = hashlib.sha256(
    b"kodi-lab-narrow-bridge|1.1.25|protocol-1|identity,capabilities,gui-screenshot,workspace-deployment,otp-bootstrap-v1,local-pairing-reset,protected-ui-observation,portable-direct-install,bounded-log-read,managed-manifest-launch"
).hexdigest()
STATE_PATH = "special://profile/addon_data/service.kodi_lab_bridge/state.json"
ONBOARDING_PATH = "special://profile/addon_data/service.kodi_lab_bridge/onboarding.json"
ADDON_ROOT = "special://home/addons"
SCREENSHOT_ROOT = "special://temp/kodi-lab-bridge"
MAX_REQUEST = 17 * 1024 * 1024
MAX_BOOTSTRAP_REQUEST = 2048
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
REPLAY_EVICTABLE_OPERATIONS = OPERATIONS - {"capture_screenshot"}
_STATE_LOCK = threading.Lock()
_RESPONSE_LOCK = threading.Lock()
_CAPTURE_LOCK = threading.Lock()
_LOG_LOCK = threading.Lock()
_DEPLOYMENT_LOCK = threading.Lock()
_RESPONSES = OrderedDict()


@contextmanager
def _state_guard():
    """Serialize state changes across Kodi service and local UI interpreters."""
    with _STATE_LOCK, _file_lock(xbmcvfs.translatePath(STATE_PATH)):
        yield


class AuthenticationError(Exception):
    pass


class LogReadError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _log_snapshot(metadata):
    return hashlib.sha256(
        _canonical(
            [
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ]
        )
    ).hexdigest()


def _read_log(payload):
    """Read only two fixed local logs, with bounded memory and pinned ancestors."""
    keys = {"log", "mode", "max_lines", "max_bytes", "before", "snapshot_id"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise LogReadError("log_request_invalid")
    log, mode = payload["log"], payload["mode"]
    lines, limit = payload["max_lines"], payload["max_bytes"]
    before, expected = payload["before"], payload["snapshot_id"]
    if (
        log not in ("current", "previous")
        or mode not in ("stats", "tail")
        or type(lines) is not int
        or not 1 <= lines <= 500
        or type(limit) is not int
        or not 1 <= limit <= 8192
        or (
            expected is not None
            and (
                not isinstance(expected, str)
                or re.fullmatch("[0-9a-f]{64}", expected) is None
            )
        )
        or (
            before is not None
            and (
                type(before) is not int
                or not 0 <= before <= 2**63 - 1
                or expected is None
                or mode != "tail"
            )
        )
    ):
        raise LogReadError("log_request_invalid")
    if not _LOG_LOCK.acquire(blocking=False):
        raise LogReadError("log_read_busy")
    try:
        name = "kodi.log" if log == "current" else "kodi.old.log"
        root = xbmcvfs.translatePath("special://logpath")
        if not os.path.isabs(root):
            raise LogReadError("log_unavailable")
        path = os.path.join(root, name)
        with _deployment_directory(root, traverse_only=True) as parent:
            descriptor = (
                _windows_open_nofollow(path)
                if os.name == "nt"
                else os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
            )
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    _unsafe_file(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or _file_identity(metadata) is None
                    or not 0 <= metadata.st_size <= 2**63 - 1
                ):
                    raise LogReadError("log_unavailable")
                if os.name == "nt":
                    info = _windows_handle_info(stream.fileno())
                    if info is None or info[0] & 0x400 or info[1] != 1:
                        raise LogReadError("log_unavailable")
                snapshot = _log_snapshot(metadata)
                if expected is not None and expected != snapshot:
                    raise LogReadError("log_changed")
                if before is not None and before > metadata.st_size:
                    raise LogReadError("log_request_invalid")
                start = end = 0
                raw = b""
                partial = False
                if mode == "tail":
                    end = metadata.st_size if before is None else before
                    start = max(0, end - limit)
                    stream.seek(max(0, start - 1))
                    preceding = stream.read(1) if start else b"\n"
                    raw = stream.read(end - start)
                    pieces = raw.splitlines(keepends=True)
                    if len(pieces) > lines:
                        raw = b"".join(pieces[-lines:])
                        start = end - len(raw)
                    elif start and preceding != b"\n":
                        # Prefer whole lines; retain a bounded fragment of a huge line.
                        first = raw.find(b"\n")
                        if 0 <= first < len(raw) - 1:
                            raw = raw[first + 1 :]
                            start = end - len(raw)
                        else:
                            partial = bool(raw)
                current = (
                    os.lstat(path)
                    if os.name == "nt"
                    else os.stat(name, dir_fd=parent, follow_symlinks=False)
                )
                if (
                    _unsafe_file(current)
                    or _log_snapshot(current) != snapshot
                    or _log_snapshot(os.fstat(stream.fileno())) != snapshot
                    or len(raw) != end - start
                ):
                    raise LogReadError("log_changed")
                content = raw.decode("utf-8", errors="replace")
                return {
                    "log": log,
                    "mode": mode,
                    "location": "special://logpath/" + name,
                    "snapshot_id": snapshot,
                    "size_bytes": metadata.st_size,
                    "modified_at_ns": metadata.st_mtime_ns,
                    "observed_at": _timestamp(),
                    "start_byte": start,
                    "end_byte": end,
                    "bytes_read": len(raw),
                    "lines_returned": len(content.splitlines()),
                    "truncated": mode == "tail"
                    and (start > 0 or end < metadata.st_size),
                    "partial_first_line": partial,
                    "content": content,
                }
    except FileNotFoundError:
        raise LogReadError("log_missing") from None
    except LogReadError:
        raise
    except (OSError, ValueError, AttributeError):
        raise LogReadError("log_unavailable") from None
    finally:
        _LOG_LOCK.release()


class ScreenshotError(Exception):
    def __init__(self, code, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(code)


_SCREENSHOT_DIAGNOSTICS = frozenset(
    {
        "windows_full_identity",
        "windows_private_root_fallback",
        "windows_privacy_failed",
        "windows_metadata_api_failed",
    }
)


def _screenshot_diagnostic(code):
    if code not in _SCREENSHOT_DIAGNOSTICS:
        return
    xbmc.log("[service.kodi_lab_bridge] screenshot_assurance=" + code, xbmc.LOGINFO)


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


def _paired(state):
    target_id = state["target_id"]
    secret = state["secret"]
    if target_id is None and secret is None:
        return False
    if target_id is None or secret is None:
        raise RuntimeError("Kodi Lab bridge pairing state is incomplete")
    try:
        _decode_b64(secret, 32)
    except ValueError:
        raise RuntimeError("Kodi Lab bridge pairing state is invalid") from None
    return True


def _onboarding_store():
    return OnboardingStore(
        xbmcvfs.translatePath(ONBOARDING_PATH),
        event=lambda name: xbmc.log(
            "[service.kodi_lab_bridge] onboarding=" + name, xbmc.LOGINFO
        ),
    )


def _local_pairing_status():
    with _state_guard():
        return _paired(_load_state())


def _reset_pairing():
    """Clear only server pairing material; the bridge identity and Kodi stay put."""
    with _state_guard():
        state = _load_state()
        if not _paired(state):
            return False
        # Clear provisional/OTP material first. An interrupted reset then still
        # appears paired until the old credential is durably removed below.
        _onboarding_store().reset_pairing()
        state["target_id"] = None
        state["secret"] = None
        state["replay"] = []
        _save_state(state)
        return True


def _require_current_pairing(state, request, secret):
    if (
        not _paired(state)
        or state["target_id"] != request["target_id"]
        or state["bridge_id"] != request["bridge_id"]
        or not hmac.compare_digest(_decode_b64(state["secret"], 32), secret)
    ):
        raise AuthenticationError


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


_WINDOWS_SYSTEM_SID = "S-1-5-18"
_WINDOWS_PRIVATE_INHERITANCE = (("OI", 0x01), ("CI", 0x02))
_WINDOWS_PRIVATE_ACCESS = ("FA", 0x1F01FF)


def _windows_private_root_aces(account):
    """Canonical effective root ACEs, inherited by files and directories."""
    flags = sum(bit for _name, bit in _WINDOWS_PRIVATE_INHERITANCE)
    access = _WINDOWS_PRIVATE_ACCESS[1]
    return (
        (0, flags, access, account),
        (0, flags, access, _WINDOWS_SYSTEM_SID),
    )


def _private_windows_acl(owner, account, protected, entries):
    """Accept only the constructor's protected, inheritable directory DACL."""
    expected = _windows_private_root_aces(account)
    return (
        owner == account
        and protected
        and len(entries) == len(expected)
        and set(entries) == set(expected)
    )


def _windows_private_root_sddl(account):
    flags = "".join(name for name, _bit in _WINDOWS_PRIVATE_INHERITANCE)
    access = _WINDOWS_PRIVATE_ACCESS[0]
    return "D:P" + "".join(
        f"(A;{flags};{access};;;{'SY' if sid == _WINDOWS_SYSTEM_SID else sid})"
        for _type, _flags, _access, sid in _windows_private_root_aces(account)
    )


@contextmanager
def _windows_security_info(get_security, free, handle):
    """Own GetSecurityInfo's output even if the API reports failure."""
    import ctypes

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    observed = ctypes.c_void_p()
    try:
        if get_security(
            handle,
            1,
            5,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(observed),
        ):
            raise OSError
        yield owner, dacl, observed
    finally:
        if observed.value:
            free(observed)


def _windows_private_root(path, create=True, hold=False):
    """Create the bridge child with a protected DACL, then verify by handle.

    An existing child is verified only. No parent or existing ACL is changed.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        security = ctypes.WinDLL("advapi32", use_last_error=True)

        def api(library, name, arguments, result):
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
            return function

        close = api(kernel, "CloseHandle", (wintypes.HANDLE,), wintypes.BOOL)
        free = api(kernel, "LocalFree", (ctypes.c_void_p,), ctypes.c_void_p)
        sid_text = api(
            security,
            "ConvertSidToStringSidW",
            (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)),
            wintypes.BOOL,
        )

        def sid_string(pointer):
            string = ctypes.c_void_p()
            if not sid_text(pointer, ctypes.byref(string)):
                raise OSError
            try:
                return ctypes.wstring_at(string.value)
            finally:
                free(string)

        token = wintypes.HANDLE()
        open_token = api(
            security,
            "OpenProcessToken",
            (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)),
            wintypes.BOOL,
        )
        get_process = api(kernel, "GetCurrentProcess", (), wintypes.HANDLE)
        if not open_token(get_process(), 0x0008, ctypes.byref(token)):
            raise OSError
        try:
            get_token = api(
                security,
                "GetTokenInformation",
                (
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ),
                wintypes.BOOL,
            )
            needed = wintypes.DWORD()
            get_token(token, 1, None, 0, ctypes.byref(needed))
            if not 16 <= needed.value <= 4096:
                raise OSError
            buffer = ctypes.create_string_buffer(needed.value)
            if not get_token(token, 1, buffer, needed, ctypes.byref(needed)):
                raise OSError
            account = sid_string(
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            )
        finally:
            close(token)

        descriptor = ctypes.c_void_p()
        convert = api(
            security,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
            ),
            wintypes.BOOL,
        )
        # P blocks parent inheritance. OI/CI give only this account and SYSTEM
        # inherited access to screenshot files created by Kodi in the child.
        sddl = _windows_private_root_sddl(account)
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise OSError

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.DWORD),
                ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL),
            ]

        try:
            if create:
                attributes = SecurityAttributes(
                    ctypes.sizeof(SecurityAttributes), descriptor, False
                )
                make_directory = api(
                    kernel,
                    "CreateDirectoryW",
                    (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)),
                    wintypes.BOOL,
                )
                if (
                    not make_directory(path, ctypes.byref(attributes))
                    and ctypes.get_last_error() != 183
                ):
                    raise OSError
        finally:
            free(descriptor)

        create_file = api(
            kernel,
            "CreateFileW",
            (
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ),
            wintypes.HANDLE,
        )
        # Share read/write but deny delete while a capture is using this root.
        handle = create_file(path, 0x20080, 3, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError
        release = None
        try:

            class FileInfo(ctypes.Structure):
                _fields_ = [
                    ("attributes", wintypes.DWORD),
                    ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME),
                    ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD),
                    ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD),
                    ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD),
                    ("index_low", wintypes.DWORD),
                ]

            get_file_info = api(
                kernel,
                "GetFileInformationByHandle",
                (wintypes.HANDLE, ctypes.POINTER(FileInfo)),
                wintypes.BOOL,
            )
            file_info = FileInfo()
            if not get_file_info(handle, ctypes.byref(file_info)):
                raise OSError
            if file_info.attributes & 0x400 or not file_info.attributes & 0x10:
                _screenshot_diagnostic("windows_privacy_failed")
                raise ScreenshotError("screenshot_capture_failed", "root_not_private")

            get_security = api(
                security,
                "GetSecurityInfo",
                (
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                ),
                wintypes.DWORD,
            )
            with _windows_security_info(get_security, free, handle) as (
                owner,
                dacl,
                observed,
            ):
                control = wintypes.WORD()
                revision = wintypes.DWORD()
                get_control = api(
                    security,
                    "GetSecurityDescriptorControl",
                    (
                        ctypes.c_void_p,
                        ctypes.POINTER(wintypes.WORD),
                        ctypes.POINTER(wintypes.DWORD),
                    ),
                    wintypes.BOOL,
                )
                if not get_control(
                    observed, ctypes.byref(control), ctypes.byref(revision)
                ):
                    raise OSError
                if not owner.value or not dacl.value:
                    _screenshot_diagnostic("windows_privacy_failed")
                    raise ScreenshotError(
                        "screenshot_capture_failed", "root_not_private"
                    )

                class AclSize(ctypes.Structure):
                    _fields_ = [
                        ("count", wintypes.DWORD),
                        ("used", wintypes.DWORD),
                        ("free", wintypes.DWORD),
                    ]

                get_acl = api(
                    security,
                    "GetAclInformation",
                    (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int),
                    wintypes.BOOL,
                )
                acl_size = AclSize()
                if not get_acl(
                    dacl, ctypes.byref(acl_size), ctypes.sizeof(acl_size), 2
                ):
                    raise OSError
                if acl_size.count > 16:
                    _screenshot_diagnostic("windows_privacy_failed")
                    raise ScreenshotError(
                        "screenshot_capture_failed", "root_not_private"
                    )
                get_ace = api(
                    security,
                    "GetAce",
                    (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)),
                    wintypes.BOOL,
                )
                entries = []
                for index in range(acl_size.count):
                    ace = ctypes.c_void_p()
                    if not get_ace(dacl, index, ctypes.byref(ace)):
                        raise OSError
                    header = ctypes.string_at(ace, 8)
                    if header[0] != 0 or int.from_bytes(header[2:4], "little") < 16:
                        _screenshot_diagnostic("windows_privacy_failed")
                        raise ScreenshotError(
                            "screenshot_capture_failed", "root_not_private"
                        )
                    entries.append(
                        (
                            header[0],
                            header[1],
                            int.from_bytes(header[4:8], "little"),
                            sid_string(ace.value + 8),
                        )
                    )
                if not _private_windows_acl(
                    sid_string(owner), account, bool(control.value & 0x1000), entries
                ):
                    _screenshot_diagnostic("windows_privacy_failed")
                    raise ScreenshotError(
                        "screenshot_capture_failed", "root_not_private"
                    )
            if hold:

                def release():
                    close(handle)
        finally:
            if release is None:
                close(handle)
        return release
    except ScreenshotError:
        raise
    except Exception:
        _screenshot_diagnostic("windows_privacy_failed")
        raise ScreenshotError(
            "screenshot_capture_failed", "privacy_query_failed"
        ) from None


def _screenshot_root():
    temporary = os.path.abspath(xbmcvfs.translatePath("special://temp"))
    root = os.path.abspath(xbmcvfs.translatePath(SCREENSHOT_ROOT))
    if os.name == "posix" and xbmc.getCondVisibility("System.Platform.Android"):
        # Android's emulated special://temp cannot enforce mode 0700. Kodi
        # supplies its actual internal cache location, including adopted storage.
        binary_addons = os.environ.get("KODI_BINADDON_PATH", "")
        if (
            not os.path.isabs(binary_addons)
            or os.path.basename(binary_addons) != "lib"
            or os.path.basename(os.path.dirname(binary_addons)) != "cache"
        ):
            raise ScreenshotError("screenshot_capture_failed", "root_not_private")
        temporary = os.path.dirname(binary_addons)
        root = os.path.join(temporary, "kodi-lab-bridge")
    try:
        if os.path.commonpath((temporary, root)) != temporary or root == temporary:
            raise ScreenshotError("screenshot_capture_failed")
    except ValueError as error:
        raise ScreenshotError("screenshot_capture_failed") from error
    try:
        if os.name == "nt":
            _windows_private_root(root)
        elif not os.path.exists(root):
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
        if os.name == "posix":
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if not nofollow:
                raise ScreenshotError("screenshot_capture_failed", "root_not_private")
            descriptor = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
            )
            try:
                os.fchmod(descriptor, 0o700)
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or (metadata.st_dev, metadata.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or opened.st_uid != os.getuid()
                    or opened.st_mode & 0o077
                ):
                    raise ScreenshotError(
                        "screenshot_capture_failed", "root_not_private"
                    )
            finally:
                os.close(descriptor)
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
    if len(payload) > MAX_PNG:
        raise ScreenshotError("screenshot_too_large", "size_too_large")
    if len(payload) <= len(PNG_SIGNATURE) or not payload.startswith(PNG_SIGNATURE):
        raise ScreenshotError("screenshot_invalid", "png_signature")
    offset = len(PNG_SIGNATURE)
    header = None
    compressed = bytearray()
    ended = False
    seen_idat = False
    while offset < len(payload):
        if offset + 12 > len(payload):
            raise ScreenshotError("screenshot_invalid", "png_chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ScreenshotError("screenshot_invalid", "png_chunk")
        data = payload[offset + 8 : offset + 8 + length]
        checksum = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if binascii.crc32(kind + data) & 0xFFFFFFFF != checksum:
            raise ScreenshotError("screenshot_invalid", "png_crc")
        if kind == b"IHDR" and header is None and offset == len(PNG_SIGNATURE):
            header = data
        elif kind == b"IDAT" and header is not None and not ended:
            seen_idat = True
            compressed.extend(data)
            if len(compressed) > MAX_PNG:
                raise ScreenshotError("screenshot_too_large", "size_too_large")
        elif kind == b"IEND" and data == b"" and seen_idat:
            ended = True
            offset = end
            break
        elif kind[:1].isupper():
            raise ScreenshotError("screenshot_invalid", "png_chunk")
        offset = end
    if not ended or offset != len(payload) or header is None or len(header) != 13:
        raise ScreenshotError("screenshot_invalid", "png_chunk")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if (
        not 1 <= width <= MAX_SCREENSHOT_WIDTH
        or not 1 <= height <= MAX_SCREENSHOT_HEIGHT
    ):
        raise ScreenshotError("screenshot_invalid", "png_ihdr")
    if (
        depth != 8
        or color not in (2, 6)
        or (compression, filtering, interlace) != (0, 0, 0)
    ):
        raise ScreenshotError("screenshot_invalid", "png_encoding")
    channels = 3 if color == 2 else 4
    stride = width * channels
    expected = height * (stride + 1)
    if expected > MAX_SCREENSHOT_DECODED:
        raise ScreenshotError("screenshot_too_large", "size_too_large")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(bytes(compressed), expected + 1)
        if len(decoded) > expected or decompressor.unconsumed_tail:
            raise ScreenshotError("screenshot_too_large", "size_too_large")
        decoded += decompressor.flush(expected + 1 - len(decoded))
    except zlib.error as error:
        raise ScreenshotError("screenshot_invalid", "png_zlib") from error
    if (
        len(decoded) != expected
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ScreenshotError("screenshot_invalid", "png_zlib")
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
                raise ScreenshotError("screenshot_invalid", "png_filter")
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
        raise ScreenshotError("screenshot_too_large", "size_too_large")
    return result


def _windows_open_nofollow(path):
    """Open the final Windows path component itself, never its reparse target."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    handle = create(path, 0x80000000, 0x7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(wintypes.HANDLE(handle))
        raise


def _windows_handle_info(descriptor, native=False):
    """Optional handle attributes/link count; some Windows volumes omit them."""
    import ctypes
    from ctypes import wintypes

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInfo))
    get_info.restype = wintypes.BOOL
    info = FileInfo()
    if not native:
        import msvcrt

        descriptor = msvcrt.get_osfhandle(descriptor)
    if not get_info(wintypes.HANDLE(descriptor), ctypes.byref(info)):
        return None
    return info.attributes, info.links


def _file_identity(metadata):
    device = getattr(metadata, "st_dev", 0)
    inode = getattr(metadata, "st_ino", 0)
    return (device, inode) if device and inode else None


class ScreenshotFileAssurance:
    """Platform-specific opening and identity checks for one transient candidate."""

    def __init__(self, platform=None):
        self.platform = os.name if platform is None else platform
        self.mode = None

    def open(self, path, before):
        if self.platform == "nt":
            return _windows_open_nofollow(path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise ScreenshotError("screenshot_invalid", "identity_unavailable")
        return os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow)

    def check(self, before, opened, descriptor):
        if not stat.S_ISREG(opened.st_mode):
            raise ScreenshotError("screenshot_invalid", "candidate_not_regular")
        if self.platform == "nt":
            try:
                info = _windows_handle_info(descriptor)
            except Exception:
                info = None
            if info is None:
                _screenshot_diagnostic("windows_metadata_api_failed")
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if _unsafe_file(opened) or (info and info[0] & reparse):
                raise ScreenshotError("screenshot_invalid", "candidate_reparse")
            if any(
                count > 1
                for count in (before.st_nlink, opened.st_nlink, info[1] if info else 0)
            ):
                raise ScreenshotError("screenshot_invalid", "candidate_hardlink")
            # Windows can report zero for path or handle identity. The no-follow
            # handle plus private random candidate remains the fallback assurance.
            prior, current = _file_identity(before), _file_identity(opened)
            if prior and current and prior != current:
                raise ScreenshotError("screenshot_invalid", "identity_changed")
            self.before_identity = prior
            self.opened_identity = current
            self.known_single_link = (
                before.st_nlink == 1
                or opened.st_nlink == 1
                or bool(info and info[1] == 1)
            )
        else:
            if before.st_nlink != 1 or opened.st_nlink != 1:
                raise ScreenshotError("screenshot_invalid", "candidate_hardlink")
            prior, current = _file_identity(before), _file_identity(opened)
            if not prior or not current:
                raise ScreenshotError("screenshot_invalid", "identity_unavailable")
            if prior != current:
                raise ScreenshotError("screenshot_invalid", "identity_changed")

    def check_after(self, path, opened, after):
        if after.st_size != opened.st_size:
            raise ScreenshotError("screenshot_invalid", "size_changed")
        if self.platform != "nt" and after.st_nlink != 1:
            raise ScreenshotError("screenshot_invalid", "candidate_hardlink")
        if self.platform != "nt" and _file_identity(after) != _file_identity(opened):
            raise ScreenshotError("screenshot_invalid", "identity_changed")
        try:
            current_path = os.lstat(path)
        except FileNotFoundError:
            raise ScreenshotError("screenshot_invalid", "identity_changed") from None
        if _unsafe_file(current_path) or not stat.S_ISREG(current_path.st_mode):
            raise ScreenshotError("screenshot_invalid", "candidate_reparse")
        if self.platform != "nt" and current_path.st_nlink != 1:
            raise ScreenshotError("screenshot_invalid", "candidate_hardlink")
        if self.platform == "nt" and (after.st_nlink > 1 or current_path.st_nlink > 1):
            raise ScreenshotError("screenshot_invalid", "candidate_hardlink")
        current, held = _file_identity(current_path), _file_identity(opened)
        if current and held and current != held:
            raise ScreenshotError("screenshot_invalid", "identity_changed")
        if self.platform == "nt":
            final = _file_identity(after)
            if final and held and final != held:
                raise ScreenshotError("screenshot_invalid", "identity_changed")
            self.mode = (
                "windows_full_identity"
                if (
                    self.before_identity
                    and self.before_identity == self.opened_identity == final == current
                    and self.known_single_link
                )
                else "windows_private_root_fallback"
            )


def _read_screenshot(path, root=None, platform=None):
    root = _screenshot_root() if root is None else root
    candidate = os.path.abspath(path)
    if (
        os.path.normcase(os.path.dirname(candidate)) != os.path.normcase(root)
        or re.fullmatch(r"capture-[0-9a-f]{32}\.png", os.path.basename(candidate))
        is None
    ):
        raise ScreenshotError("screenshot_invalid", "candidate_path")
    assurance = ScreenshotFileAssurance(platform)
    guard = None
    if assurance.platform == "nt":
        guard = _windows_private_root(root, create=False, hold=True)
    try:
        before = os.lstat(candidate)
        if _unsafe_file(before):
            raise ScreenshotError("screenshot_invalid", "candidate_reparse")
        if not stat.S_ISREG(before.st_mode):
            raise ScreenshotError("screenshot_invalid", "candidate_not_regular")
        if before.st_size > MAX_PNG:
            raise ScreenshotError("screenshot_too_large", "size_too_large")
        if before.st_size <= len(PNG_SIGNATURE):
            raise ScreenshotError("screenshot_invalid", "size_too_small")
        descriptor = assurance.open(candidate, before)
        try:
            opened = os.fstat(descriptor)
            assurance.check(before, opened, descriptor)
            if assurance.platform != "nt" and opened.st_size != before.st_size:
                raise ScreenshotError("screenshot_invalid", "size_changed")
            if opened.st_size > MAX_PNG:
                raise ScreenshotError("screenshot_too_large", "size_too_large")
            if opened.st_size <= len(PNG_SIGNATURE):
                raise ScreenshotError("screenshot_invalid", "size_too_small")
            chunks = []
            remaining = MAX_PNG + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            assurance.check_after(candidate, opened, after)
        finally:
            os.close(descriptor)
    except ScreenshotError:
        raise
    except OSError as error:
        raise ScreenshotError("screenshot_capture_failed", "candidate_io") from error
    finally:
        if guard is not None:
            guard()
    payload = b"".join(chunks)
    if len(payload) > MAX_PNG:
        raise ScreenshotError("screenshot_too_large", "size_too_large")
    if len(payload) != opened.st_size:
        raise ScreenshotError("screenshot_invalid", "short_read")
    result = _normalize_png(payload)
    if assurance.mode is not None:
        _screenshot_diagnostic(assurance.mode)
    return result


def _capture_png():
    root = _screenshot_root()
    guard = (
        _windows_private_root(root, create=False, hold=True)
        if os.name == "nt"
        else None
    )
    try:
        return _capture_png_in_root(root)
    finally:
        if guard is not None:
            guard()


def _capture_png_in_root(root):
    _cleanup_stale_screenshots(root)
    filename = "capture-" + uuid.uuid4().hex + ".png"
    native_path = os.path.join(root, filename)
    special_path = SCREENSHOT_ROOT + "/" + filename
    if os.name == "posix" and xbmc.getCondVisibility("System.Platform.Android"):
        special_path = native_path
    if os.path.lexists(native_path):
        raise ScreenshotError("screenshot_capture_failed", "candidate_preexisting")
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
                    return _read_screenshot(native_path, root)
                except ScreenshotError as error:
                    if error.code == "screenshot_too_large" or error.reason in {
                        "root_not_private",
                        "privacy_query_failed",
                    }:
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
            raise ScreenshotError("screenshot_cleanup_failed", "cleanup_failed")


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
        _remove_deployment_tree(stage)
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
        or (os.name == "nt" and not _windows_component(value))
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
            or (
                os.name == "nt"
                and (
                    not _windows_component(part)
                    or part.casefold() in {".git", ".hg", ".svn", "__pycache__"}
                )
            )
        ):
            raise ValueError("invalid relative path")
    if (value.lower() if os.name == "nt" else value).endswith(
        ".pyc"
    ) or "/__pycache__/" in f"/{value}/":
        raise ValueError("bytecode cache is not deployable")
    return value


def _windows_component(part):
    # Reject Win32 aliases, device names, alternate streams and drive paths.
    return (
        not any(character in part for character in '<>:"\\|?*')
        and not part.endswith((".", " "))
        and part.split(".", 1)[0].upper()
        not in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        and re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", part.split(".", 1)[0], re.I) is None
    )


def _deployment_metadata(metadata, directory=False):
    if (
        getattr(metadata, "st_file_attributes", 0) & 0x400
        or not (stat.S_ISDIR if directory else stat.S_ISREG)(metadata.st_mode)
        or (not directory and metadata.st_nlink != 1)
    ):
        raise ValueError("unsafe file or directory")


@contextmanager
def _windows_deployment_handle(
    path, directory=False, create=False, write=False, as_descriptor=False, rename=False
):
    """Mandatory reparse/link checks; held handles deny rename/delete sharing."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    open_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    access = 0x80 if directory else (0xC0000000 if write else 0x80000000)
    if rename:
        access |= 0x10000  # DELETE, needed for a handle-bound directory rename.
    handle = open_file(
        path,
        access,
        3 if directory else 1,
        None,
        1 if create else 3,
        0x00200000 | (0x02000000 if directory else 0),
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = None
    try:
        info = _windows_handle_info(handle, native=True)
        if (
            info is None
            or info[0] & 0x400
            or bool(info[0] & 0x10) != directory
            or (not directory and info[1] != 1)
        ):
            raise ValueError("unsafe deployment handle")
        if as_descriptor:
            import msvcrt

            descriptor = msvcrt.open_osfhandle(
                handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY)
            )
        yield handle if descriptor is None else descriptor
    finally:
        if descriptor is None:
            if not close(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.close(descriptor)


@contextmanager
def _deployment_directory(path, *, traverse_only=False):
    """Pin every ancestor without following links, including the Kodi root."""
    path = os.path.abspath(path)
    drive, tail = os.path.splitdrive(path)
    if os.name == "nt" and (
        len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha()
    ):
        raise ValueError("deployment requires a local drive")
    parts = [part for part in tail.split(os.sep) if part]
    from contextlib import ExitStack

    with ExitStack() as stack:
        current = drive + os.sep
        descriptor = None
        for index, part in enumerate([None, *parts]):
            if part is not None:
                current = os.path.join(current, part)
            if os.name == "nt":
                stack.enter_context(_windows_deployment_handle(current, directory=True))
            else:
                # Ancestors only need traversal, including Android storage
                # parents. Keep read access on the final deployment directory.
                access = (
                    getattr(os, "O_PATH", os.O_RDONLY)
                    if traverse_only or index < len(parts)
                    else os.O_RDONLY
                )
                flags = access | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(
                    current if part is None else part, flags, dir_fd=descriptor
                )
                stack.callback(os.close, descriptor)
                _deployment_metadata(os.fstat(descriptor), directory=True)
        yield descriptor


@contextmanager
def _deployment_file(path, mode="rb", permissions=0o644):
    with _deployment_directory(os.path.dirname(path)) as parent:
        if os.name == "nt":
            with _windows_deployment_handle(  # noqa: SIM117 - Python 3.8 syntax.
                path, create=mode == "xb", write=mode != "rb", as_descriptor=True
            ) as descriptor:
                with os.fdopen(
                    os.dup(descriptor), "rb" if mode == "rb" else "r+b"
                ) as stream:
                    _deployment_metadata(os.fstat(stream.fileno()))
                    yield stream
        else:
            flags = os.O_RDONLY if mode == "rb" else os.O_RDWR
            if mode == "xb":
                flags |= os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(
                    os.path.basename(path),
                    flags | os.O_NOFOLLOW,
                    permissions,
                    dir_fd=parent,
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise ValueError("unsafe file") from None
                raise
            with os.fdopen(descriptor, "rb" if mode == "rb" else "r+b") as stream:
                _deployment_metadata(os.fstat(stream.fileno()))
                if mode == "xb" and not _logical_deployment_modes(
                    os.path.dirname(path)
                ):
                    os.fchmod(stream.fileno(), permissions)
                yield stream


def _logical_deployment_modes(root):
    if os.name == "nt":
        return True
    # Android emulated storage is mounted noexec and projects synthetic modes.
    # Ordinary POSIX mounts retain real executable-bit verification.
    return xbmc.getCondVisibility("System.Platform.Android") and bool(
        os.statvfs(root).f_flag & getattr(os, "ST_NOEXEC", 0)
    )


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
    windows_seen = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("invalid manifest entry")
        path = _relative_path(raw.get("path"))
        if path in seen or (prior is not None and path.encode("utf-8") <= prior):
            raise ValueError("manifest order is invalid")
        if os.name == "nt":
            folded = path.casefold()
            if folded in windows_seen:
                raise ValueError("Windows path alias")
            windows_seen.add(folded)
            parent = path.rpartition("/")[0]
            if parent and parent not in seen:
                raise ValueError("manifest parent unavailable")
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


def _tree_manifest(root, expected_entries=None):
    with _deployment_directory(root):
        return _tree_manifest_guarded(root, expected_entries)


def _tree_manifest_guarded(root, expected_entries=None):
    if not os.path.isdir(root) or os.path.islink(root):
        raise ValueError("add-on tree unavailable")
    entries = []
    files = 0
    total = 0
    cache_files = 0
    cache_bytes = 0
    root_real = os.path.realpath(root)
    logical_modes_required = _logical_deployment_modes(root)
    logical_modes = {
        item["path"]: item["mode"]
        for item in (expected_entries or [])
        if item["type"] == "file"
    }
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
            _deployment_metadata(os.lstat(absolute), directory=True)
            entries.append(
                {"mode": 0o755, "path": path, "size": 0, "type": "directory"}
            )
            if len(entries) > MAX_ENTRIES:
                raise ValueError("tree bounds exceeded")
        for name in names:
            path = name if not prefix else prefix + "/" + name
            _relative_path(path)
            absolute = os.path.join(current, name)
            digest = hashlib.sha256()
            with _deployment_file(absolute) as stream:
                metadata = os.fstat(stream.fileno())
                if metadata.st_size > MAX_FILE:
                    raise ValueError("file too large")
                read = 0
                while True:
                    chunk = stream.read(MAX_CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > MAX_FILE:
                        raise ValueError("file too large")
                    digest.update(chunk)
                if (
                    read != metadata.st_size
                    or os.fstat(stream.fileno()).st_size != read
                ):
                    raise ValueError("file changed during inspection")
            files += 1
            total += metadata.st_size
            if (
                files + cache_files > MAX_FILES
                or len(entries) >= MAX_ENTRIES
                or total + cache_bytes > MAX_TOTAL
            ):
                raise ValueError("tree bounds exceeded")
            mode = 0o755 if metadata.st_mode & 0o111 else 0o644
            if logical_modes_required:
                # Retain authenticated manifest modes on NTFS and Android
                # noexec mounts; bytes, sizes and paths stay exact.
                mode = logical_modes.get(path, 0o644)
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
    with _deployment_directory(root):
        return _validate_bytecode_cache_guarded(root)


def _validate_bytecode_cache_guarded(root):
    try:
        metadata = os.lstat(root)
        _deployment_metadata(metadata, directory=True)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("unsafe bytecode cache")
        files = 0
        total = 0
        with os.scandir(root) as stream:
            for entry in stream:
                name = entry.name
                item = entry.stat(follow_symlinks=False)
                _deployment_metadata(item)
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
    except ValueError as error:
        raise ValueError("unsafe bytecode cache") from error
    except OSError as error:
        raise ValueError("unsafe bytecode cache") from error


def _addon_descriptor(root):
    descriptor = os.path.join(root, "addon.xml")
    with _deployment_file(descriptor) as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("descriptor unavailable")
    parsed = ET.fromstring(raw)
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


def _managed_launch_marker(state):
    path = os.path.join(
        os.path.dirname(xbmcvfs.translatePath(STATE_PATH)), "managed-launch.json"
    )
    with _deployment_file(path) as stream:
        raw = stream.read(2049)
    if len(raw) > 2048:
        raise ValueError("managed marker invalid")
    marker = json.loads(raw.decode("ascii"))
    if not isinstance(marker, dict) or set(marker) != {"binding", "signature"}:
        raise ValueError("managed marker invalid")
    binding = marker["binding"]
    if not isinstance(binding, dict) or set(binding) != {
        "target_id",
        "runtime_revision_id",
        "container_id",
        "generation",
    }:
        raise ValueError("managed marker invalid")
    secret = _decode_b64(state["secret"], 32)
    signature = hmac.new(
        secret, b"managed-launch-v1|" + _canonical(binding), hashlib.sha256
    ).hexdigest()
    if binding["target_id"] != state["target_id"] or not hmac.compare_digest(
        signature, marker["signature"]
    ):
        raise ValueError("managed marker invalid")
    return binding


def _launch_path(launch_id):
    return os.path.join(
        os.path.dirname(xbmcvfs.translatePath(STATE_PATH)),
        "launch-" + _uuid(launch_id) + ".json",
    )


def _read_launch(launch_id):
    path = _launch_path(launch_id)
    try:
        with _deployment_file(path) as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return None
    if len(raw) > 8192:
        raise ValueError("launch receipt invalid")
    return json.loads(raw.decode("utf-8"))


def _save_launch(receipt):
    path = _launch_path(receipt["launch_id"])
    temporary = path + "." + str(uuid.uuid4()) + ".tmp"
    try:
        with _deployment_file(temporary, "xb", permissions=0o600) as stream:
            stream.write(_canonical(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(os.path.dirname(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _managed_launch(payload, state, *, status_only=False):
    binding = _managed_launch_marker(state)
    keys = (
        {"launch_id"}
        if status_only
        else {
            "launch_id",
            "addon_id",
            "addon_version",
            "manifest_digest",
            "ordinal",
            "runtime_binding",
        }
    )
    if set(payload) != keys:
        raise ValueError("launch request invalid")
    launch_id = _uuid(payload["launch_id"])
    prior = _read_launch(launch_id)
    if status_only:
        # Never call the dispatcher from a receipt query, even after cache loss.
        return {"identity": _identity(state), "receipt": prior}
    if payload["runtime_binding"] != binding:
        raise ValueError("launch runtime changed")
    intent = dict(payload)
    if prior is not None:
        if prior["intent"] != intent:
            raise ValueError("launch request conflict")
        return {"identity": _identity(state), "receipt": prior}
    addon_id = _addon_id(payload["addon_id"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", addon_id):
        raise ValueError("unsupported add-on identity")
    root = os.path.join(xbmcvfs.translatePath(ADDON_ROOT), addon_id)
    with _deployment_directory(root):
        _entries, digest, _files, _total = _tree_manifest(root)
        if digest != payload["manifest_digest"]:
            raise ValueError("launch deployment changed")
        observed_id, version, _eligible = _addon_descriptor(root)
        if observed_id != addon_id or version != payload["addon_version"]:
            raise ValueError("launch identity changed")
        with _deployment_file(os.path.join(root, "addon.xml")) as stream:
            descriptor = ET.fromstring(stream.read(MAX_FILE + 1))
        extensions = descriptor.findall("extension")
        ordinal = payload["ordinal"]
        if type(ordinal) is not int or not 0 <= ordinal < min(len(extensions), 64):
            raise ValueError("undeclared entry point")
        extension = extensions[ordinal]
        point = extension.attrib.get("point")
        library = extension.attrib.get("library", "")
        if (
            not isinstance(library, str)
            or not 1 <= len(library) <= 256
            or not re.fullmatch(r"[A-Za-z0-9_.\-/]+\.py", library)
            or any(part in ("", ".", "..") for part in library.split("/"))
        ):
            raise ValueError("unsafe launch library")
        # Hold every path component and the exact selected library through dispatch.
        from contextlib import ExitStack

        with ExitStack() as held:
            parent = root
            for part in library.split("/")[:-1]:
                parent = os.path.join(parent, part)
                held.enter_context(_deployment_directory(parent))
            held.enter_context(_deployment_file(os.path.join(root, library)))
            if point == "xbmc.python.script":
                command = (
                    "RunScript(special://home/addons/" + addon_id + "/" + library + ")"
                )
            elif point == "xbmc.python.pluginsource":
                if sum(item.attrib.get("point") == point for item in extensions) != 1:
                    raise ValueError("multiple plugin extensions")
                provides = [
                    token
                    for item in extension.findall("provides")
                    for token in (item.text or "").strip().split()
                ]
                windows = {
                    "video": "videos",
                    "audio": "music",
                    "image": "pictures",
                    "executable": "programs",
                }
                if len(provides) != 1 or provides[0] not in windows:
                    raise ValueError("unsupported plugin content")
                command = (
                    "ActivateWindow("
                    + windows[provides[0]]
                    + ",plugin://"
                    + addon_id
                    + "/,return)"
                )
            else:
                raise ValueError("unsupported launch extension")
            enabled, _details = _addon_enabled(addon_id)
            if not enabled:
                raise ValueError("launch add-on disabled")
            receipt_root = os.path.dirname(_launch_path(launch_id))
            if (
                sum(
                    name.startswith("launch-") and name.endswith(".json")
                    for name in os.listdir(receipt_root)
                )
                >= 4096
            ):
                raise ValueError("launch receipt capacity reached")
            receipt = {
                "launch_id": launch_id,
                "intent": intent,
                "state": "pending",
                "accepted_at": None,
            }
            _save_launch(
                receipt
            )  # Durable barrier: interrupted dispatch is never retried.
            xbmc.executebuiltin(command)
            receipt.update(state="accepted", accepted_at=_timestamp())
            _save_launch(receipt)
    return {"identity": _identity(state), "receipt": receipt}


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
    if os.name == "nt":
        # Win32 does not expose POSIX directory fsync. Files are flushed before
        # rename; directory replacement remains best-available, not power-loss atomic.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


def _remove_deployment_tree(path):
    with _deployment_directory(os.path.dirname(path)):
        if os.path.lexists(path):
            _deployment_metadata(os.lstat(path), directory=True)
            shutil.rmtree(path)


def _deployment_replace(source, destination):
    parent = os.path.dirname(source)
    if parent != os.path.dirname(destination):
        raise ValueError("replacement must stay in one directory")
    with _deployment_directory(parent) as parent_descriptor:
        if os.path.lexists(destination):
            raise ValueError("replacement destination occupied")
        if os.name != "nt":
            source_name = os.path.basename(source)
            _deployment_metadata(
                os.stat(source_name, dir_fd=parent_descriptor, follow_symlinks=False),
                directory=True,
            )
            os.replace(
                source_name,
                os.path.basename(destination),
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            return
        import ctypes
        from ctypes import wintypes

        class RenameInfo(ctypes.Structure):
            _fields_ = [
                ("replace", wintypes.BOOL),
                ("root", wintypes.HANDLE),
                ("length", wintypes.DWORD),
                ("name", wintypes.WCHAR * 1),
            ]

        encoded = os.path.abspath(destination).encode("utf-16-le")
        size = RenameInfo.name.offset + len(encoded) + 2
        buffer = ctypes.create_string_buffer(max(size, ctypes.sizeof(RenameInfo)))
        info = RenameInfo.from_buffer(buffer)
        info.replace = False
        info.root = None
        info.length = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + RenameInfo.name.offset, encoded, len(encoded)
        )
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        rename_file = kernel.SetFileInformationByHandle
        rename_file.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        rename_file.restype = wintypes.BOOL
        with _windows_deployment_handle(source, directory=True, rename=True) as handle:
            if not rename_file(handle, 3, buffer, len(buffer)):  # FileRenameInfo
                raise ctypes.WinError(ctypes.get_last_error())


def _deployment_diagnostic(request, error):
    category = next(
        (
            kind.__name__
            for kind in (OSError, ValueError, AttributeError, RuntimeError)
            if isinstance(error, kind)
        ),
        "Exception",
    )
    # Never log exception messages, paths, content, envelopes or credentials.
    numbers = ""
    for name in ("errno", "winerror"):
        value = getattr(error, name, None)
        if type(value) is int and 0 <= value <= 65535:
            numbers += " " + name + "=" + str(value)
    xbmc.log(
        "[service.kodi_lab_bridge] operation="
        + request["operation"]
        + " request_id="
        + request["request_id"]
        + " exception="
        + category
        + numbers,
        xbmc.LOGERROR,
    )


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

    def do_GET(self):
        if self.path != BOOTSTRAP_ENDPOINT:
            self._empty(404)
            return
        if not xbmcaddon.Addon(ADDON_ID).getSettingBool("enabled"):
            self._bootstrap_error("bridge_disabled")
            return
        try:
            with _state_guard():
                state = _load_state()
                if _paired(state):
                    self._bootstrap_error("already_paired")
                    return
                status = _onboarding_store().status(paired=False)["status"]
                if status != "Pairing code active":
                    code = {
                        "Pairing code expired": "pairing_code_expired",
                        "Pairing temporarily locked": "pairing_temporarily_locked",
                    }.get(status, "pairing_code_unavailable")
                    self._bootstrap_error(code)
                    return
                body = _canonical(
                    {
                        "status": "available",
                        "bootstrap_version": BOOTSTRAP_VERSION,
                        "bridge_id": state["bridge_id"],
                        "addon_version": ADDON_VERSION,
                        "build_fingerprint": BUILD_FINGERPRINT,
                    }
                )
            self._send(200, {"Content-Type": "application/json"}, body)
        except (OnboardingError, OSError, RuntimeError):
            self._bootstrap_error("pairing_unavailable")

    def do_POST(self):
        if self.path == BOOTSTRAP_ENDPOINT:
            self._bootstrap_http()
            return
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
                with _state_guard():
                    state = _load_state()
                    if _paired(state):
                        secret = _decode_b64(state["secret"], 32)
                        _require_current_pairing(state, document, secret)
                    else:
                        try:
                            transaction = _onboarding_store().provisional(
                                document["target_id"], document["bridge_id"]
                            )
                        except OnboardingError:
                            raise AuthenticationError from None
                        secret = bytes.fromhex(transaction["secret"])
                    if not hmac.compare_digest(
                        _request_signature(secret, document), document["signature"]
                    ):
                        raise AuthenticationError
                self._send(*cached)
                return
            if document.get("operation") == "pair_bridge":
                self._empty(404)
                return
            self._authenticated(document, request_hash)
        except AuthenticationError:
            self._empty(401)
        except OnboardingError as error:
            self._bootstrap_error(str(error))
        except ValueError:
            self._empty(409)
        except Exception:
            # Unexpected server failures are never evidence of a bad credential.
            xbmc.log(
                "[service.kodi_lab_bridge] request processing failed", xbmc.LOGERROR
            )
            self._empty(500)

    def _bootstrap_http(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 1 <= length <= MAX_BOOTSTRAP_REQUEST
                or self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/json"
            ):
                raise ValueError
            document = json.loads(self.rfile.read(length).decode("utf-8"))
            self._bootstrap_pair(document)
        except OnboardingError as error:
            self._bootstrap_error(str(error))
        except (ValueError, TypeError, KeyError, UnicodeError):
            self._bootstrap_error("pairing_request_invalid")
        except (OSError, RuntimeError):
            self._bootstrap_error("pairing_unavailable")

    def _bootstrap_pair(self, document):
        if not isinstance(document, dict) or set(document) != {
            "bootstrap_version",
            "operation",
            "target_id",
            "bridge_id",
            "request_id",
            "timestamp",
            "nonce",
            "credential",
            "otp",
        }:
            raise OnboardingError("pairing_request_invalid")
        if document["bootstrap_version"] != BOOTSTRAP_VERSION:
            raise OnboardingError("bootstrap_version_mismatch")
        if document["operation"] != "otp_bootstrap":
            raise OnboardingError("pairing_request_invalid")
        _uuid(document["target_id"])
        _uuid(document["bridge_id"])
        _uuid(document["request_id"])
        _decode_b64(document["nonce"], 32)
        secret = _decode_b64(document["credential"], 32)
        if not _fresh(document["timestamp"]):
            raise OnboardingError("pairing_request_invalid")
        if not xbmcaddon.Addon(ADDON_ID).getSettingBool("enabled"):
            raise OnboardingError("bridge_disabled")
        with _state_guard():
            state = _load_state()
            if _paired(state):
                raise OnboardingError("already_paired")
            if document["bridge_id"] != state["bridge_id"]:
                raise OnboardingError("bridge_identity_mismatch")
            store = _onboarding_store()
            accepted = store.verify_and_reserve(
                document["otp"],
                self.client_address[0],
                document["target_id"],
                document["request_id"],
                paired=False,
                bridge_id=state["bridge_id"],
                secret=secret,
                nonce=document["nonce"],
                bootstrap_version=BOOTSTRAP_VERSION,
            )
            if not accepted:
                xbmc.log(
                    "[service.kodi_lab_bridge] bootstrap_attempt_failed", xbmc.LOGINFO
                )
                raise OnboardingError("wrong_code")
            provisional = store.provisional(document["target_id"], state["bridge_id"])
        xbmc.log("[service.kodi_lab_bridge] bootstrap_accepted", xbmc.LOGINFO)
        response_request = {
            "operation": "otp_bootstrap",
            "target_id": document["target_id"],
            "bridge_id": document["bridge_id"],
            "request_id": document["request_id"],
        }
        payload = {
            "bootstrap_version": BOOTSTRAP_VERSION,
            "target_id": document["target_id"],
            "bridge_id": state["bridge_id"],
            "bootstrap_request_id": document["request_id"],
            "bootstrap_nonce": document["nonce"],
            "credential_digest": provisional["credential_digest"],
            "provisional_expires_at": provisional["expires_at"],
        }
        self._json_response(response_request, None, secret, payload, cache=False)

    def _bootstrap_error(self, code):
        statuses = {
            "pairing_request_invalid": 400,
            "bootstrap_version_mismatch": 400,
            "bridge_identity_mismatch": 409,
            "wrong_code": 403,
            "pairing_code_expired": 410,
            "pairing_code_unavailable": 410,
            "pairing_temporarily_locked": 429,
            "pairing_in_progress": 409,
            "bootstrap_replay_rejected": 409,
            "already_paired": 409,
            "bridge_disabled": 503,
            "pairing_unavailable": 503,
            "provisional_expired": 410,
        }
        if code not in statuses:
            code = "pairing_unavailable"
        self._send(
            statuses[code],
            {"Content-Type": "application/json"},
            _canonical({"status": "error", "code": code}),
        )

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
        with _state_guard():
            state = _load_state()
            provisional = not _paired(state)
            if provisional:
                transaction = _onboarding_store().provisional(
                    document["target_id"], document["bridge_id"]
                )
                secret = bytes.fromhex(transaction["secret"])
            else:
                if (
                    state["target_id"] != document["target_id"]
                    or state["bridge_id"] != document["bridge_id"]
                ):
                    raise AuthenticationError
                secret = _decode_b64(state["secret"], 32)
            expected = _request_signature(secret, document)
            if not hmac.compare_digest(expected, document["signature"]):
                raise AuthenticationError
            if provisional:
                _onboarding_store().claim_provisional_request(
                    document["target_id"],
                    document["bridge_id"],
                    document["request_id"],
                    document["nonce"],
                )
            else:
                _claim_replay(
                    state,
                    document["request_id"],
                    document["nonce"],
                    document["operation"],
                )
        if provisional and document["operation"] not in {
            "bridge_identity",
            "pairing_commit",
        }:
            self._json_error_response(
                document, request_hash, secret, "provisional_operation_not_allowed"
            )
            return
        if document["operation"] == "bridge_identity":
            if document["payload"] != {}:
                raise ValueError
            payload = _identity(state)
            if provisional:
                xbmc.log("[service.kodi_lab_bridge] provisional_identity", xbmc.LOGINFO)
            self._json_response(document, request_hash, secret, payload)
        elif document["operation"] == "pairing_commit":
            self._pairing_commit(document, request_hash, secret)
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
        elif document["operation"] == "log_read":
            try:
                result = _read_log(document["payload"])
            except LogReadError as error:
                self._json_error_response(document, request_hash, secret, error.code)
                return
            # Sensitive excerpts live only in this response, never in the replay cache.
            self._json_response(
                document,
                request_hash,
                secret,
                {"identity": _identity(state), "log": result},
                cache=False,
            )
        elif document["operation"] == "inspect_ui_state":
            if document["payload"] != {}:
                raise ValueError
            from ui_observation import collect

            try:
                observation = collect(abort=self.server.stopping.is_set)
            except Exception:
                self._json_error_response(
                    document, request_hash, secret, "bridge_operation_failed"
                )
                return
            self._json_response(
                document,
                request_hash,
                secret,
                {
                    "identity": _identity(state),
                    "observation": observation,
                },
            )
        else:
            try:
                self._deployment_response(document, request_hash, secret, state)
            except AuthenticationError:
                raise
            except Exception as error:
                _deployment_diagnostic(document, error)
                code = (
                    "deployment_request_invalid"
                    if isinstance(error, ValueError)
                    else "deployment_cleanup_failed"
                    if isinstance(error, OperationError)
                    and error.code == "deployment_cleanup_failed"
                    else "deployment_installation_failed"
                )
                self._json_error_response(document, request_hash, secret, code)

    def _pairing_commit(self, document, request_hash, secret):
        payload = document["payload"]
        if not isinstance(payload, dict) or set(payload) != {
            "bootstrap_version",
            "bootstrap_request_id",
            "credential_digest",
        }:
            raise ValueError
        if payload["bootstrap_version"] != BOOTSTRAP_VERSION:
            self._json_error_response(
                document, request_hash, secret, "bootstrap_version_mismatch"
            )
            return
        _uuid(payload["bootstrap_request_id"])
        if payload["credential_digest"] != hashlib.sha256(secret).hexdigest():
            self._json_error_response(
                document, request_hash, secret, "conflicting_commit"
            )
            return
        with _state_guard():
            state = _load_state()
            if _paired(state):
                if (
                    state["target_id"] != document["target_id"]
                    or state["bridge_id"] != document["bridge_id"]
                    or _decode_b64(state["secret"], 32) != secret
                ):
                    self._json_error_response(
                        document, request_hash, secret, "conflicting_commit"
                    )
                    return
                accepted = _onboarding_store().clear_provisional(
                    document["target_id"],
                    document["bridge_id"],
                    payload["bootstrap_request_id"],
                )
                if not accepted:
                    self._json_error_response(
                        document, request_hash, secret, "conflicting_commit"
                    )
                    return
            else:
                transaction = _onboarding_store().provisional(
                    document["target_id"], document["bridge_id"]
                )
                if (
                    transaction["request_id"] != payload["bootstrap_request_id"]
                    or transaction["credential_digest"] != payload["credential_digest"]
                ):
                    self._json_error_response(
                        document, request_hash, secret, "conflicting_commit"
                    )
                    return
                state["target_id"] = document["target_id"]
                state["secret"] = _b64(secret)
                state["replay"] = [
                    [
                        document["request_id"],
                        document["nonce"],
                        int(time.time()),
                        "pairing_commit",
                    ]
                ]
                _save_state(state)
                _onboarding_store().clear_provisional(
                    document["target_id"],
                    document["bridge_id"],
                    payload["bootstrap_request_id"],
                )
                xbmc.log("[service.kodi_lab_bridge] pairing_committed", xbmc.LOGINFO)
        self._json_response(
            document,
            request_hash,
            secret,
            {"bootstrap_version": BOOTSTRAP_VERSION, "committed": True},
        )

    def _deployment_response(self, request, request_hash, secret, state):
        operation = request["operation"]
        payload = request["payload"]
        with (
            _DEPLOYMENT_LOCK,
            _state_guard(),
            _deployment_directory(xbmcvfs.translatePath(ADDON_ROOT)),
        ):
            state = _load_state()
            _require_current_pairing(state, request, secret)
            if operation in {"managed_launch", "managed_launch_receipt"}:
                try:
                    result = _managed_launch(
                        payload,
                        state,
                        status_only=operation == "managed_launch_receipt",
                    )
                except (OSError, ValueError, ET.ParseError):
                    prior = _read_launch(payload.get("launch_id"))
                    self._json_error_response(
                        request,
                        request_hash,
                        secret,
                        "launch_dispatch_unconfirmed"
                        if prior is not None
                        else "launch_request_rejected",
                    )
                    return
            elif operation == "inspect_addon":
                addon_id = _addon_id(payload.get("addon_id"))
                if set(payload) != {"addon_id"}:
                    raise ValueError
                root = os.path.join(xbmcvfs.translatePath(ADDON_ROOT), addon_id)
                try:
                    prior_entries = None
                    if _logical_deployment_modes(xbmcvfs.translatePath(ADDON_ROOT)):
                        latest = ""
                        for identity in state["deployments"]:
                            prior = _load_operation(identity)
                            if (
                                prior["addon_id"] == addon_id
                                and prior["state"] == "succeeded"
                                and (prior.get("completed_at") or "") > latest
                            ):
                                prior_entries = prior["entries"]
                                latest = prior["completed_at"]
                    _entries, digest, files, total = _tree_manifest(root, prior_entries)
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
        with _deployment_directory(xbmcvfs.translatePath(ADDON_ROOT)):
            return self._deployment_begin_guarded(payload, state)

    def _deployment_begin_guarded(self, payload, state):
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
                    with _deployment_directory(os.path.dirname(target)) as parent:
                        if os.name == "nt":
                            os.mkdir(target, 0o755)
                        else:
                            os.mkdir(os.path.basename(target), 0o755, dir_fd=parent)
                else:
                    parent = os.path.dirname(target)
                    if not os.path.isdir(parent):
                        raise ValueError("manifest parent unavailable")
                    with _deployment_file(target, "xb", entry["mode"]):
                        pass
            operation = {
                "addon_id": addon_id,
                "addon_version": addon_version,
                "target_id": state["target_id"],
                "bridge_id": state["bridge_id"],
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
            try:
                _remove_deployment_tree(stage)
            except Exception:
                raise OperationError("deployment_cleanup_failed") from None
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
        if (
            operation.get("target_id") != state["target_id"]
            or operation.get("bridge_id") != state["bridge_id"]
        ):
            raise ValueError("deployment pairing changed")
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
        offset = payload["offset"]
        with _deployment_file(target, "r+b") as stream:
            current = os.fstat(stream.fileno()).st_size
            if offset < current:
                stream.seek(offset)
                if stream.read(len(content)) != content:
                    raise ValueError("chunk conflict")
            elif offset == current and current + len(content) <= matching[0]["size"]:
                stream.seek(current)
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
                with _deployment_file(candidate) as stream:
                    if os.fstat(stream.fileno()).st_size != entry["size"]:
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
        if (
            operation.get("target_id") != state["target_id"]
            or operation.get("bridge_id") != state["bridge_id"]
        ):
            raise ValueError("deployment pairing changed")
        if operation["state"] != "ready":
            raise ValueError("transfer incomplete")
        stage = _stage_path(deployment_id)
        scanned, digest, files, total = _tree_manifest(stage, operation["entries"])
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
        before_tree = _tree_manifest(destination)
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
                    _remove_deployment_tree(prior_path)
            if not os.path.isdir(destination) or os.path.islink(destination):
                raise ValueError("destination identity unavailable")
            _deployment_metadata(os.lstat(destination), directory=True)
            _deployment_metadata(os.lstat(stage), directory=True)
            if os.path.lexists(backup):
                raise ValueError("backup identity collision")
            _deployment_replace(destination, backup)
            moved_old = True
            _deployment_replace(stage, destination)
            moved_new = True
            _fsync_directory(os.path.dirname(destination))
            if enabled:
                _set_enabled(operation["addon_id"], True)
            after_entries, after_digest, after_files, after_total = _tree_manifest(
                destination, operation["entries"]
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
        except Exception as error:
            _deployment_diagnostic(
                {
                    "operation": "deployment_commit",
                    "request_id": operation["deployment_request_id"],
                },
                error,
            )
            operation["inventory_verified"] = False
            rollback_ok = False
            restored_before_mutation = False
            try:
                if moved_new and os.path.isdir(destination):
                    failed = stage + ".failed"
                    if os.path.lexists(failed):
                        raise ValueError("failed staging identity collision")
                    _deployment_replace(destination, failed)
                if moved_old:
                    _deployment_replace(backup, destination)
                    _fsync_directory(os.path.dirname(destination))
                if enabled:
                    _set_enabled(operation["addon_id"], True)
                # A native rename may finish before handle cleanup reports an
                # error. Never infer restoration from local move flags alone.
                restored_tree = _tree_manifest(destination)
                restored_enabled, restored_details = _addon_enabled(
                    operation["addon_id"]
                )
                if (
                    restored_tree != before_tree
                    or restored_enabled != enabled
                    or restored_details.get("version") != _before.get("version")
                ):
                    raise ValueError("rollback verification failed")
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

    def _json_response(self, request, request_hash, secret, payload, cache=True):
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
        if cache:
            _cache_put(request["request_id"], request_hash, 200, headers, body)
        self._send(200, headers, body)

    def _json_error_response(self, request, request_hash, secret, code):
        if code not in {
            "launch_request_rejected",
            "launch_dispatch_unconfirmed",
            "log_request_invalid",
            "log_read_busy",
            "log_missing",
            "log_changed",
            "log_unavailable",
            "deployment_request_invalid",
            "deployment_installation_failed",
            "deployment_cleanup_failed",
            "addon_inspection_failed",
            "provisional_operation_not_allowed",
            "conflicting_commit",
            "bootstrap_version_mismatch",
        }:
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
                if error.reason in {
                    "candidate_path",
                    "candidate_not_regular",
                    "candidate_reparse",
                    "candidate_hardlink",
                    "identity_unavailable",
                    "identity_changed",
                    "size_too_small",
                    "size_too_large",
                    "size_changed",
                    "short_read",
                    "candidate_io",
                    "candidate_preexisting",
                    "cleanup_failed",
                    "png_signature",
                    "png_chunk",
                    "png_crc",
                    "png_ihdr",
                    "png_encoding",
                    "png_zlib",
                    "png_filter",
                    "root_not_private",
                    "privacy_query_failed",
                }:
                    xbmc.log(
                        "[service.kodi_lab_bridge] screenshot rejected: "
                        + error.reason,
                        xbmc.LOGWARNING,
                    )
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

    def __init__(self, *args, **kwargs):
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)


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
    try:
        with _state_guard():
            _onboarding_store().initialize(paired=_paired(_load_state()))
    except (OnboardingError, OSError, RuntimeError):
        xbmc.log(
            "[service.kodi_lab_bridge] onboarding state unavailable", xbmc.LOGERROR
        )
        return
    server = Server(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="kodi-lab-bridge")
    thread.daemon = True
    thread.start()
    try:
        while not monitor.waitForAbort(1.0):
            pass
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

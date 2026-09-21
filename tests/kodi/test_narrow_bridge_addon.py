from __future__ import annotations

import hashlib
import os
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.kodi.bridge import (
    BRIDGE_ADDON_ID,
    BRIDGE_PROTOCOL_VERSION,
    b64url,
    request_signature,
    response_signature,
    timestamp_text,
)
from kodi_lab.kodi.screenshots import EXPECTED_HEIGHT, EXPECTED_WIDTH, validate_png

ADDON = (
    Path(__file__).parents[2]
    / "bridge"
    / "service.kodi_lab_bridge"
    / "narrow_bridge.py"
)


def _module(name: str, **values: object) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pixels: bytes | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    class RenderCapture:
        def capture(self, width: int, height: int) -> None:
            assert (width, height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT)

        def getImage(self, timeout: int) -> bytes:
            assert timeout == 5000
            assert pixels is not None
            return pixels

    monkeypatch.setitem(
        sys.modules,
        "xbmc",
        _module(
            "xbmc",
            RenderCapture=RenderCapture,
            getInfoLabel=lambda _name: "21.3 (21.3.0)",
            log=lambda *_args: None,
            LOGDEBUG=0,
            LOGINFO=1,
            LOGERROR=2,
            Monitor=object,
        ),
    )
    monkeypatch.setitem(sys.modules, "xbmcaddon", _module("xbmcaddon", Addon=object))
    if state_path is None:
        vfs = _module("xbmcvfs")
    else:

        class VfsFile:
            def __init__(self, path: str, mode: str = "rb") -> None:
                self._handle = open(path, mode)  # noqa: SIM115 - Kodi-shaped double.

            def read(self) -> bytes:
                return cast(bytes, self._handle.read())

            def write(self, value: bytes) -> int:
                return self._handle.write(value)

            def close(self) -> None:
                self._handle.close()

        vfs = _module(
            "xbmcvfs",
            translatePath=lambda _path: str(state_path),
            exists=lambda path: Path(path).exists(),
            mkdirs=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            File=VfsFile,
        )
    monkeypatch.setitem(sys.modules, "xbmcvfs", vfs)
    return runpy.run_path(str(ADDON))


def test_addon_protocol_is_closed_and_crypto_matches_lab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load(monkeypatch)
    assert bridge["PROTOCOL"] == BRIDGE_PROTOCOL_VERSION
    assert bridge["ADDON_ID"] == BRIDGE_ADDON_ID
    assert bridge["OPERATIONS"] == {
        "bridge_identity",
        "bridge_capabilities",
        "capture_screenshot",
    }
    assert bridge["CAPABILITIES"] == ["screenshot"]

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
    assert bridge["_request_signature"](secret, envelope) == request_signature(
        secret, envelope
    )
    digest = hashlib.sha256(b"payload").hexdigest()
    response_arguments = (
        secret,
        envelope["target_id"],
        envelope["bridge_id"],
        envelope["operation"],
        envelope["request_id"],
        envelope["timestamp"],
        envelope["nonce"],
        "ok",
        digest,
    )
    assert bridge["_response_signature"](*response_arguments) == response_signature(
        secret,
        target_id=cast(str, envelope["target_id"]),
        bridge_id=cast(str, envelope["bridge_id"]),
        operation=cast(str, envelope["operation"]),
        request_id=cast(str, envelope["request_id"]),
        timestamp=cast(str, envelope["timestamp"]),
        nonce=cast(str, envelope["nonce"]),
        status="ok",
        payload_digest=digest,
    )


def test_addon_capture_returns_fixed_bounded_png_without_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bgra = b"\x10\x20\x30\xff" * (EXPECTED_WIDTH * EXPECTED_HEIGHT)
    bridge = _load(monkeypatch, pixels=bgra)
    png = cast(bytes, bridge["_capture_png"]())
    validated = validate_png(png)
    assert (validated.width, validated.height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT)
    assert validated.byte_size <= bridge["MAX_PNG"]


def test_addon_source_has_no_legacy_broad_authority() -> None:
    source = ADDON.read_text()
    for forbidden in (
        "Files.GetDirectory",
        "Addons.SetAddonEnabled",
        "log_path",
        "filesystem",
        "jsonrpc_method",
        "special://logpath",
        "restart",
        "shell",
    ):
        assert forbidden not in source


def test_addon_bridge_uuid_is_random_and_persistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "addon_data" / "state.json"
    bridge = _load(monkeypatch, state_path=state_path)
    first = bridge["_load_state"]()
    second = bridge["_load_state"]()
    assert first["bridge_id"] == second["bridge_id"]
    assert first["target_id"] is None
    assert first["secret"] is None
    assert state_path.exists()
    assert bridge["_state_default"]()["bridge_id"] != first["bridge_id"]

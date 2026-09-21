from __future__ import annotations

import binascii
import hashlib
import json
import os
import runpy
import struct
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zlib
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
from kodi_lab.kodi.screenshots import validate_external_png

ADDON_ROOT = Path(__file__).parents[2] / "bridge" / "service.kodi_lab_bridge"
ADDON = ADDON_ROOT / "narrow_bridge.py"


def _module(name: str, **values: object) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_path: Path | None = None,
    addon_root: Path | None = None,
    temp_root: Path | None = None,
    screenshot_png: bytes | None = None,
    screenshot_commands: list[str] | None = None,
    jsonrpc_requests: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    enabled = {"value": True}

    def execute_builtin(command: str) -> None:
        assert command.startswith(
            "TakeScreenshot(special://temp/kodi-lab-bridge/capture-"
        )
        assert command.endswith(".png,sync)")
        if screenshot_commands is not None:
            screenshot_commands.append(command)
        if screenshot_png is not None:
            assert temp_root is not None
            filename = command.removeprefix(
                "TakeScreenshot(special://temp/kodi-lab-bridge/"
            ).removesuffix(",sync)")
            destination = temp_root / "kodi-lab-bridge" / filename
            destination.write_bytes(screenshot_png)

    def execute_jsonrpc(raw: str) -> str:
        request = json.loads(raw)
        if jsonrpc_requests is not None:
            jsonrpc_requests.append(request)
        method = request["method"]
        if method == "Addons.SetAddonEnabled":
            enabled["value"] = bool(request["params"]["enabled"])
            result: object = "OK"
        elif method == "Addons.GetAddonDetails" and addon_root is not None:
            addon_id = request["params"]["addonid"]
            descriptor = ET.parse(addon_root / addon_id / "addon.xml").getroot()
            result = {
                "addon": {
                    "addonid": addon_id,
                    "enabled": enabled["value"],
                    "type": "xbmc.python.pluginsource",
                    "version": descriptor.attrib["version"],
                }
            }
        else:
            result = {}
        return json.dumps({"id": "kodi-lab-bridge", "jsonrpc": "2.0", "result": result})

    monkeypatch.setitem(
        sys.modules,
        "xbmc",
        _module(
            "xbmc",
            executebuiltin=execute_builtin,
            getInfoLabel=lambda _name: "21.3 (21.3.0)",
            log=lambda *_args: None,
            LOGDEBUG=0,
            LOGINFO=1,
            LOGERROR=2,
            LOGWARNING=3,
            Monitor=object,
            executeJSONRPC=execute_jsonrpc,
        ),
    )
    monkeypatch.setitem(sys.modules, "xbmcaddon", _module("xbmcaddon", Addon=object))
    if state_path is not None or temp_root is not None:

        class VfsFile:
            def __init__(self, path: str, mode: str = "rb") -> None:
                self._handle = open(path, mode)  # noqa: SIM115 - Kodi-shaped double.

            def read(self) -> bytes:
                return cast(bytes, self._handle.read())

            def write(self, value: bytes) -> int:
                return self._handle.write(value)

            def close(self) -> None:
                self._handle.close()

        def translate(path: str) -> str:
            if path == "special://home/addons" and addon_root is not None:
                return str(addon_root)
            if path == "special://temp" and temp_root is not None:
                return str(temp_root)
            if path == "special://temp/kodi-lab-bridge" and temp_root is not None:
                return str(temp_root / "kodi-lab-bridge")
            if state_path is not None:
                return str(state_path)
            raise AssertionError(path)

        vfs = _module(
            "xbmcvfs",
            translatePath=translate,
            exists=lambda path: Path(path).exists(),
            mkdirs=lambda path: Path(path).mkdir(parents=True, exist_ok=True),
            File=VfsFile,
        )
    else:
        vfs = _module("xbmcvfs")
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
        "inspect_addon",
        "deployment_begin",
        "deployment_chunk",
        "deployment_commit",
        "deployment_receipt",
    }
    assert bridge["CAPABILITIES"] == ["screenshot", "workspace_deployment"]

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


def test_addon_descriptor_and_settings_are_kodi_20_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load(monkeypatch)
    descriptor = ET.parse(ADDON_ROOT / "addon.xml").getroot()
    assert descriptor.attrib["id"] == bridge["ADDON_ID"]
    assert descriptor.attrib["version"] == bridge["ADDON_VERSION"] == "1.1.7"
    dependency = descriptor.find("./requires/import")
    assert dependency is not None and dependency.attrib == {
        "addon": "xbmc.python",
        "version": "3.0.0",
    }
    service = descriptor.find("./extension[@point='xbmc.service']")
    assert service is not None and service.attrib == {
        "point": "xbmc.service",
        "library": "service.py",
        "start": "login",
    }

    settings = ET.parse(ADDON_ROOT / "resources" / "settings.xml").getroot()
    assert settings.attrib == {"version": "1"}
    definitions = {
        setting.attrib["id"]: setting for setting in settings.findall(".//setting")
    }
    assert set(definitions) == {"enabled", "bridge_port", "pairing_token"}
    assert all(setting.find("control") is not None for setting in definitions.values())
    assert definitions["enabled"].findtext("default") == "false"
    assert definitions["bridge_port"].findtext("default") == "8765"
    assert definitions["bridge_port"].findtext("constraints/minimum") == "1"
    assert definitions["bridge_port"].findtext("constraints/maximum") == "65535"
    assert definitions["pairing_token"].findtext("constraints/allowempty") == "true"
    assert definitions["pairing_token"].findtext("control/hidden") == "true"

    labels = {
        element.attrib[field]
        for element in settings.findall(".//*[@label]")
        for field in ("label",)
    }
    helps = {
        element.attrib[field]
        for element in settings.findall(".//*[@help]")
        for field in ("help",)
    }
    assert labels == {"32000", "32001", "32010", "32020", "32030"}
    assert helps == {"32011", "32021", "32031"}
    catalog = (
        ADDON_ROOT / "resources/language/resource.language.en_gb/strings.po"
    ).read_text()
    for message_id in labels | helps:
        assert f'msgctxt "#{message_id}"' in catalog


def test_addon_inspection_uses_kodi_20_compatible_detail_properties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon = addon_root / "plugin.video.fixture"
    _write_video_plugin(addon, "1.0.0", "pass\n")
    requests: list[dict[str, object]] = []
    bridge = _load(
        monkeypatch,
        addon_root=addon_root,
        jsonrpc_requests=requests,
    )

    enabled, details = bridge["_addon_enabled"]("plugin.video.fixture")

    assert enabled is True
    assert details["version"] == "1.0.0"
    assert requests == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-bridge",
            "method": "Addons.GetAddonDetails",
            "params": {
                "addonid": "plugin.video.fixture",
                "properties": ["enabled", "version"],
            },
        }
    ]
    params = cast(dict[str, object], requests[0]["params"])
    assert "type" not in cast(list[str], params["properties"])


def test_addon_inspection_error_response_is_authenticated_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load(monkeypatch)
    request = {
        "operation": "inspect_addon",
        "target_id": str(uuid4()),
        "bridge_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    secret = os.urandom(32)
    sent: dict[str, object] = {}
    handler = object.__new__(bridge["Handler"])

    def capture(status: int, headers: dict[str, str], body: bytes) -> None:
        sent.update(status=status, headers=headers, body=body)

    handler._send = capture
    handler._json_error_response(request, "0" * 64, secret, "addon_inspection_failed")

    assert sent["status"] == 422
    assert sent["headers"] == {"Content-Type": "application/json"}
    response = json.loads(cast(bytes, sent["body"]))
    assert response["status"] == "addon_inspection_failed"
    assert response["payload"] == {}
    assert response["signature"] == bridge["_response_signature"](
        secret,
        request["target_id"],
        request["bridge_id"],
        request["operation"],
        request["request_id"],
        response["timestamp"],
        response["nonce"],
        "addon_inspection_failed",
        hashlib.sha256(b"{}").hexdigest(),
    )


def _png(width: int = 4, height: int = 3, *, rgba: bool = True) -> bytes:
    channels = 4 if rgba else 3
    rows = b"".join(
        b"\x00" + bytes((row + column) % 256 for column in range(width * channels))
        for row in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 6 if rgba else 2, 0, 0, 0),
            ),
            chunk(b"IDAT", zlib.compress(rows)),
            chunk(b"IEND", b""),
        )
    )


def test_addon_gui_capture_uses_fixed_temp_namespace_without_playback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[str] = []
    bridge = _load(
        monkeypatch,
        temp_root=tmp_path,
        screenshot_png=_png(),
        screenshot_commands=commands,
    )
    png = cast(bytes, bridge["_capture_png"]())
    validated = validate_external_png(png)
    assert (validated.width, validated.height) == (4, 3)
    assert commands and "TakeScreenshot(" in commands[0]
    assert "RenderCapture" not in ADDON.read_text()
    capture_root = tmp_path / "kodi-lab-bridge"
    assert list(capture_root.iterdir()) == []


def test_addon_capture_protocol_rejects_even_authenticated_path_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state" / "state.json"
    bridge = _load(monkeypatch, state_path=state_path, temp_root=tmp_path / "temp")
    state = bridge["_load_state"]()
    target_id = str(uuid4())
    secret = os.urandom(32)
    state["target_id"] = target_id
    state["secret"] = b64url(secret)
    bridge["_save_state"](state)
    request: dict[str, object] = {
        "protocol_version": bridge["PROTOCOL"],
        "operation": "capture_screenshot",
        "target_id": target_id,
        "bridge_id": state["bridge_id"],
        "request_id": str(uuid4()),
        "timestamp": bridge["_timestamp"](),
        "nonce": b64url(os.urandom(32)),
        "payload": {"path": "special://temp/attacker.png"},
    }
    request["signature"] = bridge["_request_signature"](secret, request)
    handler = object.__new__(bridge["Handler"])
    with pytest.raises(ValueError):
        handler._authenticated(request, "0" * 64)


def test_addon_gui_capture_deletes_invalid_and_oversized_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path)
    module: Any = sys.modules["xbmc"]

    def install(payload: bytes) -> None:
        def execute(command: str) -> None:
            filename = command.rsplit("/", 1)[1].removesuffix(",sync)")
            (tmp_path / "kodi-lab-bridge" / filename).write_bytes(payload)

        module.executebuiltin = execute

    install(b"not-a-png")
    with pytest.raises(bridge["ScreenshotError"], match="screenshot_invalid"):
        bridge["_capture_png"]()
    assert list((tmp_path / "kodi-lab-bridge").iterdir()) == []

    install(b"x" * (bridge["MAX_PNG"] + 1))
    with pytest.raises(bridge["ScreenshotError"], match="screenshot_too_large"):
        bridge["_capture_png"]()
    assert list((tmp_path / "kodi-lab-bridge").iterdir()) == []


def test_addon_gui_capture_bounds_kodi_22_asynchronous_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path)
    module: Any = sys.modules["xbmc"]
    writers: list[threading.Thread] = []

    def execute(command: str) -> None:
        filename = command.rsplit("/", 1)[1].removesuffix(",sync)")
        destination = tmp_path / "kodi-lab-bridge" / filename
        destination.write_bytes(b"")

        def finish() -> None:
            time.sleep(0.04)
            destination.write_bytes(_png())

        writer = threading.Thread(target=finish)
        writer.start()
        writers.append(writer)

    module.executebuiltin = execute
    png = bridge["_capture_png"]()
    for writer in writers:
        writer.join()
    assert validate_external_png(png).width == 4
    assert list((tmp_path / "kodi-lab-bridge").iterdir()) == []


def test_addon_gui_capture_rejects_symlink_and_cleans_only_bounded_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path)
    root = Path(bridge["_screenshot_root"]())
    stale = root / ("capture-" + ("a" * 32) + ".png")
    fresh = root / ("capture-" + ("b" * 32) + ".png")
    unknown = root / "keep-me.png"
    stale.write_bytes(b"stale")
    fresh.write_bytes(b"fresh")
    unknown.write_bytes(b"unknown")
    old = time.time() - bridge["SCREENSHOT_STALE_TTL"] - 1
    os.utime(stale, (old, old))
    scanned, removed = bridge["_cleanup_stale_screenshots"](root)
    assert scanned <= bridge["MAX_SCREENSHOT_SCAN"]
    assert removed == 1
    assert not stale.exists() and fresh.exists() and unknown.exists()

    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    symlink = root / ("capture-" + ("c" * 32) + ".png")
    try:
        symlink.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(bridge["ScreenshotError"], match="screenshot_invalid"):
        bridge["_read_screenshot"](str(symlink))
    assert outside.exists()


def test_addon_gui_capture_rejects_hardlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path)
    root = Path(bridge["_screenshot_root"]())
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    hardlink = root / ("capture-" + ("d" * 32) + ".png")
    try:
        os.link(outside, hardlink)
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(bridge["ScreenshotError"], match="screenshot_invalid"):
        bridge["_read_screenshot"](str(hardlink))
    assert outside.exists()


def test_screenshot_replay_identity_cannot_be_evicted_by_bridge_traffic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, state_path=tmp_path / "state.json")
    state = bridge["_state_default"]()
    request_id = str(uuid4())
    nonce = bridge["_b64"](os.urandom(32))
    bridge["_claim_replay"](state, request_id, nonce, "capture_screenshot")

    for _index in range(bridge["MAX_REPLAY"]):
        bridge["_claim_replay"](
            state,
            str(uuid4()),
            bridge["_b64"](os.urandom(32)),
            "deployment_chunk",
        )

    assert len(state["replay"]) == bridge["MAX_REPLAY"]
    assert any(item[0] == request_id for item in state["replay"])
    with pytest.raises(ValueError, match="replay"):
        bridge["_claim_replay"](
            state, request_id, bridge["_b64"](os.urandom(32)), "capture_screenshot"
        )


def test_screenshot_replay_capacity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, state_path=tmp_path / "state.json")
    state = bridge["_state_default"]()
    state["replay"] = [[str(uuid4()), bridge["_b64"](os.urandom(32)), int(time.time())]]
    for _index in range(bridge["MAX_REPLAY"] - 1):
        bridge["_claim_replay"](
            state,
            str(uuid4()),
            bridge["_b64"](os.urandom(32)),
            "capture_screenshot",
        )
    with pytest.raises(ValueError, match="replay capacity"):
        bridge["_claim_replay"](
            state,
            str(uuid4()),
            bridge["_b64"](os.urandom(32)),
            "bridge_identity",
        )


def test_addon_screenshot_failure_is_authenticated_semantic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load(monkeypatch)
    handler = object.__new__(bridge["Handler"])
    sent: list[tuple[int, dict[str, str], bytes]] = []
    handler._send = lambda status, headers, body: sent.append((status, headers, body))
    globals_ = handler._screenshot_response.__func__.__globals__
    globals_["_capture_png"] = lambda: (_ for _ in ()).throw(
        bridge["ScreenshotError"]("screenshot_capture_failed")
    )
    request = {
        "target_id": str(uuid4()),
        "bridge_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    handler._screenshot_response(request, "0" * 64, os.urandom(32))
    assert sent[0][0] == 422
    assert sent[0][1]["X-Kodi-Lab-Status"] == "screenshot_capture_failed"
    assert sent[0][2] == b""

    globals_["_capture_png"] = lambda: (_ for _ in ()).throw(
        RuntimeError("untrusted native detail")
    )
    request["request_id"] = str(uuid4())
    handler._screenshot_response(request, "1" * 64, os.urandom(32))
    assert sent[1][0] == 422
    assert sent[1][1]["X-Kodi-Lab-Status"] == "screenshot_capture_failed"
    assert sent[1][2] == b""


def test_addon_gui_capture_is_deleted_before_response_authentication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path, screenshot_png=_png())
    handler = object.__new__(bridge["Handler"])
    globals_ = handler._screenshot_response.__func__.__globals__
    globals_["_response_signature"] = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("injected response construction failure")
    )
    request = {
        "target_id": str(uuid4()),
        "bridge_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    with pytest.raises(RuntimeError, match="response construction"):
        handler._screenshot_response(request, "0" * 64, os.urandom(32))
    assert list((tmp_path / "kodi-lab-bridge").iterdir()) == []


def test_addon_gui_capture_reports_cleanup_failure_without_returning_pixels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch, temp_root=tmp_path, screenshot_png=_png())
    real_unlink = os.unlink

    def fail_capture_unlink(path: str | bytes | os.PathLike[str]) -> None:
        if Path(os.fsdecode(os.fspath(path))).name.startswith("capture-"):
            raise OSError("injected cleanup failure")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", fail_capture_unlink)
    with pytest.raises(bridge["ScreenshotError"], match="screenshot_cleanup_failed"):
        bridge["_capture_png"]()


def test_addon_source_has_no_legacy_broad_authority() -> None:
    source = ADDON.read_text()
    for forbidden in (
        "Files.GetDirectory",
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


def test_deployment_manifest_is_exact_bounded_and_pathless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _load(monkeypatch)
    content = b"pass\n"
    entries = [
        {
            "mode": 0o644,
            "path": "addon.py",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "type": "file",
        }
    ]
    encoded = bridge["_manifest_bytes"](entries)
    payload = {
        "entries": entries,
        "file_count": 1,
        "manifest_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "manifest_version": 1,
        "total_bytes": len(content),
    }
    normalized, digest, files, total = bridge["_validate_manifest"](payload)
    assert normalized == entries
    assert digest == payload["manifest_digest"]
    assert (files, total) == (1, len(content))
    assert "destination" not in payload and "root" not in payload

    for path in ("/addon.py", "../addon.py", ".git/config", "__pycache__/x.pyc"):
        invalid = dict(payload)
        invalid["entries"] = [{**entries[0], "path": path}]
        with pytest.raises(ValueError):
            bridge["_validate_manifest"](invalid)


def test_deployment_tree_rejects_links_and_direct_bytecode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "addon.py").write_text("pass\n")
    os.symlink(tree / "addon.py", tree / "linked.py")
    with pytest.raises(ValueError, match="unsafe file"):
        bridge["_tree_manifest"](str(tree))
    (tree / "linked.py").unlink()
    (tree / "cached.pyc").write_bytes(b"bytecode")
    with pytest.raises(ValueError, match="bytecode cache is not deployable"):
        bridge["_tree_manifest"](str(tree))


def test_installed_tree_ignores_only_bounded_regular_bytecode_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bridge = _load(monkeypatch)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "addon.py").write_text("pass\n")
    clean = bridge["_tree_manifest"](str(tree))
    cache = tree / "__pycache__"
    cache.mkdir()
    (cache / "addon.cpython-311.pyc").write_bytes(b"derived")
    assert bridge["_tree_manifest"](str(tree)) == clean

    (cache / "unexpected.txt").write_text("not bytecode")
    with pytest.raises(ValueError, match="unsafe bytecode cache"):
        bridge["_tree_manifest"](str(tree))
    (cache / "unexpected.txt").unlink()
    os.symlink(cache / "addon.cpython-311.pyc", cache / "linked.pyc")
    with pytest.raises(ValueError, match="unsafe bytecode cache"):
        bridge["_tree_manifest"](str(tree))


def _write_video_plugin(root: Path, version: str, body: str) -> None:
    root.mkdir(parents=True)
    (root / "addon.xml").write_text(
        '<addon id="plugin.video.fixture" name="Fixture" version="'
        + version
        + '"><extension point="xbmc.python.pluginsource" library="addon.py">'
        "<provides>video</provides></extension></addon>"
    )
    (root / "addon.py").write_text(body)


def _stage_deployment(
    bridge: dict[str, Any],
    handler: Any,
    state: dict[str, Any],
    source: Path,
    *,
    transfer: bool = True,
) -> tuple[str, str, dict[str, object]]:
    entries, digest, files, total = bridge["_tree_manifest"](str(source))
    deployment_id = str(uuid4())
    request_id = str(uuid4())
    payload = {
        "addon_id": "plugin.video.fixture",
        "addon_version": ET.parse(source / "addon.xml").getroot().attrib["version"],
        "deployment_id": deployment_id,
        "deployment_request_id": request_id,
        "entries": entries,
        "file_count": files,
        "generation": 1,
        "manifest_digest": digest,
        "manifest_version": 1,
        "total_bytes": total,
    }
    handler._deployment_begin(payload, state)
    if not transfer:
        return deployment_id, digest, payload
    for entry in entries:
        if entry["type"] != "file":
            continue
        content = (source / str(entry["path"])).read_bytes()
        handler._deployment_chunk(
            {
                "content": b64url(content),
                "content_digest": hashlib.sha256(content).hexdigest(),
                "deployment_id": deployment_id,
                "file_digest": entry["sha256"],
                "offset": 0,
                "path": entry["path"],
            },
            state,
        )
    return deployment_id, digest, payload


def test_transfer_rejects_corruption_manifest_mismatch_and_parallel_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, digest, payload = _stage_deployment(
        bridge, handler, state, source, transfer=False
    )
    entries = cast(list[dict[str, object]], payload["entries"])
    entry = next(item for item in entries if item["type"] == "file")
    content = (source / str(entry["path"])).read_bytes()
    with pytest.raises(ValueError, match="chunk invalid"):
        handler._deployment_chunk(
            {
                "content": b64url(content),
                "content_digest": "0" * 64,
                "deployment_id": deployment_id,
                "file_digest": entry["sha256"],
                "offset": 0,
                "path": entry["path"],
            },
            state,
        )
    with pytest.raises(ValueError, match="manifest mismatch"):
        handler._deployment_commit(
            {"deployment_id": deployment_id, "manifest_digest": "sha256:" + "0" * 64},
            state,
        )
    conflicting = {
        **payload,
        "deployment_id": str(uuid4()),
        "deployment_request_id": str(uuid4()),
    }
    with pytest.raises(ValueError, match="deployment capacity unavailable"):
        handler._deployment_begin(conflicting, state)
    assert digest == payload["manifest_digest"]


def test_bridge_restart_recovers_staging_and_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    destination = addon_root / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    state_path = tmp_path / "state" / "state.json"
    first_bridge = _load(monkeypatch, state_path=state_path, addon_root=addon_root)
    first_state = first_bridge["_load_state"]()
    first_handler = object.__new__(first_bridge["Handler"])
    deployment_id, digest, _payload = _stage_deployment(
        first_bridge, first_handler, first_state, source
    )

    restarted = _load(monkeypatch, state_path=state_path, addon_root=addon_root)
    restarted_state = restarted["_load_state"]()
    restarted_handler = object.__new__(restarted["Handler"])
    receipt = restarted_handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, restarted_state
    )
    assert receipt["state"] == "succeeded"
    assert (
        restarted["_receipt"](
            restarted["_load_operation"](deployment_id), restarted_state["bridge_id"]
        )
        == receipt
    )


def test_deployment_atomic_replace_preserves_enablement_and_replays_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    destination = addon_root / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, digest, _payload = _stage_deployment(bridge, handler, state, source)
    first = handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, state
    )
    replay = handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, state
    )
    assert first == replay
    assert first["state"] == "succeeded"
    assert first["enabled_state"] == "enabled"
    assert first["inventory_verified"] is True
    assert ET.parse(destination / "addon.xml").getroot().attrib["version"] == "2.0.0"
    assert (addon_root / (".kodi-lab-backup-" + deployment_id)).is_dir()


def test_abandoned_staging_bytes_expire_but_receipt_identity_is_retained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, _digest, _payload = _stage_deployment(bridge, handler, state, source)
    state["deployments"][deployment_id]["created"] = 0
    bridge["_cleanup_staging"](state)
    retained = bridge["_load_operation"](deployment_id)
    assert retained["state"] == "failed"
    assert retained["classification"] == "staging_expired"
    assert retained["receipt_id"] is not None
    assert not Path(bridge["_stage_path"](deployment_id)).exists()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_replacement_failure_rolls_back_or_requires_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rollback_fails: bool
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    destination = addon_root / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, digest, _payload = _stage_deployment(bridge, handler, state, source)
    stage = Path(bridge["_stage_path"](deployment_id))
    backup = Path(bridge["_backup_path"](deployment_id))
    real_replace = os.replace

    def fail_selected(source_path: str | Path, destination_path: str | Path) -> None:
        source_value = Path(source_path)
        destination_value = Path(destination_path)
        if source_value == stage and destination_value == destination:
            raise OSError("injected replacement failure")
        if (
            rollback_fails
            and source_value == backup
            and destination_value == destination
        ):
            raise OSError("injected rollback failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_selected)
    receipt = handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, state
    )
    if rollback_fails:
        assert receipt["state"] == "ambiguous"
        assert receipt["classification"] == "rollback_failed_reconciliation_required"
    else:
        assert receipt["state"] == "failed"
        assert receipt["classification"] == "replacement_failed_rolled_back"
        assert (
            ET.parse(destination / "addon.xml").getroot().attrib["version"] == "1.0.0"
        )


def test_replacement_failure_before_tree_mutation_is_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    destination = addon_root / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, digest, _payload = _stage_deployment(bridge, handler, state, source)
    backup = Path(bridge["_backup_path"](deployment_id))
    real_replace = os.replace

    def fail_before_mutation(
        source_path: str | Path, destination_path: str | Path
    ) -> None:
        if Path(source_path) == destination and Path(destination_path) == backup:
            raise OSError("injected pre-mutation failure")
        real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_before_mutation)
    receipt = handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, state
    )
    assert receipt["state"] == "failed"
    assert receipt["classification"] == "replacement_failed_before_mutation"
    assert ET.parse(destination / "addon.xml").getroot().attrib["version"] == "1.0.0"


def test_inventory_mismatch_rolls_back_exact_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addon_root = tmp_path / "addons"
    addon_root.mkdir()
    destination = addon_root / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "state" / "state.json",
        addon_root=addon_root,
    )
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    deployment_id, digest, _payload = _stage_deployment(bridge, handler, state, source)
    globals_ = handler._deployment_commit.__func__.__globals__
    original_inventory = globals_["_addon_enabled"]
    calls = 0

    def mismatched_inventory(addon_id: str) -> tuple[bool, dict[str, object]]:
        nonlocal calls
        calls += 1
        enabled, details = original_inventory(addon_id)
        if calls > 1:
            details = {**details, "version": "9.9.9"}
        return enabled, details

    globals_["_addon_enabled"] = mismatched_inventory
    receipt = handler._deployment_commit(
        {"deployment_id": deployment_id, "manifest_digest": digest}, state
    )
    assert receipt["state"] == "failed"
    assert receipt["classification"] == "replacement_failed_rolled_back"
    assert ET.parse(destination / "addon.xml").getroot().attrib["version"] == "1.0.0"

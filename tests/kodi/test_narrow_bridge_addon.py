from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
import xml.etree.ElementTree as ET
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
    addon_root: Path | None = None,
) -> dict[str, Any]:
    class RenderCapture:
        def capture(self, width: int, height: int) -> None:
            assert (width, height) == (EXPECTED_WIDTH, EXPECTED_HEIGHT)

        def getImage(self, timeout: int) -> bytes:
            assert timeout == 5000
            assert pixels is not None
            return pixels

    enabled = {"value": True}

    def execute_jsonrpc(raw: str) -> str:
        request = json.loads(raw)
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
            RenderCapture=RenderCapture,
            getInfoLabel=lambda _name: "21.3 (21.3.0)",
            log=lambda *_args: None,
            LOGDEBUG=0,
            LOGINFO=1,
            LOGERROR=2,
            Monitor=object,
            executeJSONRPC=execute_jsonrpc,
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

        def translate(path: str) -> str:
            if path == "special://home/addons" and addon_root is not None:
                return str(addon_root)
            return str(state_path)

        vfs = _module(
            "xbmcvfs",
            translatePath=translate,
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


def test_deployment_tree_rejects_links_and_bytecode_cache(
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
    (tree / "__pycache__").mkdir()
    with pytest.raises(ValueError, match="invalid relative path"):
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

"""Android traversal, synthetic executable modes and receipt-backed inspection."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.kodi.bridge import b64url
from tests.kodi.test_narrow_bridge_addon import (
    _load,
    _stage_deployment,
    _write_video_plugin,
)


def _android_mount(
    monkeypatch: pytest.MonkeyPatch, bridge: dict[str, Any], addons: Path
) -> None:
    bridge["xbmc"].getCondVisibility = lambda name: name == "System.Platform.Android"
    proxy = ModuleType("android_os")
    proxy.__dict__.update(vars(os))
    real_fstat = os.fstat

    def projected_stat(descriptor: int) -> os.stat_result:
        value = real_fstat(descriptor)
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if path.is_relative_to(addons) and stat.S_ISREG(value.st_mode):
            fields = list(value)
            fields[0] = stat.S_IFREG | 0o660
            return os.stat_result(fields)
        return value

    def chmod(_descriptor: int, _mode: int) -> None:
        pytest.fail("Android noexec storage must not require chmod support")

    proxy.__dict__.update(
        fstat=projected_stat,
        fchmod=chmod,
        statvfs=lambda path: SimpleNamespace(
            f_flag=os.ST_NOEXEC if Path(path).is_relative_to(addons) else 0
        ),
    )
    monkeypatch.setitem(bridge["_tree_manifest"].__globals__, "os", proxy)


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Requires path-only handles")
def test_deployment_pins_unreadable_ancestors_and_keeps_final_directory_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ancestor = tmp_path / "storage"
    addons = ancestor / "addons"
    addons.mkdir(parents=True)
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    destination = addons / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    bridge = _load(monkeypatch, state_path=tmp_path / "state.json", addon_root=addons)
    state = bridge["_load_state"]()
    handler = object.__new__(bridge["Handler"])
    ancestor.chmod(0o111)
    try:
        identity, digest, _payload = _stage_deployment(bridge, handler, state, source)
        receipt = handler._deployment_commit(
            {"deployment_id": identity, "manifest_digest": digest}, state
        )
        assert receipt["state"] == "succeeded" and receipt["inventory_verified"]
        assert "new" in (destination / "addon.py").read_text()
        with bridge["_deployment_directory"](str(addons)) as descriptor:
            assert "plugin.video.fixture" in os.listdir(descriptor)
        addons.chmod(0o111)
        try:
            with (
                pytest.raises(PermissionError),
                bridge["_deployment_directory"](str(addons)),
            ):
                pytest.fail("Unreadable final directory accepted")
            with bridge["_deployment_directory"](str(addons), traverse_only=True):
                pass
        finally:
            addons.chmod(0o755)
    finally:
        ancestor.chmod(0o755)


@pytest.mark.skipif(not hasattr(os, "ST_NOEXEC"), reason="Requires mount flags")
def test_android_noexec_transfer_restart_and_inspection_keep_authenticated_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addons = tmp_path / "addons"
    addons.mkdir()
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    (source / "addon.py").chmod(0o755)
    destination = addons / "plugin.video.fixture"
    _write_video_plugin(destination, "1.0.0", "print('old')\n")
    state_path = tmp_path / "state.json"
    bridge = _load(monkeypatch, state_path=state_path, addon_root=addons)
    _android_mount(monkeypatch, bridge, addons)
    state = bridge["_load_state"]()
    secret = os.urandom(32)
    state.update(target_id=str(uuid4()), secret=b64url(secret))
    bridge["_save_state"](state)
    handler = object.__new__(bridge["Handler"])
    identity, digest, payload = _stage_deployment(bridge, handler, state, source)
    incoming_entries = cast(list[dict[str, Any]], payload["entries"])
    receipt = handler._deployment_commit(
        {"deployment_id": identity, "manifest_digest": digest}, state
    )
    assert receipt["state"] == "succeeded" and receipt["inventory_verified"]
    assert any(
        entry["path"] == "addon.py" and entry["mode"] == 0o755
        for entry in incoming_entries
    )
    baseline = bridge["_tree_manifest"](str(destination))[0]
    assert all(entry["mode"] == 0o644 for entry in baseline)

    restarted = _load(monkeypatch, state_path=state_path, addon_root=addons)
    _android_mount(monkeypatch, restarted, addons)
    handler = object.__new__(restarted["Handler"])
    observed: list[dict[str, Any]] = []
    handler._json_response = lambda _request, _hash, _secret, value: observed.append(
        value
    )
    handler._deployment_response(
        {
            "operation": "inspect_addon",
            "payload": {"addon_id": "plugin.video.fixture"},
            "target_id": state["target_id"],
            "bridge_id": state["bridge_id"],
        },
        "0" * 64,
        secret,
        restarted["_load_state"](),
    )
    assert observed[0]["manifest_digest"] == digest
    assert observed[0]["file_count"] == payload["file_count"]
    assert hashlib.sha256((destination / "addon.py").read_bytes()).hexdigest() == next(
        entry["sha256"] for entry in incoming_entries if entry["path"] == "addon.py"
    )


@pytest.mark.parametrize("android", [False, True])
def test_exec_capable_posix_mounts_verify_actual_executable_bits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, android: bool
) -> None:
    tree = tmp_path / "tree"
    _write_video_plugin(tree, "1.0.0", "pass\n")
    (tree / "addon.py").chmod(0o755)
    bridge = _load(monkeypatch)
    bridge["xbmc"].getCondVisibility = lambda _name: android
    proxy = ModuleType("exec_capable_os")
    proxy.__dict__.update(vars(os))
    proxy.__dict__["statvfs"] = lambda _path: SimpleNamespace(f_flag=0)
    monkeypatch.setitem(bridge["_tree_manifest"].__globals__, "os", proxy)
    entries = bridge["_tree_manifest"](str(tree))[0]
    expected = [{**entry, "mode": 0o644} for entry in entries]
    actual = bridge["_tree_manifest"](str(tree), expected)[0]
    assert (
        next(entry["mode"] for entry in actual if entry["path"] == "addon.py") == 0o755
    )

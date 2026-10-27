"""Resident launch derives commands from a verified manifest and journals once."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID, uuid4

import pytest

from kodi_lab.development.deployment import capture_development_tree
from kodi_lab.kodi.bridge import b64url
from tests.kodi.test_narrow_bridge_addon import _load


class _BridgeArguments(TypedDict):
    bridge_port: int
    target_id: UUID
    bridge_id: UUID
    secret: bytes


def setup_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extensions: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    root = tmp_path / "addons"
    addon = root / "plugin.video.launch"
    addon.mkdir(parents=True)
    (addon / "addon.xml").write_text(
        '<addon id="plugin.video.launch" version="1.0.0">' + extensions + "</addon>"
    )
    (addon / "main.py").write_text("pass\n")
    state_path = tmp_path / "private" / "state.json"
    state_path.parent.mkdir(mode=0o700)
    module = _load(monkeypatch, addon_root=root, state_path=state_path)
    secret = b"s" * 32
    state = {
        "bridge_id": str(uuid4()),
        "target_id": str(uuid4()),
        "secret": b64url(secret),
    }
    binding = {
        "target_id": state["target_id"],
        "container_id": "a" * 64,
        "runtime_revision_id": str(uuid4()),
        "generation": 1,
    }
    signature = hmac.new(
        secret, b"managed-launch-v1|" + module["_canonical"](binding), hashlib.sha256
    ).hexdigest()
    (state_path.parent / "managed-launch.json").write_text(
        json.dumps({"binding": binding, "signature": signature})
    )
    digest = module["_tree_manifest"](str(addon))[1]
    payload = {
        "launch_id": str(uuid4()),
        "addon_id": "plugin.video.launch",
        "addon_version": "1.0.0",
        "manifest_digest": digest,
        "ordinal": 0,
        "runtime_binding": binding,
    }
    calls: list[str] = []
    monkeypatch.setattr(module["xbmc"], "executebuiltin", calls.append)
    return module, state, payload, calls


@pytest.mark.parametrize(
    "mode", [0o644, 0o645, 0o654, 0o655, 0o744, 0o745, 0o754, 0o755]
)
def test_launch_uses_actual_source_digest_with_ancillary_execute_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension>",
    )
    root = tmp_path / "addons"
    extra = root / "plugin.video.launch/helper.sh"
    extra.write_text("#!/bin/sh\n")
    extra.chmod(mode)
    source = capture_development_tree(root, "plugin.video.launch")
    payload["manifest_digest"] = source.digest
    first = module["_managed_launch"](payload, state)
    assert first["receipt"]["state"] == "accepted"
    assert module["_managed_launch"](payload, state) == first
    assert calls == ["ActivateWindow(videos,plugin://plugin.video.launch/,return)"]


@pytest.mark.parametrize("drift", ["mode", "content", "generation", "runtime"])
def test_partial_execute_launch_retains_exact_tree_and_generation_fences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>',
    )
    root = tmp_path / "addons"
    main = root / "plugin.video.launch/main.py"
    main.chmod(0o744)
    payload["manifest_digest"] = capture_development_tree(
        root, "plugin.video.launch"
    ).digest
    if drift == "mode":
        main.chmod(0o754)
    elif drift == "content":
        main.write_text("changed\n")
    else:
        payload["runtime_binding"] = {
            **payload["runtime_binding"],
            **(
                {"generation": 2}
                if drift == "generation"
                else {"runtime_revision_id": str(uuid4())}
            ),
        }
    with pytest.raises(ValueError):
        module["_managed_launch"](payload, state)
    assert calls == []


@pytest.mark.parametrize("provides", ["video", " \nvideo\t"])
@pytest.mark.parametrize(
    ("ordinal", "expected"),
    [
        (0, "RunScript(special://home/addons/plugin.video.launch/main.py)"),
        (1, "ActivateWindow(videos,plugin://plugin.video.launch/,return)"),
    ],
)
def test_combined_manifest_selects_script_and_plugin_without_runaddon_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ordinal: int,
    expected: str,
    provides: str,
) -> None:
    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        f"<provides>{provides}</provides></extension>",
    )
    payload["ordinal"] = ordinal
    first = module["_managed_launch"](payload, state)
    assert first["receipt"]["state"] == "accepted"
    assert calls == [expected]
    assert module["_managed_launch"](payload, state) == first
    assert (
        module["_managed_launch"](
            {"launch_id": payload["launch_id"]}, state, status_only=True
        )
        == first
    )
    assert calls == [expected]


def test_missing_marker_or_rotated_secret_never_launches_external_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>',
    )
    state["secret"] = b64url(b"z" * 32)
    with pytest.raises(ValueError, match="marker"):
        module["_managed_launch"](payload, state)
    (tmp_path / "private/managed-launch.json").unlink()
    with pytest.raises(OSError):
        module["_managed_launch"](payload, state)
    assert calls == []


@pytest.mark.parametrize(
    "failure",
    [
        "undeclared",
        "drift",
        "command",
        "binding",
        "symlink",
        "unsupported",
        "injection",
    ],
)
def test_invalid_launch_is_rejected_before_any_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    extension = (
        '<extension point="xbmc.service" library="main.py"/>'
        if failure == "unsupported"
        else '<extension point="xbmc.python.script" library="main.py"/>'
    )
    if failure == "injection":
        extension = '<extension point="xbmc.python.script" library="main.py),Quit()"/>'
    module, state, payload, calls = setup_bridge(tmp_path, monkeypatch, extension)
    if failure == "undeclared":
        payload["ordinal"] = 9
    elif failure == "drift":
        (tmp_path / "addons/plugin.video.launch/main.py").write_text("changed\n")
    elif failure == "command":
        payload["command"] = "Quit()"
    elif failure == "binding":
        payload["runtime_binding"] = {
            **payload["runtime_binding"],
            "container_id": "b" * 64,
        }
    elif failure == "symlink":
        path = tmp_path / "addons/plugin.video.launch/main.py"
        path.unlink()
        path.symlink_to(tmp_path / "outside.py")
    with pytest.raises((OSError, ValueError)):
        module["_managed_launch"](payload, state)
    assert calls == []


def test_crash_after_builtin_retains_pending_barrier_and_never_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>',
    )
    original = module["_save_launch"]

    def fail(receipt: dict[str, Any]) -> None:
        if receipt["state"] == "accepted":
            raise OSError("fixture failed accepted receipt fsync")
        original(receipt)

    monkeypatch.setitem(module["_managed_launch"].__globals__, "_save_launch", fail)
    with pytest.raises(OSError):
        module["_managed_launch"](payload, state)
    result = module["_managed_launch"](payload, state)
    assert result["receipt"]["state"] == "pending" and len(calls) == 1
    assert (
        module["_managed_launch"]({"launch_id": str(uuid4())}, state, status_only=True)[
            "receipt"
        ]
        is None
    )
    assert len(calls) == 1


def test_authenticated_wire_launch_receipt_and_predispatch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import UUID

    from kodi_lab.domain import ExternalProtocolKind
    from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
    from kodi_lab.kodi.external import ValidatedExternalEndpoint
    from kodi_lab.kodi.launch import BridgeLaunchIntent
    from tests.kodi.test_bridge_otp_bootstrap import _server

    module, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>',
    )
    persisted = module["_state_default"]()
    persisted.update(state)
    module["_save_state"](persisted)
    intent = BridgeLaunchIntent.model_validate(payload)
    endpoint = ValidatedExternalEndpoint(
        ExternalProtocolKind.JSONRPC_HTTP, "127.0.0.1", 8080
    )
    client = ExternalBridgeClient()
    with _server(module) as port:
        kwargs: _BridgeArguments = dict(
            bridge_port=port,
            target_id=UUID(state["target_id"]),
            bridge_id=UUID(state["bridge_id"]),
            secret=b"s" * 32,
        )
        identity, receipt = client.managed_launch(endpoint, intent=intent, **kwargs)
        assert (
            identity.addon_version == "1.1.31"
            and receipt is not None
            and receipt.state == "accepted"
        )
        _, replay = client.managed_launch(
            endpoint, intent=intent, status_only=True, **kwargs
        )
        assert replay == receipt and len(calls) == 1
        invalid = intent.model_copy(update={"launch_id": uuid4(), "ordinal": 10})
        with pytest.raises(ExternalBridgeError, match="launch_request_rejected"):
            client.managed_launch(endpoint, intent=invalid, **kwargs)
        assert len(calls) == 1

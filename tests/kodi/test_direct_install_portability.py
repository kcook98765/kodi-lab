"""Direct installer portability, authenticated errors and containment regressions."""

from __future__ import annotations

import ast
import ctypes
import hashlib
import ntpath
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError, b64url
from tests.kodi.test_bridge_otp_bootstrap import _endpoint, _server
from tests.kodi.test_narrow_bridge_addon import (
    _load,
    _stage_deployment,
    _write_video_plugin,
)


@pytest.fixture
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    addons = tmp_path / "addons"
    addons.mkdir()
    source = tmp_path / "source"
    _write_video_plugin(source, "2.0.0", "print('new')\n")
    _write_video_plugin(addons / "plugin.video.fixture", "1.0.0", "print('old')\n")
    bridge = _load(monkeypatch, state_path=tmp_path / "state.json", addon_root=addons)
    state = bridge["_load_state"]()
    secret = os.urandom(32)
    state.update(target_id=str(uuid4()), secret=b64url(secret))
    bridge["_save_state"](state)
    handler = object.__new__(bridge["Handler"])
    return bridge, handler, state, source, addons, secret


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (
            AttributeError("secret payload must never appear"),
            "deployment_installation_failed",
        ),
        (
            OSError(13, "secret payload must never appear"),
            "deployment_installation_failed",
        ),
        (ValueError("secret payload must never appear"), "deployment_request_invalid"),
    ],
)
def test_authenticated_installer_failure_is_signed_and_pairing_still_works(
    installer: Any, monkeypatch: pytest.MonkeyPatch, exception: Exception, code: str
) -> None:
    bridge, _handler, state, _source, _addons, secret = installer
    messages: list[str] = []
    bridge["xbmc"].log = lambda message, _level: messages.append(message)

    def fail(*_args: Any) -> Any:
        raise exception

    monkeypatch.setattr(bridge["Handler"], "_deployment_begin", fail)
    client = ExternalBridgeClient()
    options: dict[str, Any] = dict(
        bridge_port=0,
        target_id=UUID(state["target_id"]),
        bridge_id=UUID(state["bridge_id"]),
        secret=secret,
    )
    with _server(bridge) as port:
        options["bridge_port"] = port
        with pytest.raises(ExternalBridgeError, match=code):
            client._authenticated_json(
                _endpoint(), **options, operation="deployment_begin", payload={}
            )
        assert client.identity(_endpoint(), **options).bridge_id == UUID(
            state["bridge_id"]
        )
        with pytest.raises(ExternalBridgeError, match="bridge_authentication_failed"):
            client._authenticated_json(
                _endpoint(),
                **{**options, "secret": b"wrong" * 7},
                operation="deployment_begin",
                payload={},
            )
    category = "OSError" if isinstance(exception, OSError) else type(exception).__name__
    assert any("exception=" + category in line for line in messages)
    assert "secret payload" not in "\n".join(messages)
    assert b64url(secret) not in "\n".join(messages)
    assert ExternalBridgeService._reachability(code).value == "reachable"


def test_forged_installer_error_is_still_authentication_failure(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, _handler, state, _source, _addons, secret = installer
    client = ExternalBridgeClient()
    original = client._post_json

    def forge(*args: Any) -> Any:
        value = original(*args)
        assert isinstance(value, dict)
        value["status"] = "deployment_installation_failed"
        return value

    monkeypatch.setattr(client, "_post_json", forge)
    with (
        _server(bridge) as port,
        pytest.raises(ExternalBridgeError, match="bridge_authentication_failed"),
    ):
        client._authenticated_json(
            _endpoint(),
            bridge_port=port,
            target_id=UUID(state["target_id"]),
            bridge_id=UUID(state["bridge_id"]),
            secret=secret,
            operation="bridge_identity",
        )


def test_failed_begin_cleans_stage_and_allows_a_fresh_operation(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, handler, state, source, addons, _secret = installer
    globals_ = handler._deployment_begin.__func__.__globals__
    original = globals_["_deployment_file"]
    calls = 0

    @contextmanager
    def fail_second(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        if len(args) > 1 and args[1] == "xb":
            calls += 1
            if calls == 2:
                raise OSError(28, "disk full")
        with original(*args, **kwargs) as stream:
            yield stream

    with monkeypatch.context() as patch:
        patch.setitem(globals_, "_deployment_file", fail_second)
        with pytest.raises(OSError):
            _stage_deployment(bridge, handler, state, source)
    assert not list(addons.glob(".kodi-lab-stage-*"))
    assert not state["deployments"]
    assert "old" in (addons / "plugin.video.fixture/addon.py").read_text()
    identity, digest, _payload = _stage_deployment(bridge, handler, state, source)
    receipt = handler._deployment_commit(
        {"deployment_id": identity, "manifest_digest": digest}, state
    )
    assert receipt["state"] == "succeeded" and receipt["inventory_verified"]


def test_incomplete_transfer_cannot_produce_successful_receipt(installer: Any) -> None:
    bridge, handler, state, source, addons, _secret = installer
    identity, digest, _payload = _stage_deployment(
        bridge, handler, state, source, transfer=False
    )
    with pytest.raises(ValueError, match="transfer incomplete"):
        handler._deployment_commit(
            {"deployment_id": identity, "manifest_digest": digest}, state
        )
    receipt = bridge["_receipt"](
        bridge["_load_operation"](identity), state["bridge_id"]
    )
    assert receipt["state"] == "staging"
    assert not receipt["inventory_verified"] and receipt["receipt_id"] is None
    assert "old" in (addons / "plugin.video.fixture/addon.py").read_text()


@pytest.mark.parametrize("kind", ["file_link", "hard_link", "parent_link"])
def test_chunk_cannot_write_through_a_link(
    installer: Any, tmp_path: Path, kind: str
) -> None:
    bridge, handler, state, source, _addons, _secret = installer
    identity, _digest, payload = _stage_deployment(
        bridge, handler, state, source, transfer=False
    )
    stage = Path(bridge["_stage_path"](identity))
    target = stage / "addon.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"untouched")
    target.unlink()
    if kind == "file_link":
        target.symlink_to(outside)
    elif kind == "hard_link":
        os.link(outside, target)
    else:
        moved = stage.with_name(stage.name + "-moved")
        stage.rename(moved)
        stage.symlink_to(moved, target_is_directory=True)
    entry = next(
        item
        for item in cast(list[dict[str, Any]], payload["entries"])
        if item["path"] == "addon.py"
    )
    content = (source / "addon.py").read_bytes()
    with pytest.raises((ValueError, OSError)):
        handler._deployment_chunk(
            {
                "deployment_id": identity,
                "path": "addon.py",
                "offset": 0,
                "file_digest": entry["sha256"],
                "content": b64url(content),
                "content_digest": hashlib.sha256(content).hexdigest(),
            },
            state,
        )
    assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize(
    "name",
    [
        "C:escape",
        "file:stream",
        "NUL.txt",
        "con",
        "COM¹.py",
        "name.",
        "name ",
        "bad?name",
    ],
)
def test_windows_names_are_rejected(installer: Any, name: str) -> None:
    bridge = installer[0]
    assert bridge["_windows_component"](name) is False


@pytest.mark.parametrize("info", [None, (0x400, 1), (0x10, 1), (0, 2), (0, 0)])
def test_windows_handle_validation_fails_closed_and_closes(
    installer: Any, monkeypatch: pytest.MonkeyPatch, info: Any
) -> None:
    bridge, handler, *_rest = installer
    calls: list[tuple[Any, ...]] = []

    class Function:
        def __init__(self, result: Any) -> None:
            self.result = result

        def __call__(self, *args: Any) -> Any:
            calls.append(args)
            return self.result

    kernel = SimpleNamespace(CreateFileW=Function(123), CloseHandle=Function(True))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kw: kernel, raising=False)
    globals_ = handler._deployment_begin.__func__.__globals__
    monkeypatch.setitem(globals_, "_windows_handle_info", lambda *_args, **_kw: info)
    with (
        pytest.raises(ValueError, match="unsafe deployment handle"),
        bridge["_windows_deployment_handle"]("C:\\fixture"),
    ):
        pytest.fail("unsafe handle accepted")
    assert calls[0][2] == 1  # No write or delete sharing for files.
    assert calls[0][5] & 0x00200000  # OPEN_REPARSE_POINT.
    assert calls[-1] == (123,)


def test_directory_flush_never_looks_up_posix_flags_on_windows(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, handler, *_rest = installer
    globals_ = handler._deployment_begin.__func__.__globals__
    monkeypatch.setitem(globals_, "os", SimpleNamespace(name="nt"))
    bridge["_fsync_directory"]("C:\\fixture")


def test_windows_ancestor_guards_do_not_require_posix_constants(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, handler, *_rest = installer
    globals_ = handler._deployment_begin.__func__.__globals__
    held: list[str] = []

    @contextmanager
    def native_handle(path: str, **options: Any) -> Any:
        assert options == {"directory": True}
        held.append(path)
        yield 123
        assert held.pop() == path

    monkeypatch.setitem(
        globals_, "os", SimpleNamespace(name="nt", path=ntpath, sep="\\")
    )
    monkeypatch.setitem(globals_, "_windows_deployment_handle", native_handle)
    with bridge["_deployment_directory"]("C:\\root\\addons"):
        assert held == ["C:\\", "C:\\root", "C:\\root\\addons"]
    assert held == []


def test_windows_manifest_rejects_case_aliases(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, handler, *_rest = installer
    entries = [
        {"path": name, "type": "file", "mode": 0o644, "size": 1, "sha256": "0" * 64}
        for name in ("File.py", "file.py")
    ]
    payload = {
        "entries": entries,
        "file_count": 2,
        "total_bytes": 2,
        "manifest_version": 1,
        "manifest_digest": "sha256:"
        + hashlib.sha256(bridge["_manifest_bytes"](entries)).hexdigest(),
    }
    globals_ = handler._deployment_begin.__func__.__globals__
    monkeypatch.setitem(globals_, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ValueError, match="Windows path alias"):
        bridge["_validate_manifest"](payload)
    for path in (".GIT/config", "__PYCACHE__/cache.py", "bad.PYC", "nul"):
        with pytest.raises(ValueError):
            bridge["_relative_path"](path) if path != "nul" else bridge["_addon_id"](
                path
            )


def test_cleanup_failure_is_explicit_and_never_publishes_success(
    installer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, handler, state, source, addons, _secret = installer
    globals_ = handler._deployment_begin.__func__.__globals__

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("injected failure")

    entries, digest, files, total = bridge["_tree_manifest"](str(source))
    monkeypatch.setitem(globals_, "_deployment_file", fail)
    monkeypatch.setitem(globals_, "_remove_deployment_tree", fail)
    with pytest.raises(bridge["OperationError"], match="deployment_cleanup_failed"):
        handler._deployment_begin(
            {
                "addon_id": "plugin.video.fixture",
                "addon_version": "2.0.0",
                "deployment_id": str(uuid4()),
                "deployment_request_id": str(uuid4()),
                "entries": entries,
                "file_count": files,
                "generation": 6,
                "manifest_digest": digest,
                "manifest_version": 1,
                "total_bytes": total,
            },
            state,
        )
    assert state["deployments"] == {}
    assert "old" in (addons / "plugin.video.fixture/addon.py").read_text()


def test_transfer_is_not_reused_by_a_new_pairing(installer: Any) -> None:
    bridge, handler, state, source, _addons, _secret = installer
    identity, digest, _payload = _stage_deployment(bridge, handler, state, source)
    state["target_id"] = str(uuid4())
    with pytest.raises(ValueError, match="deployment pairing changed"):
        handler._deployment_commit(
            {"deployment_id": identity, "manifest_digest": digest}, state
        )
    assert bridge["_load_operation"](identity)["state"] == "ready"


@pytest.mark.parametrize("phase", ["old", "new"])
def test_error_after_native_rename_stays_ambiguous(
    installer: Any, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bridge, handler, state, source, addons, _secret = installer
    identity, digest, _payload = _stage_deployment(bridge, handler, state, source)
    globals_ = handler._deployment_commit.__func__.__globals__
    original = globals_["_deployment_replace"]
    selected = (
        str(addons / "plugin.video.fixture")
        if phase == "old"
        else bridge["_stage_path"](identity)
    )

    def rename_then_fail(source_path: str, destination_path: str) -> None:
        original(source_path, destination_path)
        if source_path == selected:
            raise OSError("injected post-rename handle cleanup failure")

    monkeypatch.setitem(globals_, "_deployment_replace", rename_then_fail)
    receipt = handler._deployment_commit(
        {"deployment_id": identity, "manifest_digest": digest}, state
    )
    assert receipt["state"] == "ambiguous"
    assert receipt["classification"] == "rollback_failed_reconciliation_required"
    assert receipt["receipt_id"] is None and not receipt["inventory_verified"]
    assert "old" in (Path(bridge["_backup_path"](identity)) / "addon.py").read_text()


@pytest.mark.parametrize(
    "code",
    [
        "bridge_unreachable",
        "deployment_installation_failed",
        "bridge_authentication_failed",
    ],
)
def test_only_transport_loss_is_automatically_retried(code: str) -> None:
    service = object.__new__(ExternalBridgeService)
    count = 0
    terminal: Any = object()

    def chunk(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal count
        count += 1
        if count == 1:
            raise ExternalBridgeError(code)
        return terminal

    service._client = cast(Any, SimpleNamespace(deployment_chunk=chunk))
    snapshot: Any = SimpleNamespace(
        endpoint=_endpoint(), bridge_port=8765, target_id=uuid4(), bridge_id=uuid4()
    )
    if code == "bridge_unreachable":
        assert (
            service._transfer_chunk_with_retry(
                snapshot, b"key", uuid4(), "addon.py", "0" * 64, 0, b"x"
            )
            is terminal
        )
        assert count == 2
    else:
        with pytest.raises(ExternalBridgeError, match=code):
            service._transfer_chunk_with_retry(
                snapshot, b"key", uuid4(), "addon.py", "0" * 64, 0, b"x"
            )
        assert count == 1


def test_standalone_native_matrix_is_runnable_without_lab_dependencies() -> None:
    script = Path(__file__).parents[2] / "scripts/check-direct-install.py"
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_distributed_python_and_native_runner_keep_python38_syntax() -> None:
    root = Path(__file__).parents[2]
    paths = list((root / "bridge/service.kodi_lab_bridge").glob("*.py"))
    paths.append(root / "scripts/check-direct-install.py")
    for path in paths:
        ast.parse(path.read_text(), filename=str(path), feature_version=(3, 8))

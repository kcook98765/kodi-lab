"""Real Bridge handler/client log reads, limits, rotation and filesystem confinement."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError, b64url
from kodi_lab.kodi.logs import KodiLogRequest, KodiLogResult
from tests.kodi.test_bridge_otp_bootstrap import _endpoint, _server
from tests.kodi.test_narrow_bridge_addon import _load


@pytest.fixture
def logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], Path]:
    bridge = _load(monkeypatch, state_path=tmp_path / "state/state.json")
    root = tmp_path / "logs"
    root.mkdir()
    vfs = sys.modules["xbmcvfs"]
    translate = vfs.translatePath
    monkeypatch.setattr(
        vfs,
        "translatePath",
        lambda p: str(root) if p == "special://logpath" else translate(p),
    )
    return bridge, root


def read(logs: tuple[dict[str, Any], Path], **kwargs: Any) -> KodiLogResult:
    request = KodiLogRequest(**kwargs)
    result = KodiLogResult.model_validate(logs[0]["_read_log"](request.model_dump()))
    result.require_request(request)
    return result


def test_stats_and_large_sparse_tail_do_not_read_entire_file(logs: Any) -> None:
    _, root = logs
    path = root / "kodi.log"
    with path.open("wb") as stream:
        stream.seek(10 * 1024**3)
        stream.write(b"\nlast one\nlast two\n")
    stats = read(logs)
    assert stats.size_bytes > 10 * 1024**3
    assert stats.content == "" and stats.bytes_read == 0 and not stats.truncated
    result = read(logs, mode="tail", max_lines=2)
    assert result.content == "last one\nlast two\n"
    assert result.bytes_read == 18 and result.truncated
    assert result.snapshot_id == stats.snapshot_id
    assert result.location == "special://logpath/kodi.log"


def test_both_logs_utf8_byte_and_line_limits_and_paging(logs: Any) -> None:
    _, root = logs
    (root / "kodi.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "kodi.old.log").write_text("previous crash\n", encoding="utf-8")
    assert read(logs, log="previous", mode="tail").content == "previous crash\n"
    tail = read(logs, mode="tail", max_lines=1)
    assert tail.content == "three\n" and tail.lines_returned == 1
    older = read(
        logs, mode="tail", before=tail.start_byte, snapshot_id=tail.snapshot_id
    )
    assert older.content == "one\ntwo\n" and older.end_byte == tail.start_byte
    assert older.truncated and not older.partial_first_line
    assert read(logs, mode="tail", max_bytes=4).content == "ree\n"
    assert read(logs, mode="tail", max_bytes=4).partial_first_line
    (root / "kodi.log").write_bytes(b"x" * 20000)
    fragment = read(logs, mode="tail", max_bytes=32)
    assert fragment.bytes_read == 32 and fragment.partial_first_line
    (root / "kodi.log").write_bytes(b"\xff" * 8192)
    assert len(read(logs, mode="tail").content) == 8192


@pytest.mark.parametrize("kind", ["append", "truncate", "rotate"])
def test_snapshot_detects_append_truncation_and_restart_rotation(
    logs: Any, kind: str
) -> None:
    _, root = logs
    path = root / "kodi.log"
    path.write_text("before\n", encoding="utf-8")
    stats = read(logs)
    if kind == "rotate":
        path.rename(root / "kodi.old.log")
        path.write_text("after restart\n", encoding="utf-8")
        assert read(logs, log="previous", mode="tail").content == "before\n"
    else:
        with path.open("a" if kind == "append" else "w", encoding="utf-8") as stream:
            stream.write("new\n")
    with pytest.raises(Exception, match=r"^log_changed$"):
        read(logs, mode="tail", snapshot_id=stats.snapshot_id)
    assert read(logs).snapshot_id != stats.snapshot_id


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_reader_rejects_non_regular_and_linked_files(
    logs: Any, tmp_path: Path, kind: str
) -> None:
    _, root = logs
    path = root / "kodi.log"
    secret = tmp_path / "secret"
    secret.write_text("never disclose", encoding="utf-8")
    if kind == "symlink":
        path.symlink_to(secret)
    elif kind == "hardlink":
        os.link(secret, path)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(Exception, match=r"^log_unavailable$"):
        read(logs, mode="tail")


def test_reader_rejects_symlinked_ancestors(logs: Any) -> None:
    _, root = logs
    real = root.with_name("real")
    root.rename(real)
    root.symlink_to(real, target_is_directory=True)
    with pytest.raises(Exception, match=r"^log_unavailable$"):
        read(logs)


def test_rotation_during_read_never_publishes_stale_content(
    logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, root = logs
    path = root / "kodi.log"
    path.write_text("old\n", encoding="utf-8")
    original = bridge["_log_snapshot"]
    calls: list[bool] = []

    def rotating(metadata: Any) -> str:
        result = str(original(metadata))
        if not calls:
            calls.append(True)
            path.rename(root / "kodi.old.log")
            path.write_text("new\n", encoding="utf-8")
        return result

    monkeypatch.setitem(bridge["_read_log"].__globals__, "_log_snapshot", rotating)
    with pytest.raises(Exception, match=r"^log_changed$"):
        read(logs, mode="tail")


@pytest.mark.parametrize(
    "change",
    [
        {"log": "../../secret"},
        {"path": "/etc/passwd"},
        {"url": "http://example.test"},
        {"max_bytes": 8193},
        {"max_lines": 501},
        {"max_lines": True},
        {"before": 1},
        {"mode": "shell"},
        {"snapshot_id": "x" * 1000},
    ],
)
def test_closed_payload_rejected_at_client_and_bridge(
    logs: Any, change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        KodiLogRequest.model_validate({**KodiLogRequest().model_dump(), **change})
    with pytest.raises(Exception, match=r"^log_request_invalid$"):
        logs[0]["_read_log"]({**KodiLogRequest().model_dump(), **change})


def test_authenticated_handler_client_errors_and_no_content_persistence(
    logs: Any,
) -> None:
    bridge, root = logs
    marker = "sensitive-log-test-marker"
    (root / "kodi.log").write_text(marker, encoding="utf-8")
    (root / "kodi.old.log").write_text("previous", encoding="utf-8")
    state = bridge["_load_state"]()
    target, secret = uuid4(), os.urandom(32)
    state.update(target_id=str(target), secret=b64url(secret))
    bridge["_save_state"](state)
    with _server(bridge) as port:
        client = ExternalBridgeClient()
        kwargs: dict[str, Any] = dict(
            bridge_port=port,
            target_id=target,
            bridge_id=UUID(state["bridge_id"]),
            secret=secret,
        )
        for log in ("current", "previous"):
            identity, result = client.read_log(
                _endpoint(), **kwargs, request=KodiLogRequest(log=log, mode="tail")
            )
            assert "log_read" in identity.capabilities and result.content
        with pytest.raises(ExternalBridgeError, match="bridge_authentication_failed"):
            client.read_log(
                _endpoint(), **{**kwargs, "secret": b"z" * 32}, request=KodiLogRequest()
            )
        (root / "kodi.old.log").unlink()
        with pytest.raises(ExternalBridgeError, match=r"^log_missing$"):
            client.read_log(
                _endpoint(), **kwargs, request=KodiLogRequest(log="previous")
            )
    assert marker not in json.dumps(bridge["_load_state"]())
    assert marker not in str(bridge["_RESPONSES"])


def test_empty_log_and_cursor_out_of_bounds(logs: Any) -> None:
    (logs[1] / "kodi.log").touch()
    result = read(logs, mode="tail")
    assert result.content == "" and result.lines_returned == 0 and not result.truncated
    with pytest.raises(Exception, match=r"^log_request_invalid$"):
        read(logs, mode="tail", before=1, snapshot_id=result.snapshot_id)


@pytest.mark.parametrize(
    "change",
    [
        {"bytes_read": 8193},
        {"content": "x" * 8192},
        {"log": "previous"},
        {"location": "file:///etc/passwd"},
        {"snapshot_id": "bad"},
        {"end_byte": 0},
        {"truncated": False},
    ],
)
def test_client_rejects_contradictory_signed_results(
    logs: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    bridge, root = logs
    (root / "kodi.log").write_text("0123456789\n", encoding="utf-8")
    request = KodiLogRequest(mode="tail", max_bytes=4)
    result = {**bridge["_read_log"](request.model_dump()), **change}
    client = ExternalBridgeClient()
    monkeypatch.setattr(
        client,
        "_authenticated_json",
        lambda *a, **k: {
            "identity": bridge["_identity"]({"bridge_id": str(uuid4())}),
            "log": result,
        },
    )
    with pytest.raises(ExternalBridgeError, match="bridge_operation_failed"):
        client.read_log(
            _endpoint(),
            bridge_port=8765,
            target_id=uuid4(),
            bridge_id=uuid4(),
            secret=b"x" * 32,
            request=request,
        )


def test_log_reads_have_bounded_admission(logs: Any) -> None:
    bridge, _ = logs
    assert bridge["_LOG_LOCK"].acquire(blocking=False)
    try:
        with pytest.raises(Exception, match=r"^log_read_busy$"):
            read(logs)
    finally:
        bridge["_LOG_LOCK"].release()

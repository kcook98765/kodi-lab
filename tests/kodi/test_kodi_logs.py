"""Real Bridge handler/client log reads, limits, rotation and filesystem confinement."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError, b64url
from kodi_lab.kodi.logs import (
    KodiLogRequest,
    KodiLogResult,
    KodiLogScanBridgeRequest,
    KodiLogScanRequest,
    KodiLogScanResult,
)
from tests.kodi.test_bridge_otp_bootstrap import _endpoint, _server
from tests.kodi.test_narrow_bridge_addon import _load


@pytest.fixture
def logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[dict[str, Any], Path]]:
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
    try:
        yield bridge, root
    finally:
        bridge["_LOG_SCANS"].shutdown()


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


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Requires path-only handles")
@pytest.mark.parametrize("log", ["current", "previous"])
def test_logs_and_deployment_allow_traverse_only_ancestors(logs: Any, log: str) -> None:
    bridge, root = logs
    filename = "kodi.log" if log == "current" else "kodi.old.log"
    (root / filename).write_text("bounded log line\n", encoding="utf-8")
    ancestor = root.parent
    permissions = ancestor.stat().st_mode & 0o777
    ancestor.chmod(0o111)
    try:
        stats = read(logs, log=log)
        assert stats.bytes_read == 0 and stats.content == ""
        assert (
            read(logs, log=log, mode="tail", max_lines=1).content
            == "bounded log line\n"
        )
        with bridge["_deployment_directory"](str(root)) as descriptor:
            assert os.listdir(descriptor) == [filename]
    finally:
        ancestor.chmod(permissions)


def test_log_reader_without_path_only_handles(
    logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (logs[1] / "kodi.log").write_text("portable fallback\n", encoding="utf-8")
    monkeypatch.delattr(os, "O_PATH", raising=False)
    assert read(logs, mode="tail").content == "portable fallback\n"


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


def test_authenticated_scan_handler_and_no_sensitive_replay_storage(logs: Any) -> None:
    bridge, root = logs
    marker = "private-scan-content"
    (root / "kodi.log").write_text(f"ERROR: {marker}\n")
    (root / "kodi.old.log").touch()
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
            request=KodiLogScanBridgeRequest(client_binding="c" * 64),
        )
        identity, result = client.read_log(_endpoint(), **kwargs)
        assert "log_scan" in identity.capabilities
        assert result.counts_exact and result.files[0].matched_records == 1
        assert marker in result.matches[0].content
        with pytest.raises(ExternalBridgeError, match="bridge_authentication_failed"):
            client.read_log(_endpoint(), **{**kwargs, "secret": b"z" * 32})
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


def scan(fixture_logs: Any, **query: Any) -> KodiLogScanResult:
    request = KodiLogScanBridgeRequest(client_binding="c" * 64, **query)
    bridge = fixture_logs[0]
    result = KodiLogScanResult.model_validate(
        bridge["_LOG_SCANS"].scan(
            request.model_dump(),
            ("target", "bridge", "secret"),
            bridge["_open_scan_log"],
        )
    )
    result.require_request(request)
    return result


def finish_scan(fixture_logs: Any, **query: Any) -> list[KodiLogScanResult]:
    pages = [scan(fixture_logs, **query)]
    while pages[-1].cursor:
        assert len(pages) < 200
        pages.append(scan(fixture_logs, **query, cursor=pages[-1].cursor))
    return pages


def record(
    level: str, text: str = "example", stamp: str = "2026-11-15 12:00:00.000"
) -> str:
    return f"{stamp} T:42 {level} <general>: {text}\n"


def test_scan_72_mib_exact_counts_and_bounded_work(logs: Any) -> None:
    bridge, root = logs
    # Reuse a small generated block; neither fixture creation nor scan holds 72 MiB.
    line = record("info", "x" * 450).encode()
    block = line * 1024
    for name in ("kodi.log", "kodi.old.log"):
        with (root / name).open("wb") as stream:
            stream.write(record("error", "begin").encode())
            for _ in range(38):
                stream.write(block)
            stream.write(record("fatal", "middle").encode())
            for _ in range(38):
                stream.write(block)
            stream.write(record("ERROR", "end").encode())
    size = sum((root / name).stat().st_size for name in ("kodi.log", "kodi.old.log"))
    assert 72 * 1024**2 <= size < 80 * 1024**2
    started = time.perf_counter()
    pages = finish_scan(logs, max_matches=4, context_records=1)
    elapsed = time.perf_counter() - started
    final = pages[-1]
    assert final.complete and final.counts_exact and final.excerpts_truncated
    assert [f.matched_records for f in final.files] == [3, 3]
    assert [f.severity_counts for f in final.files] == [{"ERROR": 2, "FATAL": 1}] * 2
    assert all(f.scanned_bytes == f.boundary_bytes for f in final.files)
    assert len(final.matches) == 4
    assert all(len(p.model_dump_json().encode()) <= 32768 for p in pages)
    assert all(p.bytes_read <= bridge["_LOG_SCANS"].byte_limit for p in pages)
    assert len(pages) <= 32
    print(
        f"large-log scan: {size} bytes, {len(pages)} calls, {elapsed:.3f}s, "
        f"max response {max(len(p.model_dump_json().encode()) for p in pages)} bytes"
    )


@pytest.mark.parametrize("matches", [False, True])
def test_scan_zero_matches_density_and_continuation(logs: Any, matches: bool) -> None:
    bridge, root = logs
    bridge["_LOG_SCANS"].byte_limit = 113
    (root / "kodi.log").write_text(record("ERROR" if matches else "INFO") * 100)
    pages = finish_scan(logs, logs="current", max_matches=2)
    assert len(pages) > 1 and pages[0].cursor and not pages[0].counts_exact
    assert pages[-1].files[0].matched_records == (100 if matches else 0)
    assert len(pages[-1].matches) == (2 if matches else 0)
    assert pages[-1].excerpts_truncated is matches
    assert pages[-1].files[0].records_scanned == 100


def test_scan_serialized_response_budget_with_escaped_dense_records(logs: Any) -> None:
    _, root = logs
    (root / "kodi.log").write_text(record("ERROR", "\x00" * 2000) * 30)
    page = finish_scan(logs, logs="current", max_matches=20, context_records=2)[-1]
    assert page.counts_exact and page.files[0].matched_records == 30
    assert page.excerpts_truncated and len(page.matches) < 20
    assert len(json.dumps(page.model_dump(), separators=(",", ":")).encode()) <= 32768


@pytest.mark.parametrize("change", ["append", "replace", "truncate"])
def test_scan_change_during_bounded_file_read(
    logs: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    bridge, root = logs
    path = root / "kodi.log"
    original = record("ERROR") * 3000
    path.write_text(original)
    opener = bridge["_open_scan_log"]
    changed = False

    @contextmanager
    def changing(log: str) -> Any:
        with opener(log) as (stream, observe):

            class Reader:
                def fileno(self) -> int:
                    return int(stream.fileno())

                def seek(self, offset: int) -> Any:
                    return stream.seek(offset)

                def read(self, limit: int) -> bytes:
                    nonlocal changed
                    assert 0 < limit <= 65536
                    raw = stream.read(limit)
                    if not changed:
                        changed = True
                        if change == "append":
                            with path.open("a") as writer:
                                writer.write(record("ERROR", "excluded"))
                        elif change == "truncate":
                            path.write_text("short\n")
                        else:
                            path.rename(root / "replaced")
                            path.write_text(original)
                    return bytes(raw)

            yield Reader(), observe

    monkeypatch.setitem(bridge, "_open_scan_log", changing)
    page = finish_scan(logs, logs="current")[-1]
    if change == "append":
        assert page.counts_exact and page.files[0].matched_records == 3000
        assert page.files[0].append_observed
    else:
        assert page.reason == "file_changed" and not page.counts_exact


def test_scan_utf8_oversized_multiline_literal_and_context(logs: Any) -> None:
    bridge, root = logs
    bridge["_LOG_SCANS"].byte_limit = 1031
    text = record("INFO", "before") + record("ERROR", "traceback")
    text += (
        "   Traceback (most recent call last):\n"
        + "ö" * 10000
        + "plugin.video.example\n"
    )
    text += "   RuntimeError: continuation\n" + record("INFO", "after")
    (root / "kodi.log").write_text(text, encoding="utf-8")
    final = finish_scan(
        logs, logs="current", text="plugin.video.example", context_records=1
    )[-1]
    assert final.files[0].matched_records == 1 and final.files[0].records_scanned == 3
    match = final.matches[0]
    assert match.content_truncated and "Traceback" in match.content
    assert "before" in match.before[0] and "after" in match.after[0]
    assert match.end_byte == len(text[: text.index(record("INFO", "after"))].encode())
    assert not finish_scan(logs, logs="current", text="Plugin.video.example")[
        -1
    ].matches


def test_scan_timestamp_semantics_and_unsorted_records(logs: Any) -> None:
    _, root = logs
    text = record("ERROR", "late", "2026-11-15 13:00:00.000")
    text += record("WARNING", "inside", "2026-11-15 12:30:00.000")
    text += record("FATAL", "invalid", "2026-19-15 12:00:00.000")
    text += "ERROR: missing date\ntraceback\n"
    text += record("ERROR", "earlier", "2026-11-15 12:00:00.000")
    text += "12:00:00.000 T:42 ERROR: legacy time only\n"
    text += record("ERROR", "ISO separator", "2026-11-15T12:00:00.000")
    (root / "kodi.log").write_text(text)
    result = finish_scan(
        logs,
        logs="current",
        severities=["ERROR", "FATAL", "WARNING"],
        start_time="2026-11-15T12:00:00",
        end_time="2026-11-15T12:30:00",
    )[-1]
    assert result.files[0].matched_records == 3
    assert result.files[0].records_scanned == 7
    assert result.files[0].unknown_timestamp_records == 3
    assert [m.severity for m in result.matches] == ["WARNING", "ERROR", "ERROR"]


@pytest.mark.parametrize(
    "change", ["append", "truncate", "replace", "rotate", "rewrite", "rewrite_append"]
)
def test_scan_file_continuity(logs: Any, change: str) -> None:
    bridge, root = logs
    bridge["_LOG_SCANS"].byte_limit = 97
    path = root / "kodi.log"
    original = record("ERROR", "old") * 10
    path.write_text(original)
    first = scan(logs, logs="current")
    assert first.cursor
    if change == "append":
        with path.open("a") as stream:
            stream.write(record("ERROR", "excluded new error"))
    elif change == "truncate":
        path.write_text("short\n")
    elif change in ("replace", "rotate"):
        path.rename(root / ("kodi.old.log" if change == "rotate" else "replaced.log"))
        path.write_text(original)
    else:
        with path.open("r+b") as stream:
            stream.write(record("INFO ", "old").encode())
            if change == "rewrite_append":
                stream.seek(0, 2)
                stream.write(record("ERROR", "new").encode())
    pages = [scan(logs, logs="current", cursor=first.cursor)]
    while pages[-1].cursor:
        assert len(pages) < 100
        pages.append(scan(logs, logs="current", cursor=pages[-1].cursor))
    final = pages[-1]
    if change == "append":
        assert final.complete and final.files[0].matched_records == 10
        assert final.files[0].verified_bytes == len(original.encode())
        assert final.files[0].append_observed
        assert any(p.files[0].state == "verifying" for p in pages)
    else:
        assert not final.complete and not final.counts_exact
        assert final.reason == "file_changed" and final.files[0].state == "changed"


def test_scan_identity_anchor_prevents_inode_reuse_after_unlink(logs: Any) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    store.byte_limit = 113
    path = root / "kodi.log"
    original = record("ERROR") * 100
    path.write_text(original)
    page = scan(logs, logs="current")
    work = store.states[page.cursor].work[0]
    anchor = work["anchor"]
    identity = (os.fstat(anchor).st_dev, os.fstat(anchor).st_ino)
    assert not os.get_inheritable(anchor)
    path.unlink()
    path.write_text(original + record("ERROR", "replacement"))
    assert (path.stat().st_dev, path.stat().st_ino) != identity
    page = scan(logs, logs="current", cursor=page.cursor)
    assert page.reason == "file_changed" and not page.counts_exact
    assert "anchor" not in work
    with pytest.raises(OSError):
        os.fstat(anchor)


def test_scan_idle_expiry_and_shutdown_release_anchors(logs: Any) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    store.byte_limit = 3
    (root / "kodi.log").write_text(record("ERROR"))
    page = scan(logs, logs="current")
    active = store.states[page.cursor]
    anchor = active.work[0]["anchor"]
    active.created -= store.ttl
    # Exercise actual idle server maintenance, without another scan request.
    with _server(bridge):
        deadline = time.monotonic() + 3
        while store.states and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not store.states
        assert "parser" not in active.work[0]
        with pytest.raises(OSError):
            os.fstat(anchor)
    assert store.closed


def test_scan_shutdown_during_work_disposes_cursor_and_anchor(logs: Any) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    store.byte_limit = 3
    (root / "kodi.log").write_text(record("ERROR"))
    page = scan(logs, logs="current")
    anchor = store.states[page.cursor].work[0]["anchor"]
    request = KodiLogScanBridgeRequest(
        logs="current", cursor=page.cursor, client_binding="c" * 64
    )

    def abort() -> bool:
        store.shutdown()  # Nonblocking while the scan owns its lock.
        return True

    result = store.scan(
        request.model_dump(),
        ("target", "bridge", "secret"),
        bridge["_open_scan_log"],
        abort,
    )
    assert result["reason"] == "aborted"
    assert not store.states
    with pytest.raises(OSError):
        os.fstat(anchor)


@pytest.mark.parametrize("operation", ["scan", "prune"])
def test_scan_shutdown_at_lock_release_does_not_lose_cleanup(
    logs: Any,
    operation: str,
) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    store.byte_limit = 3
    (root / "kodi.log").write_text(record("ERROR"))
    page = scan(logs, logs="current")
    anchor = store.states[page.cursor].work[0]["anchor"]
    lock = store.lock
    at_release, resume = threading.Event(), threading.Event()
    errors = []

    class ReleaseGate:
        used = False

        def acquire(self, blocking: bool = True) -> bool:
            return bool(lock.acquire(blocking=blocking))

        def release(self) -> None:
            if threading.current_thread() is worker and not self.used:
                self.used = True
                at_release.set()
                resumed = resume.wait(3)
                lock.release()
                assert resumed
            else:
                lock.release()

    def work() -> None:
        try:
            if operation == "scan":
                scan(logs, logs="current", cursor=page.cursor)
            else:
                store.prune()
        except BaseException as error:
            errors.append(error)

    store.lock = ReleaseGate()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        assert at_release.wait(3)
        store.shutdown()
    finally:
        resume.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert not store.states
    with pytest.raises(OSError):
        os.fstat(anchor)


def test_scan_append_during_verification_fails_closed(logs: Any) -> None:
    bridge, root = logs
    bridge["_LOG_SCANS"].byte_limit = 97
    path = root / "kodi.log"
    path.write_text(record("ERROR") * 5)
    page = scan(logs, logs="current")
    with path.open("a") as stream:
        stream.write("append\n")
    while page.files[0].state != "verifying":
        page = scan(logs, logs="current", cursor=page.cursor)
        assert page.cursor
    with path.open("a") as stream:
        stream.write("another append\n")
    page = scan(logs, logs="current", cursor=page.cursor)
    assert page.reason == "file_changed" and not page.counts_exact


def test_scan_cursor_query_client_target_and_expiry_binding(logs: Any) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    store.byte_limit = 3
    (root / "kodi.log").write_text(record("ERROR"))
    page = scan(logs, logs="current")
    for change in ({"text": "changed"}, {"logs": "both"}):
        with pytest.raises(Exception, match="log_cursor_invalid"):
            scan(logs, **{"logs": "current", "cursor": page.cursor, **change})
    payload = KodiLogScanBridgeRequest(
        logs="current", cursor=page.cursor, client_binding="b" * 64
    ).model_dump()
    with pytest.raises(Exception, match="log_cursor_invalid"):
        store.scan(payload, ("target", "bridge", "secret"), bridge["_open_scan_log"])
    payload["client_binding"] = "c" * 64
    with pytest.raises(Exception, match="log_cursor_invalid"):
        store.scan(
            payload, ("other-target", "bridge", "secret"), bridge["_open_scan_log"]
        )
    store.states[page.cursor].created -= store.ttl
    with pytest.raises(Exception, match="log_cursor_invalid"):
        scan(logs, logs="current", cursor=page.cursor)


@pytest.mark.parametrize(
    "kind",
    ["missing", "empty", "symlink", "hardlink", "directory", "fifo", "unreadable"],
)
def test_scan_missing_empty_and_unsafe_files(
    logs: Any, tmp_path: Path, kind: str
) -> None:
    _, root = logs
    path = root / "kodi.log"
    if kind in ("empty", "unreadable"):
        path.touch()
        if kind == "unreadable":
            path.chmod(0)
    elif kind in ("symlink", "hardlink"):
        secret = tmp_path / "private"
        secret.write_text("secret")
        if kind == "symlink":
            path.symlink_to(secret)
        else:
            os.link(secret, path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    page = scan(logs, logs="current")
    assert page.complete is (kind == "empty")
    assert page.files[0].state == (
        "complete"
        if kind == "empty"
        else "missing"
        if kind == "missing"
        else "unavailable"
    )
    assert not page.matches


def test_scan_time_concurrency_capacity_and_abort_limits(logs: Any) -> None:
    bridge, root = logs
    store = bridge["_LOG_SCANS"]
    (root / "kodi.log").write_text(record("ERROR"))
    store.seconds = 0
    page = scan(logs, logs="current")
    assert page.reason == "time_budget" and page.cursor and not page.counts_exact
    store.capacity = 1
    with pytest.raises(Exception, match="log_scan_busy"):
        scan(logs, logs="current")
    with store.lock, pytest.raises(Exception, match="log_scan_busy"):
        scan(logs, logs="current", cursor=page.cursor)
    payload = KodiLogScanBridgeRequest(
        logs="current", cursor=page.cursor, client_binding="c" * 64
    ).model_dump()
    result = store.scan(
        payload, ("target", "bridge", "secret"), bridge["_open_scan_log"], lambda: True
    )
    assert result["reason"] == "aborted" and result["cursor"]


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/etc/passwd"},
        {"regex": ".*"},
        {"severities": ["NOPE"]},
        {"context_records": True},
        {"max_matches": 21},
        {"start_time": "2026-11-15T00:00:00Z"},
        {"start_time": "2026-19-15T00:00:00"},
        {"text": ""},
    ],
)
def test_scan_closed_input_at_mcp_and_bridge(logs: Any, change: Any) -> None:
    with pytest.raises(ValidationError):
        KodiLogScanRequest.model_validate(change)
    payload = {
        **KodiLogScanBridgeRequest(client_binding="c" * 64).model_dump(),
        **change,
    }
    with pytest.raises(Exception, match="log_request_invalid"):
        logs[0]["_LOG_SCANS"].scan(payload, ("target",), logs[0]["_open_scan_log"])

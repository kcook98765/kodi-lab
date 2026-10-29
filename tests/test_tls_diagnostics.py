"""TLS failure diagnostics preserve requests, secrets and existing watchdogs."""

import gc
import json
from pathlib import Path
from typing import Any, TextIO, cast

import httpx
import pytest

from tests.web.tls_diagnostics import request_diagnostics


def test_success_trace_omits_query_headers_and_body(tmp_path: Path) -> None:
    secret = "private-fixture-sentinel"
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200)

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as client,
        request_diagnostics(tmp_path) as trace,
    ):
        response = trace.request(
            client,
            "POST",
            f"/login?secret={secret}",
            data={"password": secret},
            headers={"Authorization": secret},
        )
    assert response.status_code == 200
    assert len(observed) == 1
    assert secret in observed[0].url.query.decode()
    assert secret in observed[0].content.decode()
    body = (tmp_path / "requests.jsonl").read_text()
    assert secret not in body
    events = [json.loads(line) for line in body.splitlines()]
    assert events[0]["path"] == "/login"
    assert events[-1]["status"] == 200


def test_timeout_keeps_same_exception_and_never_retries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts = []
    failures = []
    before_callbacks = list(gc.callbacks)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        error = httpx.ReadTimeout("private-error-sentinel", request=request)
        failures.append(error)
        raise error

    with (
        pytest.raises(httpx.ReadTimeout) as caught,
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as client,
        request_diagnostics(tmp_path) as trace,
    ):
        trace.request(client, "GET", "/kodi?secret=private-query-sentinel")
    assert caught.value is failures[0]
    assert len(attempts) == 1
    assert "GET /kodi" in caught.value.__notes__[0]
    assert gc.callbacks == before_callbacks
    captured = capsys.readouterr().out
    assert "private-error-sentinel" not in captured
    assert "private-query-sentinel" not in captured
    assert "ReadTimeout" in captured


def test_existing_faulthandler_watchdog_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faulthandler

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("existing watchdog must be preserved")

    monkeypatch.setattr(faulthandler, "dump_traceback_later", forbidden)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", forbidden)
    with request_diagnostics(tmp_path, existing_watchdog=True):
        pass


def test_failed_diagnostic_io_preserves_timeout_and_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.web.tls_diagnostics import RequestTrace

    class BrokenStream:
        def write(self, value: str) -> int:
            raise OSError("diagnostic write unavailable")

        def flush(self) -> None:
            raise OSError("diagnostic flush unavailable")

    attempts = []
    error = httpx.ReadTimeout("original")

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise error

    original_open = Path.open

    def unavailable_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        if args and args[0] == "rb":
            raise PermissionError("diagnostic read unavailable")
        return original_open(path, *args, **kwargs)

    with (
        httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as client,
        pytest.raises(httpx.ReadTimeout) as caught,
        request_diagnostics(tmp_path) as trace,
    ):
        monkeypatch.setattr(trace, "stream", BrokenStream())
        monkeypatch.setattr(Path, "open", unavailable_read)
        trace.request(client, "GET", "/kodi")
    assert caught.value is error
    assert len(attempts) == 1

    # Successful requests also survive unavailable trace writes.
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        base_url="http://test",
    ) as client:
        assert (
            RequestTrace(cast(TextIO, BrokenStream()))
            .request(client, "GET", "/")
            .status_code
            == 200
        )


def test_stack_observer_start_failure_restores_gc_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import faulthandler

    before_callbacks = list(gc.callbacks)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("observer thread unavailable")

    def forbidden_cancel() -> None:
        raise AssertionError("no timer was installed")

    monkeypatch.setattr(faulthandler, "dump_traceback_later", unavailable)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later", forbidden_cancel)
    with request_diagnostics(tmp_path):
        assert len(gc.callbacks) == len(before_callbacks) + 1
    assert gc.callbacks == before_callbacks

"""Bounded private diagnostics for the isolated administrator TLS route test."""

from __future__ import annotations

import faulthandler
import gc
import json
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from io import StringIO
from pathlib import Path
from typing import IO, Any, TextIO
from urllib.parse import urlsplit

import httpx


class RequestTrace:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.records = 0
        self.gc_started: float | None = None

    def record(self, **values: object) -> None:
        if self.records >= 128:
            return
        self.records += 1
        try:
            self.stream.write(
                json.dumps({"at": time.monotonic(), **values}, allow_nan=False) + "\n"
            )
            self.stream.flush()
        except (OSError, ValueError):
            # Diagnostics must never replace a request result or exception.
            pass

    def observe_gc(self, phase: str, info: dict[str, int]) -> None:
        if info.get("generation") != 2:
            return
        if phase == "start":
            self.gc_started = time.monotonic()
        elif phase == "stop" and self.gc_started is not None:
            self.record(event="gc", seconds=time.monotonic() - self.gc_started)
            self.gc_started = None

    def request(
        self, browser: httpx.Client, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        # The test uses fixed routes; omit queries, headers and bodies entirely.
        safe_path = urlsplit(path).path[:256]
        self.record(event="request_start", method=method, path=safe_path)
        started = time.monotonic()
        try:
            response = browser.request(method, path, **kwargs)
        except httpx.RequestError as error:
            self.record(
                event="request_error",
                method=method,
                path=safe_path,
                error_type=type(error).__name__,
                seconds=time.monotonic() - started,
            )
            error.add_note(f"Isolated administrator TLS proxy: {method} {safe_path}")
            raise
        self.record(
            event="request_end",
            method=method,
            path=safe_path,
            status=response.status_code,
            seconds=time.monotonic() - started,
            redirects=[
                {"status": item.status_code, "path": item.url.path[:256]}
                for item in response.history[:20]
            ],
        )
        return response


def _close_diagnostic(stream: IO[Any]) -> None:
    with suppress(OSError, ValueError):
        stream.close()


@contextmanager
def request_diagnostics(
    root: Path, *, existing_watchdog: bool = False
) -> Iterator[RequestTrace]:
    """Observe requests/GC/stacks without changing requests, deadlines or retries."""
    with ExitStack() as resources:
        stream: TextIO
        try:
            stream = (root / "requests.jsonl").open("x")
        except OSError:
            stream = StringIO()
        resources.callback(_close_diagnostic, stream)
        try:
            stacks = (root / "threads.log").open("xb")
        except OSError:
            stacks = None
        if stacks is not None:
            resources.callback(_close_diagnostic, stacks)
        trace = RequestTrace(stream)
        callback = trace.observe_gc
        gc.callbacks.append(callback)
        timer_installed = False
        try:
            # Respect an explicitly configured pytest faulthandler watchdog.
            if not existing_watchdog and stacks is not None:
                try:
                    faulthandler.dump_traceback_later(3, file=stacks)
                    timer_installed = True
                except (OSError, RuntimeError):
                    pass
            yield trace
        except BaseException:
            with suppress(OSError, ValueError):
                stream.flush()
            for name in (
                "requests.jsonl",
                "nginx-access.log",
                "nginx-error.log",
                "threads.log",
            ):
                try:
                    path = root / name
                    if path.is_file():
                        with path.open("rb") as diagnostic:
                            body = diagnostic.read(16384)
                        print(
                            f"TLS diagnostic {name}:\n{body.decode(errors='replace')}"
                        )
                except (OSError, ValueError):
                    continue
            raise
        finally:
            try:
                if timer_installed:
                    faulthandler.cancel_dump_traceback_later()
            finally:
                gc.callbacks.remove(callback)

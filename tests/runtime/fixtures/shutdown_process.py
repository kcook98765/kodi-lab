"""Real isolated processes for the graceful-stop regression; never uses Docker."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(os.environ["STOP_FIXTURE_ROOT"])
MODE = os.environ["STOP_FIXTURE_MODE"]


def event(name: str) -> None:
    with (ROOT / "events.jsonl").open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "event": name,
                    "pid": os.getpid(),
                    "group": os.getpgrp(),
                    "at": time.monotonic(),
                }
            )
            + "\n"
        )


def driver() -> int:
    # Match container PID 1 adoption without changing the pytest process.
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("fixture subreaper setup failed")
    spec = importlib.util.spec_from_file_location(
        "stop_fixture_supervisor", sys.argv[2]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def ready(process: subprocess.Popen[bytes]) -> bool:
        return (ROOT / "display-ready").exists()

    module.__dict__.update(
        DISPLAY_COMMAND=(sys.executable, __file__, "display"),
        KODI_COMMAND=(
            "/usr/bin/dbus-run-session",
            "--",
            sys.executable,
            __file__,
            "wrapper",
        ),
        KODI_EXECUTABLE=Path(sys.executable).resolve().name,
        RPC_URL="http://127.0.0.1:" + os.environ["STOP_FIXTURE_PORT"] + "/jsonrpc",
        KODI_STOP_SECONDS=2.0 if MODE in {"http-hang", "http-trickle"} else 1.2,
        DISPLAY_STOP_SECONDS=0.3,
        REAP_SECONDS=0.2,
        display_ready=ready,
    )
    return int(module.supervise(ROOT))


def wrapper() -> int:
    event("wrapper-start")
    child = subprocess.Popen([sys.executable, __file__, "kodi"])

    def stop(_sig: int, _frame: object) -> None:
        # Model a launcher/D-Bus parent exiting before Kodi completes its save.
        event("wrapper-terminated")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    return child.wait()


def display() -> int:
    event("display-start")
    (ROOT / "display-ready").touch()

    def stop(_sig: int, _frame: object) -> None:
        event("display-terminated")
        if MODE != "display-hang":
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(0.02)


def kodi() -> int:
    if ctypes.CDLL(None).prctl(15, b"kodi.bin", 0, 0, 0) != 0:
        raise RuntimeError("fixture process name setup failed")
    stopping = threading.Event()

    def stop(_sig: int, _frame: object) -> None:
        event("kodi-term")
        if MODE != "hang":
            stopping.set()

    signal.signal(signal.SIGTERM, stop)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["method"] == "Application.Quit"
            event("quit-request")
            if MODE == "http-hang":
                time.sleep(2)
            self.send_response(200)
            self.end_headers()
            result = {"jsonrpc": "2.0", "id": "stop", "result": "OK"}
            if MODE == "rejected":
                result = {"jsonrpc": "2.0", "id": "stop", "error": "denied"}
            payload = json.dumps(result).encode()
            if MODE == "http-trickle":
                # Each byte arrives inside socket inactivity timeout, but the
                # overall transaction must still obey the absolute Quit budget.
                for value in payload:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(0.2)
            else:
                self.wfile.write(payload)
            if MODE not in {"hang", "rejected"}:
                stopping.set()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", int(os.environ["STOP_FIXTURE_PORT"])), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    event("kodi-ready")
    while not stopping.wait(0.02):
        pass
    # Represents actual application save work, not a delay in the supervisor.
    time.sleep(0.15)
    assert (ROOT / "display-ready").exists()
    (ROOT / "saved-setting").write_text("pending-value")
    # A real session-bus call proves the supporting service was not killed early.
    subprocess.run(
        [
            "/usr/bin/dbus-send",
            "--session",
            "--print-reply",
            "--dest=org.freedesktop.DBus",
            "/",
            "org.freedesktop.DBus.ListNames",
        ],
        check=True,
        timeout=1,
        stdout=subprocess.DEVNULL,
    )
    event("bus-alive-at-save")
    event("settings-saved")
    event("kodi-exit")
    return 0


if __name__ == "__main__":
    role = sys.argv[1]
    raise SystemExit(
        {"driver": driver, "wrapper": wrapper, "display": display, "kodi": kodi}[role]()
    )

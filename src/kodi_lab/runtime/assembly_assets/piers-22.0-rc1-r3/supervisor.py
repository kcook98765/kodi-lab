"""Private Xvfb/session-D-Bus supervisor, with an eight-second stop budget.

Docker's existing managed stop timeout is ten seconds. Signal handlers only
request shutdown; HTTP and waits run outside handlers. Quit gets one second,
Kodi gets the remainder of six seconds, Xvfb gets 1.5 seconds. Forced termination
is a recorded failure, never reported as a clean stop.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

from profile_init import initialize
from xvfb_guard import clear_stale_display

KODI_STOP_SECONDS = 6.0
XVFB_STOP_SECONDS = 1.5
TOTAL_STOP_BUDGET_SECONDS = 8.0


def terminate(process: subprocess.Popen[bytes], seconds: float) -> bool:
    if process.poll() is not None:
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=seconds)
        return False
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=0.25)
        return True


def quit_kodi(process: subprocess.Popen[bytes]) -> str:
    request = urllib.request.Request(
        "http://127.0.0.1:8080/jsonrpc",
        data=b'{"jsonrpc":"2.0","id":"stop","method":"Application.Quit"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            response.read(8192)
        return "Application.Quit"
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        return "SIGTERM"


def main() -> int:
    profile: Path = initialize()
    record: dict[str, object] = {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "forced_kill": False,
        "shutdown_request": False,
    }
    requested_at: float | None = None

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal requested_at
        if requested_at is None:
            requested_at = time.monotonic()
            record["shutdown_request"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    display = kodi = None
    exit_code = 1
    try:
        clear_stale_display()
        with (
            (profile / "xvfb.log").open("ab") as xlog,
            (profile / "kodi-stdout.log").open("ab") as klog,
        ):
            display = subprocess.Popen(
                [
                    "/usr/bin/Xvfb",
                    ":99",
                    "-screen",
                    "0",
                    "640x360x24",
                    "-nolisten",
                    "tcp",
                    "-ac",
                ],
                stdout=xlog,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            for _ in range(100):
                if requested_at is not None:
                    return 0
                if display.poll() is not None:
                    raise RuntimeError("Xvfb exited before readiness")
                result = subprocess.run(
                    ["/usr/bin/xdpyinfo", "-display", ":99"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                )
                if result.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Xvfb readiness timeout")
            kodi = subprocess.Popen(
                [
                    "/usr/bin/dbus-run-session",
                    "--",
                    "/usr/local/bin/kodi-lab-launcher",
                    "--standalone",
                    "--windowing=x11",
                    "--audio-backend=alsa",
                ],
                stdout=klog,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            sent = False
            while kodi.poll() is None:
                if display.poll() is not None:
                    record["display_failed"] = True
                    request_stop(signal.SIGTERM, None)
                if requested_at is not None:
                    if not sent:
                        record["shutdown_method"] = quit_kodi(kodi)
                        sent = True
                    if time.monotonic() - requested_at >= KODI_STOP_SECONDS:
                        os.killpg(kodi.pid, signal.SIGKILL)
                        kodi.wait(timeout=0.25)
                        record["forced_kill"] = True
                        break
                time.sleep(0.05)
            record["kodi_exit_code"] = kodi.wait()
            exit_code = (
                0 if kodi.returncode == 0 and not record.get("display_failed") else 1
            )
    finally:
        if kodi is not None:
            record["forced_kill"] = terminate(kodi, 0.25) or record["forced_kill"]
        if display is not None:
            record["forced_kill"] = (
                terminate(display, XVFB_STOP_SECONDS) or record["forced_kill"]
            )
            record["xvfb_exit_code"] = display.returncode
        # Reap any children reparented to PID 1 (the local session bus included).
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
        if requested_at is not None:
            record["shutdown_seconds"] = round(time.monotonic() - requested_at, 3)
        with (profile / "supervisor-cycles.jsonl").open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    return 1 if record["forced_kill"] else exit_code


if __name__ == "__main__":
    raise SystemExit(main())

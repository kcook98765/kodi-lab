"""Frozen v1 APT-runtime supervisor using the existing Piers shutdown pattern.

Future behavior changes need a new asset version and new reviewed recipes.

Keep Xvfb alive while Kodi saves. All process groups are created here; no caller
can select a command, PID or endpoint. The complete stop budget fits below the
broker's ten-second Docker timeout. Forced cleanup is never a successful exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

QUIT_REQUEST_SECONDS = 1.0
KODI_STOP_SECONDS = 6.0
DISPLAY_STOP_SECONDS = 1.0
REAP_SECONDS = 0.25
TOTAL_STOP_BUDGET_SECONDS = 8.0
DISPLAY_COMMAND = (
    "/usr/bin/Xvfb",
    ":99",
    "-screen",
    "0",
    "640x360x24",
    "-nolisten",
    "tcp",
    "-ac",
)
KODI_COMMAND = ("/usr/bin/dbus-run-session", "--", "/usr/bin/kodi", "--standalone")
RPC_URL = "http://127.0.0.1:8080/jsonrpc"
KODI_EXECUTABLE = "kodi.bin"


def group_alive(process: subprocess.Popen[bytes]) -> bool:
    # poll first: Popen owns its direct child's status. PID 1 must also reap
    # adopted Kodi/D-Bus descendants, even after the session wrapper has exited.
    if process.poll() is not None:
        try:
            while os.waitpid(-process.pid, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def signal_group(process: subprocess.Popen[bytes], sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


def wait_group(process: subprocess.Popen[bytes], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while group_alive(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def terminate(process: subprocess.Popen[bytes], seconds: float) -> bool:
    if not group_alive(process):
        return False
    signal_group(process, signal.SIGTERM)
    if wait_group(process, seconds):
        return False
    signal_group(process, signal.SIGKILL)
    if not wait_group(process, REAP_SECONDS):
        raise RuntimeError("owned process group did not exit after bounded cleanup")
    return True


def signal_kodi(process: subprocess.Popen[bytes]) -> bool:
    """Signal one verified Kodi main process, keeping its bus/session alive."""
    descriptors: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            descriptor = None
            try:
                pid = int(entry.name)
                if os.getpgid(pid) != process.pid:
                    continue
                descriptor = os.pidfd_open(pid)
                if (
                    os.getpgid(pid) != process.pid
                    or entry.stat().st_uid != os.geteuid()
                    or (entry / "comm").read_text().strip() != "kodi.bin"
                    or Path(os.readlink(entry / "exe")).name != KODI_EXECUTABLE
                ):
                    continue
                descriptors.append(descriptor)
                descriptor = None
            except OSError:
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if len(descriptors) == 1:
            signal.pidfd_send_signal(descriptors[0], signal.SIGTERM)
            return True
        return False
    except (OSError, AttributeError):
        # Unavailable kernel support or ambiguous identity never authorizes a
        # different PID. The owned-group deadline still bounds failed shutdown.
        return False
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def request_quit(acknowledged: threading.Event) -> None:
    request = urllib.request.Request(
        RPC_URL,
        data=b'{"jsonrpc":"2.0","id":"stop","method":"Application.Quit"}',
        headers={"Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=QUIT_REQUEST_SECONDS) as response:
            payload = response.read(8193)
        result = json.loads(payload) if len(payload) <= 8192 else None
        if (
            isinstance(result, dict)
            and result.get("id") == "stop"
            and result.get("result") == "OK"
        ):
            acknowledged.set()
    except Exception:
        pass


def quit_kodi(process: subprocess.Popen[bytes]) -> str:
    # Socket timeouts bound inactivity, not a trickling HTTP transaction. One
    # daemon thread bounds this single request absolutely; it cannot outlive the
    # container supervisor or redispatch Quit. No extra process/service is added.
    acknowledged = threading.Event()
    worker = threading.Thread(target=request_quit, args=(acknowledged,), daemon=True)
    try:
        worker.start()
        worker.join(QUIT_REQUEST_SECONDS)
    except RuntimeError:
        pass
    if acknowledged.is_set():
        return "Application.Quit"
    # Kodi's TERM handler also requests a graceful Quit. Keep D-Bus/display alive.
    return "SIGTERM-kodi" if signal_kodi(process) else "unavailable"


def display_ready(process: subprocess.Popen[bytes]) -> bool:
    result = subprocess.run(
        ("/usr/bin/xdpyinfo", "-display", ":99"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=1,
        check=False,
    )
    return (
        result.returncode == 0
        and process.poll() is None
        and Path("/tmp/.X99-lock").read_text().strip() == str(process.pid)
    )


def supervise(profile: Path) -> int:
    record: dict[str, object] = {"shutdown_request": False, "forced_kill": False}
    requested_at: float | None = None

    def request_stop(_sig: int, _frame: object) -> None:
        nonlocal requested_at
        if requested_at is None:
            requested_at = time.monotonic()
            record["shutdown_request"] = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    display = kodi = None
    exit_code = 1
    try:
        with (
            (profile / "xvfb.log").open("ab") as xlog,
            (profile / "kodi-stdout.log").open("ab") as klog,
        ):
            display = subprocess.Popen(
                DISPLAY_COMMAND,
                stdout=xlog,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            for _ in range(100):
                if requested_at is not None:
                    exit_code = 0
                    break
                if display.poll() is not None:
                    raise RuntimeError("Xvfb exited before readiness")
                if display_ready(display):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Xvfb readiness timeout")
            if requested_at is None:
                kodi = subprocess.Popen(
                    KODI_COMMAND,
                    stdout=klog,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={**os.environ, "LP_NUM_THREADS": "1"},
                )
                sent = False
                while group_alive(kodi):
                    if display.poll() is not None:
                        record["display_failed"] = True
                        request_stop(signal.SIGTERM, None)
                    # A dead wrapper must not strand live descendants forever.
                    if kodi.poll() is not None:
                        request_stop(signal.SIGTERM, None)
                    if requested_at is not None:
                        if not sent:
                            record["shutdown_method"] = quit_kodi(kodi)
                            sent = True
                        if time.monotonic() - requested_at >= KODI_STOP_SECONDS:
                            record["timed_out"] = True
                            break
                    time.sleep(0.02)
                record["kodi_exit_code"] = kodi.poll()
                exit_code = (
                    0
                    if kodi.returncode == 0
                    and not record.get("display_failed")
                    and not record.get("timed_out")
                    else 1
                )
    finally:
        # Even after the wrapper exits, drain its whole owned group before Xvfb.
        # Each cleanup runs even if another group cannot be reaped.
        for name, process, budget in (
            ("kodi", kodi, REAP_SECONDS),
            ("display", display, DISPLAY_STOP_SECONDS),
        ):
            if process is not None:
                try:
                    record["forced_kill"] = (
                        terminate(process, budget) or record["forced_kill"]
                    )
                    record[name + "_exit_code"] = process.poll()
                except Exception:
                    record["cleanup_failed"] = True
        if requested_at is not None:
            record["shutdown_seconds"] = round(time.monotonic() - requested_at, 3)
        with (profile / "supervisor-cycles.jsonl").open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    return 1 if record["forced_kill"] or record.get("cleanup_failed") else exit_code


def main() -> int:
    profile = Path.home() / ".kodi"
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    profile.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    subprocess.run(
        ("/usr/bin/python3", "/usr/local/bin/kodi-lab-xvfb-guard"), check=True
    )
    return supervise(profile)


if __name__ == "__main__":
    raise SystemExit(main())

"""Broker-packaged x0 worker, run as the runtime UID with python3 -I -B.

stdin: [kind:u8, length:u32be, payload]; kind 1=RFB, 2=renew, 3=close.
Only the trusted broker creates envelopes. stdout is raw RFB. No shell/TTY.
The selector watchdog remains active under blocked reads/writes.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import secrets
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from runpy import run_path

CHUNK = 65536
BACKLOG = 1024 * 1024
LEASE_SECONDS = 3.0
SEALED_HELPER = "/usr/local/lib/kodi-lab-novnc-view.py"


def command(socket_path: str, rate: int) -> list[str]:
    """Keep the approved runtime's view-only flags and remove its time limit."""
    # Matrix and Nexus ship TigerVNC without X11 clipboard support. Their
    # sealed policy omits arguments that those versions reject at startup.
    policy = run_path(SEALED_HELPER)["command"]
    args: list[str] = policy(socket_path, rate)
    args[args.index("-MaxConnectionTime=600")] = "-MaxConnectionTime=0"
    return args


def run(rate: int) -> None:
    os.umask(0o077)
    root = Path("/run/kodi")
    # flock fences surviving helpers across broker restarts. Never unlink lock.
    lock = os.open(
        root / "novnc-phase1.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    directory = tempfile.mkdtemp(prefix="view-", dir=root)
    path = str(Path(directory) / "rfb.sock")
    child: subprocess.Popen[bytes] | None = None
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    selector = selectors.DefaultSelector()
    started = time.monotonic()
    renewed = started
    incoming = bytearray()
    to_vnc = bytearray()
    to_broker = bytearray()
    closed = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal closed
        closed = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        supervisor_pid = os.getpid()

        def child_guard() -> None:
            # Linux PR_SET_PDEATHSIG: uncatchable helper loss also kills x0.
            if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
                os._exit(70)
            if os.getppid() != supervisor_pid:
                os._exit(70)

        nonce = secrets.token_bytes(32)
        os.write(1, b"KLVNCARM" + nonce)
        os.set_blocking(0, False)
        arm = bytearray()
        while len(arm) < 37 and time.monotonic() - started < 2 and not closed:
            try:
                data = os.read(0, 37 - len(arm))
            except BlockingIOError:
                time.sleep(0.01)
                continue
            if not data:
                raise RuntimeError("worker was not armed")
            arm.extend(data)
        if bytes(arm) != struct.pack("!BI", 4, 32) + nonce or closed:
            raise RuntimeError("worker was not armed")
        renewed = time.monotonic()
        child = subprocess.Popen(
            command(path, rate),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"DISPLAY": ":99", "HOME": "/home/kodi", "PATH": "/usr/bin:/bin"},
            close_fds=True,
            pass_fds=(lock,),
            preexec_fn=child_guard,
        )
        while not Path(path).exists():
            if child.poll() is not None or time.monotonic() - started > 2:
                raise RuntimeError("view server startup failed")
            time.sleep(0.02)
        peer.settimeout(max(0.001, 2 - (time.monotonic() - started)))
        peer.connect(path)
        peer.setblocking(False)
        for fd in (0, 1):
            os.set_blocking(fd, False)
        selector.register(0, selectors.EVENT_READ)
        selector.register(peer, selectors.EVENT_READ)
        while not closed:
            now = time.monotonic()
            if now - renewed >= LEASE_SECONDS:
                break
            if child.poll() is not None:
                break
            selector.modify(
                peer, selectors.EVENT_READ | (selectors.EVENT_WRITE if to_vnc else 0)
            )
            if to_broker:
                with suppress(KeyError):
                    selector.register(1, selectors.EVENT_WRITE)
            else:
                with suppress(KeyError):
                    selector.unregister(1)
            for key, mask in selector.select(0.05):
                if key.fd == 0:
                    data = os.read(0, CHUNK)
                    if not data:
                        closed = True
                        break
                    incoming.extend(data)
                    while len(incoming) >= 5:
                        kind, size = struct.unpack("!BI", incoming[:5])
                        if (
                            size > CHUNK
                            or kind not in (1, 2, 3)
                            or (kind != 1 and size)
                        ):
                            raise ValueError("invalid trusted envelope")
                        if len(incoming) < 5 + size:
                            break
                        payload = incoming[5 : 5 + size]
                        del incoming[: 5 + size]
                        if kind == 1:
                            to_vnc.extend(payload)
                        elif kind == 2:
                            renewed = time.monotonic()
                        else:
                            closed = True
                elif key.fd == 1:
                    count = os.write(1, to_broker[:CHUNK])
                    del to_broker[:count]
                else:
                    if mask & selectors.EVENT_READ:
                        data = peer.recv(CHUNK)
                        if not data:
                            closed = True
                            break
                        to_broker.extend(data)
                    if mask & selectors.EVENT_WRITE and to_vnc:
                        count = peer.send(to_vnc[:CHUNK])
                        del to_vnc[:count]
            if max(len(incoming), len(to_broker), len(to_vnc)) > BACKLOG:
                raise RuntimeError("view backlog exceeded")
    finally:
        selector.close()
        peer.close()
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    child.kill()
            child.wait(timeout=0.5)
        try:
            shutil.rmtree(directory)
        finally:
            os.close(lock)


if __name__ == "__main__":
    # The in-image cleanup probe also recognizes this marker in /proc.
    if sys.argv[1:] != [SEALED_HELPER]:
        raise SystemExit(64)
    run(int(os.environ["KODI_LAB_VIEW_RATE"]))

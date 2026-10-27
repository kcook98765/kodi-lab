"""Fixed view-only x0 worker. Installed root-owned; run with python3 -I -B.

stdin: [kind:u8, length:u32be, payload]; kind 1=RFB, 2=renew, 3=close.
Only the trusted broker creates envelopes. stdout is raw RFB. No shell/TTY.
The selector watchdog remains active under blocked reads/writes.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

CHUNK = 65536
BACKLOG = 1024 * 1024
LEASE_SECONDS = 3.0
LIFETIME_SECONDS = 900.0


def command(socket_path: str, rate: int) -> list[str]:
    """Closed policy: client preferences cannot enable input or TCP."""
    if rate not in (10, 15):
        raise ValueError("unsupported rate")
    return [
        "/usr/bin/X0tigervnc",
        "-display=:99",
        "-rfbport=-1",
        f"-rfbunixpath={socket_path}",
        "-rfbunixmode=0600",
        "-SecurityTypes=None",
        "-AcceptKeyEvents=0",
        "-AcceptPointerEvents=0",
        "-AcceptCutText=0",
        "-SendCutText=0",
        "-AcceptSetDesktopSize=0",
        "-SetPrimary=0",
        "-SendPrimary=0",
        "-NeverShared=1",
        "-DisconnectClients=0",
        "-MaxDisconnectionTime=5",
        "-MaxConnectionTime=900",
        "-MaxProcessorUsage=35",
        f"-FrameRate={rate}",
        "-Log=*:stderr:0",
    ]


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

        child = subprocess.Popen(
            command(path, rate),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"DISPLAY": ":99", "HOME": "/home/kodi", "PATH": "/usr/bin:/bin"},
            close_fds=True,
            preexec_fn=child_guard,
        )
        while not Path(path).exists():
            if child.poll() is not None or time.monotonic() - started > 2:
                raise RuntimeError("view server startup failed")
            time.sleep(0.02)
        peer.connect(path)
        peer.setblocking(False)
        for fd in (0, 1):
            os.set_blocking(fd, False)
        selector.register(0, selectors.EVENT_READ)
        selector.register(peer, selectors.EVENT_READ)
        while not closed:
            now = time.monotonic()
            if now - renewed >= LEASE_SECONDS or now - started >= LIFETIME_SECONDS:
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
        shutil.rmtree(directory)
        os.close(lock)


if __name__ == "__main__":
    # Environment is set by a fixed broker command, never supplied by Web/RFB.
    run(int(os.environ.get("KODI_LAB_VIEW_RATE", "10")))

"""Fixed view-only x0 worker. Installed root-owned; run with python3 -I -B.

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

# TigerVNC 1.12.0 XDesktop::clientCutText is empty and has no X11 clipboard
# implementation. These unsupported flags must not be passed to this exact
# reviewed Jammy package; newer Omega/Piers helpers retain explicit off flags.
CLIPBOARD_POLICY = "unimplemented"

CHUNK = 65536
BACKLOG = 1024 * 1024
LEASE_SECONDS = 3.0
LIFETIME_SECONDS = 600.0


def command(socket_path: str, rate: int, mode: str = "view-only") -> list[str]:
    """Closed policy: client preferences cannot enable input or TCP."""
    if rate not in (2, 10, 15) or mode not in ("view-only", "interactive"):
        raise ValueError("unsupported rate")
    return [
        "/usr/bin/X0tigervnc",
        "-display=:99",
        "-rfbport=-1",
        f"-rfbunixpath={socket_path}",
        "-rfbunixmode=0600",
        "-SecurityTypes=None",
        f"-AcceptKeyEvents={int(mode == 'interactive')}",
        f"-AcceptPointerEvents={int(mode == 'interactive')}",
        "-AcceptSetDesktopSize=0",
        "-NeverShared=1",
        "-DisconnectClients=0",
        "-MaxDisconnectionTime=5",
        "-MaxConnectionTime=600",
        "-MaxProcessorUsage=35",
        f"-FrameRate={rate}",
        "-Log=*:stderr:0",
    ]


def run(rate: int, mode: str = "view-only") -> None:
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
            command(path, rate, mode),
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
            if mode == "interactive":
                # This armed helper still owns the inherited lock and durable
                # fence. A separately launched proof never mutates X input.
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                signal.alarm(1)
                try:
                    check_input(release=True)
                finally:
                    signal.alarm(0)
        try:
            shutil.rmtree(directory)
        finally:
            os.close(lock)


def check_input(*, release: bool) -> None:
    """Release held XTest keys/buttons only after exclusive controller death.

    The durable controller fence still excludes other writers. No arbitrary
    event, key, command or device is accepted by this fixed cleanup operation.
    """
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    display_type = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = display_type
    x11.XDefaultRootWindow.argtypes = [display_type]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XQueryKeymap.argtypes = [display_type, ctypes.c_void_p]
    x11.XQueryPointer.argtypes = [
        display_type,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XSync.argtypes = [display_type, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [display_type]
    xtst.XTestFakeKeyEvent.argtypes = [
        display_type,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xtst.XTestFakeButtonEvent.argtypes = [
        display_type,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    display = x11.XOpenDisplay(b":99")
    if not display:
        raise RuntimeError("input cleanup display unavailable")

    def state() -> tuple[bytes, int]:
        keys = (ctypes.c_ubyte * 32)()
        x11.XQueryKeymap(display, keys)
        roots = [ctypes.c_ulong(), ctypes.c_ulong()]
        coords = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        if not x11.XQueryPointer(
            display,
            x11.XDefaultRootWindow(display),
            *[ctypes.byref(v) for v in (*roots, *coords, mask)],
        ):
            raise RuntimeError("input state unavailable")
        return bytes(keys), mask.value

    try:
        keys, mask = state()
        if release:
            for key in range(8, 256):
                if keys[key // 8] & (1 << (key % 8)):
                    xtst.XTestFakeKeyEvent(display, key, 0, 0)
            for button in range(1, 6):
                if mask & (1 << (button + 7)):
                    xtst.XTestFakeButtonEvent(display, button, 0, 0)
        x11.XSync(display, 0)
        keys, mask = state()
        if any(keys) or mask & 0x1F00:
            raise RuntimeError("input neutral state unproven")
    finally:
        x11.XCloseDisplay(display)


def probe(*, interactive: bool = False) -> None:
    """Prove lock ownership is available and no owned X0/helper process survives.

    The child inherits the flock descriptor, so helper SIGKILL cannot release
    exclusion before its X0 child dies. Stale private directories are harmless.
    """
    lock = os.open(
        "/run/kodi/novnc-phase1.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for item in Path("/proc").iterdir():
            if not item.name.isdigit() or int(item.name) == os.getpid():
                continue
            try:
                args = (item / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            if (
                b"/usr/bin/X0tigervnc" in args
                or b"/usr/local/lib/kodi-lab-novnc-view.py" in args
            ):
                raise RuntimeError("native worker remains")
        if interactive:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(1)
            check_input(release=False)
            signal.alarm(0)
    finally:
        os.close(lock)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        probe()
    elif sys.argv[1:] == ["--probe-control"]:
        probe(interactive=True)
    elif not sys.argv[1:]:
        # Only the root broker sets these closed values after durable admission.
        run(
            int(os.environ.get("KODI_LAB_VIEW_RATE", "2")),
            os.environ.get("KODI_LAB_VIEW_MODE", "view-only"),
        )
    else:
        raise SystemExit(64)

"""Fixed display-99 preflight for the private managed Kodi container.

This script accepts no caller path or display.  Its internal ``root`` argument
exists solely to exercise the same descriptor-relative checks in disposable
tests; the installed entrypoint always calls ``main()`` with the real root.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path

_LOCK = ".X99-lock"
_SOCKET = "X99"
_ABSTRACT = b"\0/tmp/.X11-unix/X99"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class UnsafeDisplayState(RuntimeError):
    """Display state cannot be proved stale without touching a live server."""


def _directory(path: Path, expected_uid: int) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
        entry = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(entry.st_mode)
            or entry.st_uid != expected_uid
            or stat.S_IMODE(entry.st_mode) != 0o1777
        ):
            raise UnsafeDisplayState("display directory identity differs")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def _entry(descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, first.st_mode, first.st_uid, first.st_gid) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_uid,
        second.st_gid,
    )


def _lock_pid(descriptor: int, entry: os.stat_result, uid: int, gid: int) -> int:
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_uid != uid
        or entry.st_gid != gid
        or entry.st_nlink != 1
        or stat.S_IMODE(entry.st_mode) != 0o444
        or entry.st_size != 11
    ):
        raise UnsafeDisplayState("display lock identity differs")
    fd = os.open(_LOCK, _FILE_FLAGS, dir_fd=descriptor)
    try:
        if not _same(entry, os.fstat(fd)):
            raise UnsafeDisplayState("display lock changed during inspection")
        payload = os.read(fd, 12)
    finally:
        os.close(fd)
    if len(payload) != 11 or payload[-1:] != b"\n":
        raise UnsafeDisplayState("display lock payload is malformed")
    try:
        pid = int(payload[:10].decode("ascii"))
    except (ValueError, UnicodeDecodeError) as error:
        raise UnsafeDisplayState("display lock PID is malformed") from error
    if pid <= 0 or payload != f"{pid:10d}\n".encode("ascii"):
        raise UnsafeDisplayState("display lock PID is malformed")
    return pid


def _no_listener(address: str | bytes) -> None:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        try:
            probe.connect(address)
        except OSError as error:
            if error.errno == errno.ECONNREFUSED:
                return
            raise UnsafeDisplayState(
                "display listener state is inconclusive"
            ) from error
        raise UnsafeDisplayState("display listener is active")
    finally:
        probe.close()


def _is_display_server(pid: int, proc_root: Path) -> bool:
    process = proc_root / str(pid)
    try:
        executable = os.readlink(process / "exe")
        command = (process / "cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return False
    except OSError as error:
        raise UnsafeDisplayState("display process identity is inconclusive") from error
    name = Path(executable).name
    return name in {"Xvfb", "Xorg", "X", "Xwayland"} or any(
        token in {b"Xvfb", b"Xorg", b"Xwayland"} for token in command[:1]
    )


def _no_display_process(proc_root: Path) -> None:
    try:
        processes = tuple(proc_root.iterdir())
    except OSError as error:
        raise UnsafeDisplayState("process namespace cannot be inspected") from error
    for process in processes:
        if process.name.isdecimal() and _is_display_server(
            int(process.name), proc_root
        ):
            raise UnsafeDisplayState("display server process is active")


def _remove_exact(
    descriptor: int,
    name: str,
    inspected: os.stat_result,
    before_unlink: Callable[[str], None] | None,
) -> None:
    if before_unlink is not None:
        before_unlink(name)
    current = _entry(descriptor, name)
    if current is None or not _same(inspected, current):
        raise UnsafeDisplayState("display artifact changed before removal")
    os.unlink(name, dir_fd=descriptor)


def clear_stale_display(
    *,
    root: Path = Path("/"),
    proc_root: Path = Path("/proc"),
    root_uid: int = 0,
    before_unlink: Callable[[str], None] | None = None,
) -> tuple[str, ...]:
    """Remove only proved-stale display-99 entries from a private container."""

    uid, gid = os.geteuid(), os.getegid()
    tmp_fd = _directory(root / "tmp", root_uid)
    try:
        socket_fd = _directory(root / "tmp" / ".X11-unix", root_uid)
        try:
            lock = _entry(tmp_fd, _LOCK)
            endpoint = _entry(socket_fd, _SOCKET)
            if lock is not None:
                _lock_pid(tmp_fd, lock, uid, gid)
            if endpoint is not None and (
                not stat.S_ISSOCK(endpoint.st_mode)
                or endpoint.st_uid != uid
                or endpoint.st_gid != gid
                or endpoint.st_nlink != 1
            ):
                raise UnsafeDisplayState("display socket identity differs")
            # An abstract listener has no persistent pathname.  Probe it even
            # when neither filesystem artifact exists, before starting Xvfb.
            _no_listener(_ABSTRACT)
            if endpoint is not None:
                _no_listener(str(root / "tmp" / ".X11-unix" / _SOCKET))
            _no_display_process(proc_root)
            removed: list[str] = []
            # Socket first: if interrupted, the remaining lock blocks a new
            # X server until the next complete preflight.
            if endpoint is not None:
                _remove_exact(socket_fd, _SOCKET, endpoint, before_unlink)
                removed.append("/tmp/.X11-unix/X99")
            if lock is not None:
                _remove_exact(tmp_fd, _LOCK, lock, before_unlink)
                removed.append("/tmp/.X99-lock")
            return tuple(removed)
        finally:
            os.close(socket_fd)
    finally:
        os.close(tmp_fd)


def main() -> int:
    if len(sys.argv) != 1:
        print("display guard accepts no arguments", file=sys.stderr)
        return 2
    try:
        removed = clear_stale_display()
    except (OSError, UnsafeDisplayState) as error:
        print(f"display 99 preflight refused: {error}", file=sys.stderr)
        return 1
    for name in removed:
        print(f"removed stale display 99 artifact: {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

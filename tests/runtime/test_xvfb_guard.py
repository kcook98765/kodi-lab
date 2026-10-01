"""Display-99 cleanup is bounded to demonstrably stale managed-runtime state."""

from __future__ import annotations

import errno
import importlib.util
import os
import socket
import subprocess
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest


def _guard() -> ModuleType:
    source = (
        Path(__file__).parents[2] / "src/kodi_lab/runtime/assembly_assets/xvfb_guard.py"
    )
    spec = importlib.util.spec_from_file_location("xvfb_guard_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def display(tmp_path: Path) -> tuple[ModuleType, Path, Path]:
    guard = _guard()
    directory = tmp_path / "tmp"
    directory.mkdir(mode=0o1777)
    directory.chmod(0o1777)
    sockets = directory / ".X11-unix"
    sockets.mkdir(mode=0o1777)
    sockets.chmod(0o1777)
    proc = tmp_path / "proc"
    proc.mkdir()
    return guard, tmp_path, proc


def _lock(root: Path, pid: int = 9) -> Path:
    path = root / "tmp/.X99-lock"
    path.write_bytes(f"{pid:10d}\n".encode())
    path.chmod(0o444)
    return path


def _socket(root: Path) -> Path:
    path = root / "tmp/.X11-unix/X99"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    sock.close()
    return path


def _clear(display: tuple[ModuleType, Path, Path]) -> tuple[str, ...]:
    guard, root, proc = display
    return cast(
        tuple[str, ...],
        guard.clear_stale_display(root=root, proc_root=proc, root_uid=os.geteuid()),
    )


def test_clean_and_repeat_start_are_noops(
    display: tuple[ModuleType, Path, Path],
) -> None:
    assert _clear(display) == ()
    assert _clear(display) == ()


@pytest.mark.parametrize(
    "lock,socket_file", [(True, False), (False, True), (True, True)]
)
def test_only_stale_display_artifacts_are_removed(
    display: tuple[ModuleType, Path, Path], lock: bool, socket_file: bool
) -> None:
    _, root, _ = display
    if lock:
        _lock(root)
    if socket_file:
        _socket(root)
    unrelated = root / "tmp/unrelated"
    unrelated.write_text("retain")
    other_display = root / "tmp/.X100-lock"
    other_display.write_text("retain")
    transient = root / "tmp/.tX99-lock"
    transient.write_text("X server-owned transient")
    removed = _clear(display)
    assert set(removed) == (
        ({"/tmp/.X99-lock"} if lock else set())
        | ({"/tmp/.X11-unix/X99"} if socket_file else set())
    )
    assert unrelated.read_text() == "retain"
    assert other_display.read_text() == "retain"
    assert transient.read_text() == "X server-owned transient"
    assert _clear(display) == ()


def test_reused_pid_of_unrelated_process_does_not_make_old_lock_live(
    display: tuple[ModuleType, Path, Path],
) -> None:
    _, root, proc = display
    _lock(root)
    process = proc / "9"
    process.mkdir()
    (process / "exe").symlink_to("/usr/bin/sleep")
    (process / "cmdline").write_bytes(b"sleep\0")
    assert _clear(display) == ("/tmp/.X99-lock",)


def test_live_xvfb_process_refuses_cleanup(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, proc = display
    lock = _lock(root)
    process = proc / "9"
    process.mkdir()
    (process / "exe").symlink_to("/usr/bin/Xvfb")
    (process / "cmdline").write_bytes(b"Xvfb\0:99\0")
    with pytest.raises(guard.UnsafeDisplayState, match="active"):
        _clear(display)
    assert lock.exists()


def test_accepting_filesystem_listener_refuses_cleanup(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, _ = display
    _lock(root)
    path = root / "tmp/.X11-unix/X99"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen(1)
    try:
        with pytest.raises(guard.UnsafeDisplayState, match="active"):
            _clear(display)
    finally:
        listener.close()
    assert path.exists()


def test_accepting_abstract_listener_refuses_cleanup(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, _ = display
    lock = _lock(root)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(b"\0/tmp/.X11-unix/X99")
    listener.listen(1)
    try:
        with pytest.raises(guard.UnsafeDisplayState, match="active"):
            _clear(display)
    finally:
        listener.close()
    assert lock.exists()


@pytest.mark.parametrize("payload", [b"bad\n", b"         0\n", b"     xxxxx\n"])
def test_malformed_lock_refuses_cleanup(
    display: tuple[ModuleType, Path, Path], payload: bytes
) -> None:
    guard, root, _ = display
    lock = _lock(root)
    lock.chmod(0o644)
    lock.write_bytes(payload)
    lock.chmod(0o444)
    with pytest.raises(guard.UnsafeDisplayState):
        _clear(display)
    assert lock.exists()


def test_unexpected_type_mode_and_parent_refuse_cleanup(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, _ = display
    lock = root / "tmp/.X99-lock"
    lock.symlink_to(root / "tmp/other")
    with pytest.raises(guard.UnsafeDisplayState):
        _clear(display)
    lock.unlink()
    lock = _lock(root)
    lock.chmod(0o644)
    with pytest.raises(guard.UnsafeDisplayState):
        _clear(display)
    lock.chmod(0o444)
    (root / "tmp/.X11-unix").chmod(0o755)
    with pytest.raises(guard.UnsafeDisplayState):
        _clear(display)
    assert lock.exists()


def test_foreign_owner_and_inconclusive_listener_refuse_cleanup(
    display: tuple[ModuleType, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, root, _ = display
    lock = _lock(root)
    original = guard._entry

    def foreign_owner(fd: int, name: str) -> os.stat_result | None:
        value = cast(os.stat_result | None, original(fd, name))
        if name != ".X99-lock" or value is None:
            return value
        fields = list(value)
        fields[4] = os.geteuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(guard, "_entry", foreign_owner)
    with pytest.raises(guard.UnsafeDisplayState):
        _clear(display)
    monkeypatch.setattr(guard, "_entry", original)

    def uncertain(_address: str | bytes) -> None:
        raise guard.UnsafeDisplayState("display listener state is inconclusive")

    monkeypatch.setattr(guard, "_no_listener", uncertain)
    with pytest.raises(guard.UnsafeDisplayState, match="inconclusive"):
        _clear(display)
    assert lock.exists()


def test_interruption_after_first_removal_is_retryable(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, proc = display
    lock = _lock(root)
    endpoint = _socket(root)

    def interrupt(name: str) -> None:
        if name == ".X99-lock":
            raise InterruptedError(errno.EINTR, "simulated termination")

    with pytest.raises(InterruptedError):
        guard.clear_stale_display(
            root=root,
            proc_root=proc,
            root_uid=os.geteuid(),
            before_unlink=interrupt,
        )
    assert lock.exists() and not endpoint.exists()
    assert _clear(display) == ("/tmp/.X99-lock",)


def test_replacement_between_check_and_unlink_is_preserved(
    display: tuple[ModuleType, Path, Path],
) -> None:
    guard, root, proc = display
    lock = _lock(root)

    def replace(name: str) -> None:
        if name == ".X99-lock":
            lock.unlink()
            lock.write_bytes(b"replacement")

    with pytest.raises(guard.UnsafeDisplayState, match="changed"):
        guard.clear_stale_display(
            root=root,
            proc_root=proc,
            root_uid=os.geteuid(),
            before_unlink=replace,
        )
    assert lock.read_bytes() == b"replacement"


def test_entrypoint_checks_child_and_own_lock_before_kodi() -> None:
    script = (
        Path(__file__).parents[2] / "src/kodi_lab/runtime/assembly_assets/entrypoint.sh"
    ).read_text()
    assert script.index("kodi-lab-xvfb-guard") < script.index("Xvfb :99")
    assert script.index('kill -0 "$xvfb_pid"') < script.index("xdpyinfo -display :99")
    assert script.index('"$lock_pid" = "$xvfb_pid"') < script.index("dbus-run-session")
    assert "-nolock" not in script


@pytest.mark.parametrize("xvfb_exits", [True, False])
def test_entrypoint_requires_own_live_xvfb_before_kodi(
    tmp_path: Path, xvfb_exits: bool
) -> None:
    source = (
        Path(__file__).parents[2] / "src/kodi_lab/runtime/assembly_assets/entrypoint.sh"
    ).read_text()
    lock = tmp_path / ".X99-lock"
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        source.replace(
            "/usr/bin/python3 /usr/local/bin/kodi-lab-xvfb-guard", ":"
        ).replace("/tmp/.X99-lock", str(lock))
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    xvfb = binary / "Xvfb"
    xvfb.write_text(
        "#!/bin/sh\n"
        + (
            "exit 0\n"
            if xvfb_exits
            else f"printf '%10d\\n' \"$$\" > {lock}\nexec sleep 30\n"
        )
    )
    xvfb.chmod(0o755)
    xdpyinfo = binary / "xdpyinfo"
    xdpyinfo.write_text("#!/bin/sh\nexit 0\n")
    xdpyinfo.chmod(0o755)
    dbus = binary / "dbus-run-session"
    dbus.write_text(f"#!/bin/sh\ntouch {tmp_path / 'kodi-launched'}\nexit 0\n")
    dbus.chmod(0o755)
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "PATH": f"{binary}:/usr/bin:/bin",
    }
    result = subprocess.run(
        ["/bin/sh", str(script)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    assert (tmp_path / "kodi-launched").exists() is not xvfb_exits
    assert (result.returncode == 0) is not xvfb_exits

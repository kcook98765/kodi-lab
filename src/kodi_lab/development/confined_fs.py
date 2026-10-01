"""Linux V1 source reads confined to an already-open development root."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
from pathlib import Path

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_OPENAT2_AMD64 = 437
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_SOURCE_RESOLVE = (
    _RESOLVE_NO_XDEV | _RESOLVE_NO_MAGICLINKS | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
)
_SUPPORTED = platform.system() == "Linux" and platform.machine() in {
    "x86_64",
    "AMD64",
}
_SYSCALL = ctypes.CDLL(None, use_errno=True).syscall
_SYSCALL.restype = ctypes.c_long


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


def open_development_root(root: Path) -> int:
    """Open the configured root without following any pathname component.

    The root itself may be a mount point. Mount crossing is forbidden only
    *below* this descriptor, where source locators and tree entries begin.
    """

    if (
        not root.is_absolute()
        or root == Path("/")
        or root != Path(os.path.normpath(root))
    ):
        raise ValueError("development root must be normalized and non-root")
    current_fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def open_source_component(
    directory_fd: int, component: str, flags: int, *, mode: int = 0
) -> int:
    """Open one child without symlink or nested-mount traversal; never fall back."""

    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or "\x00" in component
    ):
        raise OSError(errno.EINVAL, "unsafe development-source component")
    if not _SUPPORTED:
        raise OSError(errno.ENOSYS, "Linux amd64 openat2 is required")
    how = _OpenHow(flags=flags, mode=mode, resolve=_SOURCE_RESOLVE)
    result = _SYSCALL(
        ctypes.c_long(_OPENAT2_AMD64),
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(os.fsencode(component)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), component)
    return int(result)

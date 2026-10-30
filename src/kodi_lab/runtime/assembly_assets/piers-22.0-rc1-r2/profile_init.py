"""Initialize only the fixed managed profile; preserve existing userdata."""

import contextlib
import os
import stat
from pathlib import Path


def initialize() -> Path:
    if (os.geteuid(), os.getegid()) != (1000, 1000):
        raise RuntimeError("managed Kodi requires UID/GID 1000")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    home = os.open("/home/kodi", flags)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(".kodi", mode=0o700, dir_fd=home)
        profile = os.open(".kodi", flags, dir_fd=home)
    finally:
        os.close(home)
    try:
        if os.fstat(profile).st_uid != 1000:
            raise RuntimeError("unexpected profile owner")
        with contextlib.suppress(FileExistsError):
            os.mkdir("userdata", mode=0o700, dir_fd=profile)
        data = os.open("userdata", flags, dir_fd=profile)
        try:
            if os.fstat(data).st_uid != 1000:
                raise RuntimeError("unexpected userdata owner")
            try:
                info = os.stat("guisettings.xml", dir_fd=data, follow_symlinks=False)
            except FileNotFoundError:
                payload = Path("/usr/share/kodi-lab/guisettings.xml").read_bytes()
                fd = os.open(
                    "guisettings.xml",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=data,
                )
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(payload)
                except BaseException:
                    os.unlink("guisettings.xml", dir_fd=data)
                    raise
            else:
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 1000
                    or info.st_nlink != 1
                ):
                    raise RuntimeError("unsafe existing profile settings")
        finally:
            os.close(data)
    finally:
        os.close(profile)
    info = os.lstat("/run/kodi")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000:
        raise RuntimeError("unsafe runtime directory")
    os.chmod("/run/kodi", 0o700, follow_symlinks=False)
    return Path("/home/kodi/.kodi")

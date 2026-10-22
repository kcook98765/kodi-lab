"""Unprivileged test-root ownership and descriptor-bound cleanup (stdlib only)."""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MARKER = ".kodi-lab-run-scratch.json"
PREFIXES = ("klp.", "kodi-lab-check.", "kodi-lab-snapshot-tests.")


@dataclass(frozen=True)
class OwnedScratch:
    path: Path
    parent_identity: tuple[int, int]
    identity: tuple[int, int]
    private_parent: bool = True

    @classmethod
    def capture(
        cls, path: Path, parent: Path, *, private_parent: bool = True
    ) -> OwnedScratch:
        if path.parent != parent or path.is_symlink() or parent.is_symlink():
            raise ValueError("scratch must be an immediate pytest-owned directory")
        root, base = (
            path.stat(follow_symlinks=False),
            parent.stat(follow_symlinks=False),
        )
        if (
            not stat.S_ISDIR(root.st_mode)
            or not stat.S_ISDIR(base.st_mode)
            or root.st_uid != os.getuid()
            or (private_parent and base.st_uid != os.getuid())
            or stat.S_IMODE(root.st_mode) & 0o077
            or (private_parent and stat.S_IMODE(base.st_mode) & 0o077)
        ):
            raise ValueError("scratch ownership or private permissions changed")
        return cls(
            path, (base.st_dev, base.st_ino), (root.st_dev, root.st_ino), private_parent
        )

    def remove(self) -> int:
        """Change directory permissions by handle; never chmod linked files."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parent = os.open(self.path.parent, flags)
        try:
            base = os.fstat(parent)
            if (
                (base.st_dev, base.st_ino) != self.parent_identity
                or (self.private_parent and base.st_uid != os.getuid())
                or (self.private_parent and stat.S_IMODE(base.st_mode) & 0o077)
            ):
                raise ValueError("scratch parent replaced")
            root = os.open(self.path.name, flags, dir_fd=parent)
            try:
                observed = os.fstat(root)
                if (
                    (observed.st_dev, observed.st_ino) != self.identity
                    or observed.st_uid != os.getuid()
                    or stat.S_IMODE(observed.st_mode) & 0o077
                ):
                    raise ValueError("scratch root replaced")
                removed = _clear_directory(root)
            finally:
                os.close(root)
            os.rmdir(self.path.name, dir_fd=parent)
            return removed
        finally:
            os.close(parent)


def _clear_directory(directory: int) -> int:
    observed = os.fstat(directory)
    if observed.st_uid != os.getuid():
        raise ValueError("foreign-owned scratch directory")
    os.fchmod(directory, stat.S_IMODE(observed.st_mode) | 0o700)
    removed = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            before = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
                try:
                    after = os.fstat(child)
                    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError("scratch child replaced")
                    if after.st_dev != observed.st_dev:
                        raise ValueError("scratch contains another filesystem")
                    removed += _clear_directory(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=directory)
            else:
                os.unlink(entry.name, dir_fd=directory)
                if stat.S_ISREG(before.st_mode):
                    removed += before.st_size
    return removed


def process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = raw[raw.rfind(")") + 2 :].split()
    if len(fields) < 20:
        raise ValueError("unknown process identity")
    ticks = int(fields[19])
    if ticks <= 0:
        raise ValueError("unknown process identity")
    return ticks


def encode(owned: OwnedScratch) -> str:
    return json.dumps({**asdict(owned), "path": str(owned.path)}, separators=(",", ":"))


def decode(token: str) -> OwnedScratch:
    document = json.loads(token)
    if (
        not isinstance(document, dict)
        or set(document) != {"path", "parent_identity", "identity", "private_parent"}
        or not isinstance(document["path"], str)
        or not Path(document["path"]).is_absolute()
        or type(document["private_parent"]) is not bool
    ):
        raise ValueError("invalid cleanup ownership token")
    for key in ("parent_identity", "identity"):
        pair = document[key]
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(type(v) is not int or v < 0 for v in pair)
        ):
            raise ValueError("invalid cleanup directory identity")
    return OwnedScratch(
        Path(document["path"]),
        tuple(document["parent_identity"]),
        tuple(document["identity"]),
        document["private_parent"],
    )


def register(path: Path, owner_pid: int) -> OwnedScratch:
    if not path.is_absolute() or not path.name.startswith(PREFIXES):
        raise ValueError("unrecognized test root")
    owned = OwnedScratch.capture(path, path.parent, private_parent=False)
    start = process_start_ticks(owner_pid)
    if start is None:
        raise ValueError("test owner is not live")
    document = {
        "schema": 1,
        "kind": "kodi-lab-run-scratch",
        "uid": os.getuid(),
        "pid": owner_pid,
        "start_ticks": start,
        "created_at": time.time(),
        "ownership": json.loads(encode(owned)),
    }
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = os.open(path, flags)
    try:
        if (os.fstat(root).st_dev, os.fstat(root).st_ino) != owned.identity:
            raise ValueError("test root replaced")
        marker = os.open(
            MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root,
        )
        with os.fdopen(marker, "w") as stream:
            json.dump(document, stream)
    finally:
        os.close(root)
    return owned


def abandoned(path: Path, minimum_age: float) -> OwnedScratch | None:
    """Unknown owner state, malformed markers and replaced roots never delete."""
    owned = OwnedScratch.capture(path, path.parent, private_parent=False)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = os.open(path, flags)
    try:
        if (os.fstat(root).st_dev, os.fstat(root).st_ino) != owned.identity:
            raise ValueError("test root replaced")
        fd = os.open(
            MARKER,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=root,
        )
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > 8192
            ):
                raise ValueError("unsafe test marker")
            document: dict[str, Any] = json.loads(stream.read(8193))
    finally:
        os.close(root)
    if not isinstance(document, dict):
        raise ValueError("invalid test marker")
    recorded = decode(json.dumps(document["ownership"]))
    if (
        document.get("schema") != 1
        or document.get("kind") != "kodi-lab-run-scratch"
        or document.get("uid") != os.getuid()
        or recorded != owned
        or type(document.get("pid")) is not int
        or document["pid"] <= 0
        or type(document.get("start_ticks")) is not int
        or document["start_ticks"] <= 0
        or type(document.get("created_at")) not in {int, float}
        or not math.isfinite(document["created_at"])
        or document["created_at"] < 0
    ):
        raise ValueError("test marker identity mismatch")
    if time.time() - document["created_at"] < minimum_age:
        return None
    if process_start_ticks(document["pid"]) == document["start_ticks"]:
        return None
    return owned


def sweep(parent: Path, minimum_age: float, *, delete: bool) -> tuple[int, int]:
    found = removed = 0
    for path in sorted(parent.iterdir()):
        if not path.name.startswith(PREFIXES) or not (path / MARKER).exists():
            continue
        try:
            candidate = abandoned(path, minimum_age)
            if candidate is None:
                continue
            found += 1
            if delete:
                candidate.remove()
                removed += 1
            print(f"{'removed' if delete else 'dead candidate'}: {path}")
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            print(f"retained unverified test scratch: {path}", file=sys.stderr)
    return found, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "remove"))
    parser.add_argument("value")
    parser.add_argument("--owner-pid", type=int, default=os.getppid())
    args = parser.parse_args()
    try:
        if args.action == "capture":
            print(encode(register(Path(args.value), args.owner_pid)))
        else:
            owned = decode(args.value)
            print(
                f"test scratch removed: {owned.path} ({owned.remove()} logical bytes)"
            )
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        print(
            "test scratch retained: ownership, permissions or identity unavailable",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

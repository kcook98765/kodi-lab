"""Named stopped-profile archives using one file per snapshot."""

import hashlib
import json
import re
import sqlite3
import tarfile
from pathlib import PurePosixPath
from uuid import uuid4

from .runtime import BRIDGE, IMAGE, Refusal, atomic

MAX_PROFILE = 256 * 1024**2
MAX_FILES = 18000


def name_ok(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9 _.-]{0,63}", value
    ):
        raise Refusal("invalid_snapshot_name")
    return value


def excluded(name):
    return name.parts[:1] == ("temp",) or name.parts[:3] == (
        "userdata",
        "addon_data",
        BRIDGE,
    )


def protected(name):
    parts = name.parts
    return (
        len(parts) > 1
        and parts[0] == "addons"
        and (parts[1] == BRIDGE or parts[1].startswith("repository."))
    )


def archive_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_profile(path):
    for folder in path.glob("userdata/**/Database"):
        for file in folder.glob("*.db"):
            if not file.is_file() or file.is_symlink():
                raise Refusal("unsafe_database")
            with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise Refusal("snapshot_database_integrity")


class Snapshots:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = runtime.root / "snapshots"

    def path(self, name):
        return self.root / (name_ok(name) + ".tar.gz")

    def list(self):
        return [
            {"name": path.name.removesuffix(".tar.gz"), "bytes": path.stat().st_size}
            for path in sorted(self.root.glob("*.tar.gz"))
            if path.is_file()
            and not path.is_symlink()
            and path.with_suffix(".json").is_file()
        ]

    def save(self, name):
        self.runtime.require_stopped()
        self.runtime.capacity()
        path = self.path(name)
        if path.exists() or len(self.list()) >= 8:
            raise Refusal("snapshot_exists_or_quota")
        staging = self.root / (".stage-" + uuid4().hex)
        source = self.runtime.profile()
        total = count = 0
        try:
            with tarfile.open(staging, "x:gz", compresslevel=3) as tar:
                for file in sorted(source.rglob("*")):
                    rel = file.relative_to(source)
                    if excluded(rel):
                        continue
                    if (
                        file.is_symlink()
                        or protected(rel)
                        or not (file.is_dir() or file.is_file())
                    ):
                        raise Refusal("unsafe_snapshot_source")
                    info = tar.gettarinfo(file, arcname=rel.as_posix())
                    count += 1
                    total += info.size
                    if (
                        count > MAX_FILES
                        or total > MAX_PROFILE
                        or not (info.isdir() or info.isreg())
                    ):
                        raise Refusal("snapshot_limit_or_links")
                    if info.isreg():
                        with file.open("rb") as stream:
                            tar.addfile(info, stream)
                    else:
                        tar.addfile(info)
            digest = archive_digest(staging)
            staging.replace(path)
            atomic(
                path.with_suffix(".json"),
                json.dumps({"image": IMAGE, "sha256": digest}).encode(),
            )
        finally:
            staging.unlink(missing_ok=True)
        return {
            "name": name,
            "bytes": path.stat().st_size,
            "entries": count,
            "sha256": digest,
        }

    def load(self, name):
        self.runtime.require_stopped()
        path = self.path(name)
        if path.is_symlink() or not path.is_file():
            raise Refusal("snapshot_not_found")
        meta = path.with_suffix(".json")
        if not meta.is_file() or meta.is_symlink() or meta.stat().st_size > 1024:
            raise Refusal("snapshot_metadata_invalid")
        if json.loads(meta.read_text()) != {
            "image": IMAGE,
            "sha256": archive_digest(path),
        }:
            raise Refusal("snapshot_identity_mismatch")
        slot = self.runtime.stage(clean=False)
        stage = self.runtime.profile(slot)
        seen = set()
        size = 0
        try:
            with tarfile.open(path, "r|gz") as tar:
                for item in tar:
                    posix = PurePosixPath(item.name)
                    size += item.size
                    if (
                        posix.is_absolute()
                        or str(posix) != item.name
                        or ".." in posix.parts
                        or not posix.parts
                        or len(item.name) > 240
                        or posix in seen
                        or not (item.isdir() or item.isreg())
                        or excluded(posix)
                        or protected(posix)
                        or size > MAX_PROFILE
                        or len(seen) >= MAX_FILES
                    ):
                        raise Refusal("unsafe_snapshot_archive")
                    seen.add(posix)
                    tar.extract(item, stage, filter="data")
            if not (stage / "userdata/guisettings.xml").is_file():
                raise Refusal("snapshot_missing_profile")
            validate_profile(stage)
            self.runtime.exchange(slot)
        except Exception:
            if self.runtime.read_pointer() != slot:
                self.runtime.retire(slot)
            raise
        return {"name": name, "state": "restored", "entries": len(seen)}

    def delete(self, name):
        path = self.path(name)
        if not path.is_file() or path.is_symlink():
            raise Refusal("snapshot_not_found")
        path.unlink()
        path.with_suffix(".json").unlink(missing_ok=True)
        return {"name": name, "state": "deleted"}

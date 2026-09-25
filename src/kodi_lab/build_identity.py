"""Deterministic runtime-content identity while the package version is 0.0.0."""

from __future__ import annotations

import hashlib
from importlib.metadata import version
from pathlib import Path


def package_sha256(package_root: Path | None = None) -> str:
    root = package_root or Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def installed_identity() -> str:
    return f"Kodi Lab {version('kodi-lab')} package-sha256={package_sha256()}"

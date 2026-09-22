#!/usr/bin/env python3
"""Build the deterministic Kodi Lab narrow-bridge review archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, cast

BUILDER_ID = "kodi-lab-narrow-bridge-zip-v1"
NORMALIZED_TIMESTAMP = (2026, 9, 21, 0, 0, 0)


def package_entries(addon_root: Path) -> tuple[str, ...]:
    """Return the closed, ordered archive inventory below the add-on root."""

    entries: list[str] = []
    for path in addon_root.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"unsupported bridge package entry: {path}")
        relative = path.relative_to(addon_root).as_posix()
        entries.append(relative + ("/" if path.is_dir() else ""))
    return ("", *sorted(entries))


def package_identity(addon_root: Path) -> tuple[str, str]:
    descriptor = ET.parse(addon_root / "addon.xml").getroot()
    return descriptor.attrib["id"], descriptor.attrib["version"]


def build_archive(addon_root: Path, output: Path) -> str:
    addon_id, _version = package_identity(addon_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for relative in package_entries(addon_root):
            info = zipfile.ZipInfo(f"{addon_id}/{relative}", NORMALIZED_TIMESTAMP)
            info.create_system = 3
            if relative == "" or relative.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
                info.compress_type = zipfile.ZIP_STORED
                content = b""
            else:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                cast(Any, info)._compresslevel = 9
                content = (addon_root / relative).read_bytes()
            archive.writestr(info, content)
    os.chmod(output, 0o644)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    default_root = repository / "bridge" / "service.kodi_lab_bridge"
    addon_id, version = package_identity(default_root)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_root)
    parser.add_argument(
        "--output",
        type=Path,
        default=repository / "build" / "bridge" / f"{addon_id}-{version}.zip",
    )
    arguments = parser.parse_args()
    digest = build_archive(arguments.source, arguments.output)
    result: dict[str, Any] = {
        "builder": BUILDER_ID,
        "output": str(arguments.output.resolve()),
        "sha256": digest,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

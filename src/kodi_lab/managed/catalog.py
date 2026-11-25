"""Operator-only batch intake of reviewed managed dependency archives."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .packages import MAX_ZIP, Package, Packages, read_zip
from .runtime import Refusal, Runtime, atomic

CATALOG_ROOT = Path("/var/lib/kodi-lab-managed")
MAX_BATCH_BYTES = 128 * 1024**2


def approve_batch(
    root: Path, archives: list[Path], *, replace_versions: bool
) -> list[dict[str, str]]:
    if not 0 < len(archives) <= 64:
        raise Refusal("invalid_catalog_batch")
    approved: dict[str, Package] = {}
    total = 0
    for path in archives:
        if path.suffix != ".zip" or path.is_symlink() or not path.is_file():
            raise Refusal("invalid_catalog_archive")
        with path.open("rb") as stream:
            data = stream.read(MAX_ZIP + 1)
        total += len(data)
        if total > MAX_BATCH_BYTES:
            raise Refusal("invalid_catalog_batch")
        package = read_zip(data)
        if package.id in approved:
            raise Refusal("duplicate_catalog_addon")
        approved[package.id] = package
    runtime = Runtime(root)
    with runtime.exclusive():
        store = Packages(runtime.root, runtime.feed)
        existing = store.catalog()
        for addon, package in approved.items():
            previous = existing.get(addon)
            if previous is None or previous.digest == package.digest:
                continue
            if not replace_versions or previous.version == package.version:
                raise Refusal("catalog_version_conflict")
        changed = {
            addon: package
            for addon, package in approved.items()
            if addon not in existing or existing[addon].digest != package.digest
        }
        for package in changed.values():
            atomic(store.approved / (package.digest + ".zip"), package.data)
        if changed:
            selected = existing | changed
            index = [selected[addon].digest for addon in sorted(selected)]
            atomic(store.catalog_file, (json.dumps(index) + "\n").encode())
    return [
        {"addon": pkg.id, "version": pkg.version, "sha256": pkg.digest}
        for pkg in sorted(approved.values(), key=lambda pkg: pkg.id)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replace-versions",
        action="store_true",
        help="Explicitly change the approved version of an existing add-on.",
    )
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        receipt = approve_batch(
            CATALOG_ROOT, args.archives, replace_versions=args.replace_versions
        )
    except (OSError, Refusal) as error:
        parser.exit(1, f"Managed catalog approval refused: {error}\n")
    print(json.dumps(receipt))

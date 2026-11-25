"""Operator-approved public repository inputs for the disposable Omega test."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from .packages import HEX, MAX_ZIP, Package, read_zip, sha
from .protocol import Refusal
from .runtime import Runtime, atomic

HOST = "kcook98765.github.io"
BASE = f"https://{HOST}/kcook-lab-repo/"
REPOSITORY = "repository.ai.kodi"
CATEGORY = "xbmc.addon.repository"
CATALOG_LIMIT = 512 * 1024
LANES = {
    "matrix": ("19.0.0", "19.9.999"),
    "nexus": ("20.0.0", "20.9.999"),
    "omega": ("21.0.0", "21.9.999"),
    "piers": ("21.90.0", "22.9.999"),
}


def passive_repository(data: bytes) -> Package:
    package = read_zip(data, repository=True)
    node = package.node
    extensions = node.findall("extension")
    repo = node.find(f"extension[@point='{CATEGORY}']")
    if (
        package.id != REPOSITORY
        or package.category != CATEGORY
        or repo is None
        or len(extensions) != 2
        or extensions[1].get("point") != "xbmc.addon.metadata"
        or set(package.members)
        - {
            "addon.xml",
            "LICENSE.txt",
            "resources/icon.png",
            "resources/ICON-LICENSE.txt",
        }
        or [item.attrib for item in node.findall("requires/import")]
        != [{"addon": "xbmc.addon", "version": "19.0.0"}]
        or len(repo) != len(LANES)
    ):
        raise Refusal("public_repository_manifest")
    for directory, (lane, versions) in zip(repo, LANES.items(), strict=True):
        expected = {
            "info": (BASE + f"catalogs/{lane}/addons.xml", {}),
            "checksum": (
                BASE + f"catalogs/{lane}/addons.xml.sha256",
                {"verify": "sha256"},
            ),
            "datadir": (BASE + "packages/", {}),
            "artdir": (BASE + f"artwork/{lane}/", {}),
            "hashes": ("sha256", {}),
        }
        if (
            directory.tag != "dir"
            or directory.attrib
            != dict(zip(("minversion", "maxversion"), versions, strict=True))
            or len(directory) != len(expected)
            or any(len(child) for child in directory)
            or {child.tag: (child.text, child.attrib) for child in directory}
            != expected
        ):
            raise Refusal("public_repository_origin")
    return package


@dataclass(frozen=True)
class Approval:
    repository: Package
    target: Package
    checksum: str

    def info(self) -> dict[str, str]:
        return {
            "repository": self.repository.id,
            "repository_version": self.repository.version,
            "repository_digest": self.repository.digest,
            "origin": BASE,
            "addon": self.target.id,
            "version": self.target.version,
            "digest": self.target.digest,
            "checksum": self.checksum,
        }


def check_inputs(installer: bytes, addon: bytes, index: bytes) -> Approval:
    repo, target = passive_repository(installer), read_zip(addon)
    if (
        len(index) > CATALOG_LIMIT
        or b"<!DOCTYPE" in index.upper()
        or b"<!ENTITY" in index.upper()
    ):
        raise Refusal("public_catalog_invalid")
    try:
        catalog = ET.fromstring(index)
    except ET.ParseError as error:
        raise Refusal("public_catalog_invalid") from error
    entries = [child for child in catalog if child.get("id") == target.id]
    if (
        catalog.tag != "addons"
        or len(entries) != 1
        or ET.canonicalize(ET.tostring(entries[0]), strip_text=True)
        != ET.canonicalize(ET.tostring(target.node), strip_text=True)
        or target.category
        not in {"xbmc.python.script", "xbmc.python.pluginsource", "xbmc.gui.skin"}
    ):
        raise Refusal("public_catalog_package_mismatch")
    return Approval(repo, target, sha(index))


def approval(root: Path) -> Approval | None:
    config = root / "public-approval.json"
    if not config.exists():
        return None
    record = json.loads(config.read_text())
    result = check_inputs(
        *(
            stored(root, record[key])
            for key in ("repository_digest", "digest", "checksum")
        )
    )
    if result.info() != record:
        raise Refusal("public_approval_mismatch")
    return result


def stored(root: Path, digest: str) -> bytes:
    if not HEX.fullmatch(digest):
        raise Refusal("public_approval_mismatch")
    path = root / "public-approved" / digest
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ZIP:
        raise Refusal("public_approval_mismatch")
    data = path.read_bytes()
    if sha(data) != digest:
        raise Refusal("public_approval_mismatch")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="action", required=True)
    add = command.add_parser("approve")
    add.add_argument("installer", type=Path)
    add.add_argument("target", type=Path)
    add.add_argument("catalog", type=Path)
    command.add_parser("inspect")
    command.add_parser("revoke")
    args = parser.parse_args()
    runtime = Runtime(Path("/var/lib/kodi-lab-managed"))
    root = runtime.root
    config = root / "public-approval.json"
    try:
        with runtime.exclusive():
            if args.action == "inspect":
                selected = approval(root)
            else:
                if (root / "public-test").exists():
                    raise Refusal("stop_public_test_first")
                if args.action == "revoke":
                    config.unlink(missing_ok=True)
                    selected = None
                else:
                    data = []
                    for path, limit in (
                        (args.installer, 128 * 1024),
                        (args.target, MAX_ZIP),
                        (args.catalog, CATALOG_LIMIT),
                    ):
                        if path.is_symlink() or not path.is_file():
                            raise Refusal("public_approval_input")
                        with path.open("rb") as stream:
                            data.append(stream.read(limit + 1))
                        if len(data[-1]) > limit:
                            raise Refusal("public_approval_input")
                    selected = check_inputs(*data)
                    directory = root / "public-approved"
                    directory.mkdir(mode=0o700, exist_ok=True)
                    for content in data:
                        atomic(directory / sha(content), content)
                    atomic(config, json.dumps(selected.info()).encode())
            print(json.dumps(selected.info() if selected else None))
    except (OSError, Refusal) as error:
        parser.exit(1, f"Public repository approval refused: {error}\n")

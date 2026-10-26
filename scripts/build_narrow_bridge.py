#!/usr/bin/env python3
"""Build the deterministic Kodi Lab narrow-bridge review archive."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, cast

BUILDER_ID = "kodi-lab-narrow-bridge-zip-v2"
NORMALIZED_TIMESTAMP = (2026, 9, 21, 0, 0, 0)
ADDON_ID = "service.kodi_lab_bridge"
ADDON_VERSION = "1.1.27"
PROTOCOL = "1"
BOOTSTRAP = "otp-1"
FINGERPRINT_INPUT = (
    b"kodi-lab-narrow-bridge|1.1.27|protocol-1|identity,capabilities,"
    b"gui-screenshot,workspace-deployment,otp-bootstrap-v1,local-pairing-reset,protected-ui-observation,portable-direct-install,bounded-log-read,managed-manifest-launch,partial-execute-modes"
)
SEMANTIC_FINGERPRINT = hashlib.sha256(FINGERPRINT_INPUT).hexdigest()
RELEASE_INVENTORY = (
    "LICENSE",
    "README.md",
    "addon.xml",
    "narrow_bridge.py",
    "ui_observation.py",
    "onboarding.py",
    "pairing_ui.py",
    "resources/language/resource.language.en_gb/strings.po",
    "resources/settings.xml",
    "service.py",
)


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def source_constant(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError(f"bridge source constant missing: {name}")


def package_entries(addon_root: Path) -> tuple[str, ...]:
    """Use only the reviewed files; incidental runtime files never enter the ZIP."""

    directories: set[str] = set()
    for relative in RELEASE_INVENTORY:
        path = addon_root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or unsafe bridge release file: {relative}")
        parent = Path(relative).parent
        while parent != Path("."):
            directory = addon_root / parent
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"unsafe bridge release directory: {parent}")
            directories.add(parent.as_posix() + "/")
            parent = parent.parent
    return ("", *sorted((*directories, *RELEASE_INVENTORY)))


def package_identity(addon_root: Path) -> tuple[str, str]:
    descriptor = ET.parse(addon_root / "addon.xml").getroot()
    identity = descriptor.attrib["id"], descriptor.attrib["version"]
    if identity != (ADDON_ID, ADDON_VERSION):
        raise ValueError("bridge add-on identity differs from reviewed release")
    if (
        descriptor.findtext("./extension[@point='xbmc.addon.metadata']/license")
        != "MIT"
    ):
        raise ValueError("bridge license identity differs")
    if (
        descriptor.find("./requires/import[@addon='xbmc.python'][@version='3.0.0']")
        is None
    ):
        raise ValueError("bridge Python dependency differs")
    if (
        descriptor.find("./extension[@point='xbmc.service'][@library='service.py']")
        is None
    ):
        raise ValueError("bridge service entrypoint differs")
    if (
        source_constant(addon_root / "narrow_bridge.py", "PROTOCOL") != PROTOCOL
        or source_constant(addon_root / "narrow_bridge.py", "ADDON_ID") != ADDON_ID
        or source_constant(addon_root / "narrow_bridge.py", "ADDON_VERSION")
        != ADDON_VERSION
        or source_constant(addon_root / "onboarding.py", "BOOTSTRAP_VERSION")
        != BOOTSTRAP
        or FINGERPRINT_INPUT not in (addon_root / "narrow_bridge.py").read_bytes()
    ):
        raise ValueError("bridge source protocol identity differs")
    return identity


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


def provenance(addon_root: Path, archive: Path, repository: Path) -> dict[str, Any]:
    """Bind exact committed source, builder bytes, and reviewed ZIP members."""

    package_identity(addon_root)
    package_entries(addon_root)
    tracked = subprocess.check_output(
        ["git", "ls-files", "--", "bridge/service.kodi_lab_bridge"],
        cwd=repository,
        text=True,
    ).splitlines()
    if set(tracked) != {
        f"bridge/service.kodi_lab_bridge/{name}" for name in RELEASE_INVENTORY
    }:
        raise ValueError("tracked bridge release inventory differs")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
    ).strip()
    bridge_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD:bridge/service.kodi_lab_bridge"],
        cwd=repository,
        text=True,
    ).strip()
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repository, text=True
    ).strip():
        raise ValueError("bridge provenance requires a clean committed source")
    inventory = [
        {
            "path": name,
            "sha256": hashlib.sha256((addon_root / name).read_bytes()).hexdigest(),
            "size": (addon_root / name).stat().st_size,
        }
        for name in RELEASE_INVENTORY
    ]
    return {
        "schema": "kodi-lab-bridge-artifact-v1",
        "addon_id": ADDON_ID,
        "version": ADDON_VERSION,
        "protocol": PROTOCOL,
        "bootstrap": BOOTSTRAP,
        "license": "MIT",
        "semantic_fingerprint": SEMANTIC_FINGERPRINT,
        "capabilities": [
            "screenshot",
            "workspace_deployment",
            "ui_observation",
            "log_read",
        ],
        "source_commit": commit,
        "source_tree": tree,
        "bridge_tree": bridge_tree,
        "builder": BUILDER_ID,
        "builder_sha256": hashlib.sha256(
            (repository / "scripts/build_narrow_bridge.py").read_bytes()
        ).hexdigest(),
        "filename": archive.name,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "inventory": inventory,
        "inventory_sha256": hashlib.sha256(canonical(inventory)).hexdigest(),
    }


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    default_root = repository / "bridge" / "service.kodi_lab_bridge"
    addon_id, version = package_identity(default_root)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_root)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=repository / "build" / "bridge" / f"{addon_id}-{version}.zip",
    )
    arguments = parser.parse_args()
    digest = build_archive(arguments.source, arguments.output)
    if arguments.provenance is not None:
        arguments.provenance.parent.mkdir(parents=True, exist_ok=True)
        arguments.provenance.write_bytes(
            canonical(provenance(arguments.source, arguments.output, repository))
        )
    result: dict[str, Any] = {
        "builder": BUILDER_ID,
        "output": str(arguments.output.resolve()),
        "sha256": digest,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

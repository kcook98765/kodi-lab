"""Reviewed RC1 Bridge text overlay; the stable Bridge source stays unchanged."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

_INVENTORY = (
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
_FRAGMENT_SHA256 = "7f95447d72a99e2688d638c715209a2b3afce90d4174f4e6eef404f0a1adc672"
RC1_ARCHIVE_SHA256 = "f565cbe885d26a6f1da7a25c4952798edf13e607d9921c89eefbc0c78cb68fff"


def packaged_rc1_bridge_sources() -> dict[str, bytes]:
    """Closed wheel-owned candidate, never a caller-selected ZIP or source tree."""
    from tempfile import TemporaryDirectory

    resource = files("kodi_lab.kodi").joinpath("bridge_source")
    if not resource.is_dir():
        # Source-checkout equivalent of the wheel force-include; still fixed,
        # never supplied by a caller, and verified against the same ZIP digest.
        repository = Path(__file__).resolve().parents[3]
        if not (repository / "pyproject.toml").is_file():
            raise ValueError("Reviewed Bridge source resource unavailable")
        resource = repository / "bridge/service.kodi_lab_bridge"
    with TemporaryDirectory(prefix="kodi-lab-reviewed-bridge-") as temporary:
        root = Path(temporary)
        for name in _INVENTORY:
            data = resource.joinpath(name).read_bytes()
            if len(data) > 1048576:
                raise ValueError("Bridge source exceeds bounds")
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        archive = root / "candidate.zip"
        if build_rc1_bridge_archive(root, archive) != RC1_ARCHIVE_SHA256:
            raise ValueError("Packaged Bridge candidate differs from review")
        return reviewed_rc1_bridge_sources(root)


def reviewed_rc1_bridge_sources(legacy_root: Path) -> dict[str, bytes]:
    """Generate only reviewed bytes from the existing approved stable source."""
    package = files("kodi_lab.kodi")
    legacy = json.loads(package.joinpath("managed_bridge_build.json").read_text())
    preview = json.loads(
        package.joinpath("managed_bridge_preview_build.json").read_text()
    )
    fragment = package.joinpath("rc1_bridge_identity.txt").read_bytes()
    if hashlib.sha256(fragment).hexdigest() != _FRAGMENT_SHA256:
        raise ValueError("RC1 Bridge overlay differs from review")
    if legacy_root.is_symlink() or not legacy_root.is_dir():
        raise ValueError("Bridge source root is unsafe")
    result: dict[str, bytes] = {}
    for name in _INVENTORY:
        path = legacy_root / name
        if (
            any(parent.is_symlink() for parent in path.parents)
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError("Bridge source path is unsafe")
        if path.stat().st_size > 1048576:
            raise ValueError("Bridge source exceeds bounds")
        data = path.read_bytes()
        if (
            name in legacy["files"]
            and hashlib.sha256(data).hexdigest() != legacy["files"][name]
        ):
            raise ValueError("Stable Bridge source differs from review")
        result[name] = data
    result["addon.xml"] = result["addon.xml"].replace(
        b'version="1.1.31"', b'version="1.1.32"'
    )
    source = result["narrow_bridge.py"]
    source = source.replace(b'ADDON_VERSION = "1.1.31"', b'ADDON_VERSION = "1.1.32"')
    source = source.replace(b"|1.1.31|protocol-1|", b"|1.1.32|protocol-1|")
    source = source.replace(
        b',managed-manifest-launch,partial-execute-modes,managed-addon-settings-persistent"',
        b',managed-manifest-launch,partial-execute-modes,managed-addon-settings-persistent,exact-release"',
    )
    start = source.index(b"def _identity(state):")
    end = source.index(b"def _png_chunk(kind, data):")
    # Keep function separation in assembly, not as empty lines at fragment EOF.
    result["narrow_bridge.py"] = source[:start] + fragment + b"\n\n" + source[end:]
    for name, digest in preview["files"].items():
        if hashlib.sha256(result[name]).hexdigest() != digest:
            raise ValueError("Generated RC1 Bridge source differs from review")
    return result


def build_rc1_bridge_archive(legacy_root: Path, output: Path) -> str:
    """Build a deterministic candidate; this does not publish release provenance."""
    sources = reviewed_rc1_bridge_sources(legacy_root)
    directories = {""}
    for name in sources:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix() + "/")
            parent = parent.parent
    entries = ("", *sorted((directories - {""}) | set(sources)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w") as archive:
        for name in entries:
            info = zipfile.ZipInfo(
                "service.kodi_lab_bridge/" + name, (2026, 9, 21, 0, 0, 0)
            )
            info.create_system = 3
            if name == "" or name.endswith("/"):
                info.external_attr = (0o40755 << 16) | 0x10
                info.compress_type = zipfile.ZIP_STORED
                data = b""
            else:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                cast(Any, info)._compresslevel = 9
                data = sources[name]
            archive.writestr(info, data)
    os.chmod(output, 0o644)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def packaged_stable_bridge_sources() -> dict[str, bytes]:
    """Exact wheel-owned stable release bytes for an explicit managed upgrade."""
    resource = files("kodi_lab.kodi").joinpath("bridge_source")
    if not resource.is_dir():
        resource = (
            Path(__file__).resolve().parents[3] / "bridge/service.kodi_lab_bridge"
        )
    inventory = json.loads(
        files("kodi_lab.kodi")
        .joinpath("managed_bridge_release_inventory.json")
        .read_text()
    )
    result: dict[str, bytes] = {}
    for name in _INVENTORY:
        data = resource.joinpath(name).read_bytes()
        if len(data) > 1048576 or hashlib.sha256(data).hexdigest() != inventory[name]:
            raise ValueError("Packaged stable Bridge differs from review")
        result[name] = data
    return result

"""Bridge 1.1.13 release ZIP has a closed, reproducible source inventory."""

from __future__ import annotations

import hashlib
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from kodi_lab.kodi.bridge import (
    BOOTSTRAP_PROTOCOL_VERSION,
    BRIDGE_ADDON_ID,
    BRIDGE_PROTOCOL_VERSION,
    SUPPORTED_V1_BRIDGE_ADDON_VERSION,
)
from scripts.build_narrow_bridge import (
    ADDON_ID,
    ADDON_VERSION,
    BOOTSTRAP,
    BUILDER_ID,
    PROTOCOL,
    RELEASE_INVENTORY,
    build_archive,
    package_entries,
    package_identity,
)

ROOT = Path(__file__).parents[2]
ADDON = ROOT / "bridge/service.kodi_lab_bridge"


def test_bridge_source_identity_matches_application_contract() -> None:
    descriptor = ET.parse(ADDON / "addon.xml").getroot()
    assert package_identity(ADDON) == (ADDON_ID, ADDON_VERSION)
    assert (ADDON_ID, ADDON_VERSION, PROTOCOL, BOOTSTRAP) == (
        BRIDGE_ADDON_ID,
        SUPPORTED_V1_BRIDGE_ADDON_VERSION,
        BRIDGE_PROTOCOL_VERSION,
        BOOTSTRAP_PROTOCOL_VERSION,
    )
    assert (
        descriptor.find("./requires/import[@addon='xbmc.python'][@version='3.0.0']")
        is not None
    )
    assert (
        descriptor.find("./extension[@point='xbmc.service'][@library='service.py']")
        is not None
    )
    source = (ADDON / "narrow_bridge.py").read_text()
    assert 'if document.get("operation") == "pair_bridge":' in source
    assert '"pair_bridge"' not in source.split("OPERATIONS = {", 1)[1].split("}", 1)[0]
    assert "MIT License" in (ADDON / "LICENSE").read_text()


def test_closed_inventory_excludes_unreviewed_and_runtime_state(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(ADDON, source)
    for relative in (
        "__pycache__/service.cpython-312.pyc",
        "credentials.json",
        "onboarding.json",
        "state.json",
        "capture.png",
        "bridge.log",
        ".DS_Store",
        ".idea/workspace.xml",
        "output/junit.xml",
        "unknown.py",
        "temp.swp",
    ):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"secret or unreviewed bytes")
    assert tuple(
        name for name in package_entries(source) if name and not name.endswith("/")
    ) == tuple(sorted(RELEASE_INVENTORY))
    clean = tmp_path / "clean.zip"
    dirty = tmp_path / "dirty.zip"
    assert build_archive(ADDON, clean) == build_archive(source, dirty)
    assert clean.read_bytes() == dirty.read_bytes()
    with zipfile.ZipFile(dirty) as archive:
        members = archive.namelist()
        assert members[0] == "service.kodi_lab_bridge/"
        assert all(name.startswith("service.kodi_lab_bridge/") for name in members)
        assert len(members) == 1 + len(RELEASE_INVENTORY) + 3
        assert all(
            not any(
                bad in name
                for bad in ("__pycache__", ".pyc", ".idea", "unknown.py", "state.json")
            )
            for name in members
        )
        assert (
            hashlib.sha256(archive.read("service.kodi_lab_bridge/LICENSE")).digest()
            == hashlib.sha256((ADDON / "LICENSE").read_bytes()).digest()
        )
    assert BUILDER_ID == "kodi-lab-narrow-bridge-zip-v2"

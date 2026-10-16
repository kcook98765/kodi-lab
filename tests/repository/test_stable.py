"""Stable feed determinism, reviewed identity and bounded ZIP admission."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from kodi_lab.repository.stable import (
    BRIDGE_SHA256,
    REPOSITORY_ID,
    bootstrap_zip,
    canonical,
    generate,
    inspect_addon_zip,
    load_current,
    sha256,
    validate_config,
)
from scripts.build_narrow_bridge import build_archive

ROOT = Path(__file__).parents[2]
BASE = "http://192.168.50.10:18120"


def candidate(tmp_path: Path) -> Path:
    bundle = tmp_path / "candidate"
    (bundle / "bridge").mkdir(parents=True)
    (bundle / "repository").mkdir()
    bridge = bundle / "bridge/service.kodi_lab_bridge-1.1.17.zip"
    build_archive(ROOT / "bridge/service.kodi_lab_bridge", bridge)
    assert sha256(bridge.read_bytes()) == BRIDGE_SHA256
    stable = ROOT / "repository/STABLE.json"
    (bundle / "repository/STABLE.json").write_bytes(stable.read_bytes())
    provenance = {
        "schema": "kodi-lab-bridge-artifact-v1",
        "addon_id": "service.kodi_lab_bridge",
        "version": "1.1.17",
        "sha256": BRIDGE_SHA256,
        "semantic_fingerprint": (
            "bcc6526c379f09b52f14cad3a582aafb227eafc309cb66b8387dcec057462a37"
        ),
        "source_commit": "c" * 40,
        "source_tree": "a" * 40,
        "filename": bridge.name,
        "protocol": "1",
        "bootstrap": "otp-1",
        "builder": "kodi-lab-narrow-bridge-zip-v2",
        "capabilities": ["screenshot", "workspace_deployment", "ui_observation"],
    }
    (bundle / "bridge/BRIDGE.json").write_bytes(canonical(provenance))
    manifest = {
        "schema": "kodi-lab-linux-v1-bundle-v3",
        "source_commit": "c" * 40,
        "source_tree": "a" * 40,
        "bridge": {
            "zip": "bridge/service.kodi_lab_bridge-1.1.17.zip",
            "provenance": "bridge/BRIDGE.json",
        },
        "files": {
            "repository/STABLE.json": {
                "sha256": "sha256:" + sha256(stable.read_bytes()),
                "size": stable.stat().st_size,
            },
            "bridge/service.kodi_lab_bridge-1.1.17.zip": {
                "sha256": "sha256:" + BRIDGE_SHA256,
                "size": bridge.stat().st_size,
            },
            "bridge/BRIDGE.json": {
                "sha256": "sha256:"
                + sha256((bundle / "bridge/BRIDGE.json").read_bytes()),
                "size": (bundle / "bridge/BRIDGE.json").stat().st_size,
            },
        },
    }
    (bundle / "BUNDLE.json").write_bytes(canonical(manifest))
    return bundle


def archive_with(entries: list[tuple[str, bytes, int]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, content)
    return output.getvalue()


@pytest.fixture
def valid_entries() -> list[tuple[str, bytes, int]]:
    return [
        ("test.addon/", b"", stat.S_IFDIR | 0o755),
        (
            "test.addon/addon.xml",
            b'<addon id="test.addon" version="1.0.0" name="Test"/>',
            stat.S_IFREG | 0o644,
        ),
    ]


def test_generation_is_deterministic_and_restarts(tmp_path: Path) -> None:
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    first = generate(bundle, data, BASE)
    root, observed = load_current(data)
    assert first == observed
    assert first["bootstrap_zip_sha256"] == sha256(bootstrap_zip(BASE))
    assert (
        sha256((root / "stable/addons.xml").read_bytes()) == first["addons_xml_sha256"]
    )
    assert b"service.kodi_lab_bridge" in (root / "stable/addons.xml").read_bytes()
    assert b"<hashes>sha256</hashes>" in (root / "stable/addons.xml").read_bytes()
    assert generate(bundle, data, BASE) == first
    assert len(list((data / "generations").iterdir())) == 1
    assert stat.S_IMODE((data / "CURRENT").stat().st_mode) == 0o644
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert (
        stat.S_IMODE(
            (
                data
                / "packages/service.kodi_lab_bridge/service.kodi_lab_bridge-1.1.17.zip"
            )
            .stat()
            .st_mode
        )
        == 0o644
    )
    with pytest.raises(ValueError, match="another origin"):
        generate(bundle, data, "http://192.168.50.11:18120")
    assert load_current(data)[1] == first


def test_same_version_different_bytes_fails_closed(tmp_path: Path) -> None:
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    generate(bundle, data, BASE)
    package = data / "packages/repository.kodi-lab/repository.kodi-lab-1.0.0.zip"
    package.write_bytes(bootstrap_zip("http://192.168.50.11:18120"))
    with pytest.raises(ValueError, match="package bytes differ"):
        generate(bundle, data, BASE)
    with pytest.raises(ValueError, match="package bytes differ"):
        load_current(data)


def test_bridge_provenance_must_match_candidate_manifest(tmp_path: Path) -> None:
    bundle = candidate(tmp_path)
    provenance_path = bundle / "bridge/BRIDGE.json"
    provenance_path.write_bytes(provenance_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="reviewed bridge provenance"):
        generate(bundle, tmp_path / "repository-data", BASE)


def test_incomplete_generation_and_bad_checksum_never_served(tmp_path: Path) -> None:
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    generate(bundle, data, BASE)
    root, _ = load_current(data)
    (root / "stable/addons.xml.sha256").write_text("0" * 64 + "\n")
    with pytest.raises(ValueError, match="checksum"):
        load_current(data)
    (data / "CURRENT").write_text("f" * 64 + "\n")
    with pytest.raises(OSError):
        load_current(data)


def test_tampered_install_zip_is_rejected_even_with_matching_provenance_hash(
    tmp_path: Path,
) -> None:
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    generate(bundle, data, BASE)
    root, provenance = load_current(data)
    changed = bootstrap_zip("http://192.168.50.11:18120")
    (root / "install/repository.kodi-lab-1.0.0.zip").write_bytes(changed)
    provenance["bootstrap_zip_sha256"] = sha256(changed)
    (root / "PROVENANCE.json").write_bytes(canonical(provenance))
    with pytest.raises(ValueError, match="bootstrap bytes differ"):
        load_current(data)


@pytest.mark.parametrize("bad", ["0.0.0.0", "127.0.0.1", "8.8.8.8"])
def test_bind_requires_private_lan(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_config(bad, 18120, BASE)


@pytest.mark.parametrize(
    "base",
    [
        "http://user:secret@192.168.50.10:18120",
        "http://192.168.50.10:18120/path",
        "http://127.0.0.1:18120",
    ],
)
def test_base_url_rejects_credentials_path_and_loopback(base: str) -> None:
    with pytest.raises(ValueError):
        validate_config("192.168.50.10", 18120, base)


def test_zip_rejects_duplicate_traversal_link_and_wrong_id(
    valid_entries: list[tuple[str, bytes, int]],
) -> None:
    cases = [
        [*valid_entries, valid_entries[-1]],
        [*valid_entries, ("test.addon/../escape", b"x", stat.S_IFREG | 0o644)],
        [*valid_entries, ("test.addon/link", b"target", stat.S_IFLNK | 0o777)],
    ]
    for entries in cases:
        with pytest.raises(ValueError):
            inspect_addon_zip(archive_with(entries), "test.addon", "1.0.0")
    with pytest.raises(ValueError, match="root/descriptor"):
        inspect_addon_zip(archive_with(valid_entries), REPOSITORY_ID, "1.0.0")


def test_zip_rejects_dtd_missing_asset_and_size(
    valid_entries: list[tuple[str, bytes, int]],
) -> None:
    dtd = [
        valid_entries[0],
        (
            valid_entries[1][0],
            (
                b'<!DOCTYPE addon [<!ENTITY x "x">]>'
                b'<addon id="test.addon" version="1.0.0"/>'
            ),
            valid_entries[1][2],
        ),
    ]
    with pytest.raises(ValueError, match="unsafe"):
        inspect_addon_zip(archive_with(dtd), "test.addon", "1.0.0")
    missing_asset = [
        valid_entries[0],
        (
            valid_entries[1][0],
            (
                b'<addon id="test.addon" version="1.0.0">'
                b'<extension point="xbmc.addon.metadata"><assets><icon>icon.png</icon>'
                b"</assets></extension></addon>"
            ),
            valid_entries[1][2],
        ),
    ]
    with pytest.raises(ValueError, match="asset"):
        inspect_addon_zip(archive_with(missing_asset), "test.addon", "1.0.0")
    oversize = [
        *valid_entries,
        ("test.addon/huge.bin", b"a" * (8 * 1024 * 1024 + 1), stat.S_IFREG | 0o644),
    ]
    with pytest.raises(ValueError, match="size"):
        inspect_addon_zip(archive_with(oversize), "test.addon", "1.0.0")

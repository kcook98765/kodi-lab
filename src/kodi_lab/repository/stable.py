"""Deterministic, reviewed stable Kodi feed generation and verification."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REPOSITORY_ID = "repository.kodi-lab"
REPOSITORY_VERSION = "1.0.0"
BRIDGE_ID = "service.kodi_lab_bridge"
BRIDGE_VERSION = "1.1.13"
BRIDGE_SHA256 = "665b15cb6597f6a8e40b3cbdfc29606f453f567d49b6877a1ca2ee704c90f5f3"
BRIDGE_FINGERPRINT = "764d42dc3d86dd0d46452c14661f2dd771f9cba9542b8cfc7ab8886c56ad4e33"
GENERATOR_ID = "kodi-lab-stable-repository-v1"
ZIP_DATE = (2026, 9, 29, 0, 0, 0)
ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9_.-]*)\Z")
HEX_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_FILES = 256
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
PRIVATE_LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_config(bind: str, port: int, base_url: str) -> str:
    """Require an explicit private interface and unambiguous public base URL."""
    try:
        address = ipaddress.ip_address(bind)
    except ValueError as error:
        raise ValueError("repository bind must be a private LAN IP literal") from error
    if (
        address.version != 4
        or not any(address in network for network in PRIVATE_LAN_NETWORKS)
        or not 1 <= port <= 65535
    ):
        raise ValueError("repository bind/port must select a private LAN interface")
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or base_url.endswith("/")
        or any(ord(char) < 33 or ord(char) > 126 for char in base_url)
    ):
        raise ValueError("repository base URL must be a credential-free origin")
    try:
        _ = url.port
    except ValueError as error:
        raise ValueError("repository base URL port is invalid") from error
    if url.hostname in {"localhost", "127.0.0.1"}:
        raise ValueError("repository base URL cannot name loopback")
    return base_url


@dataclass(frozen=True)
class AddonPackage:
    addon_id: str
    version: str
    descriptor: ET.Element
    sha256: str
    tree_sha256: str
    bytes: bytes


def inspect_addon_zip(
    data: bytes, expected_id: str, expected_version: str
) -> AddonPackage:
    """Inspect bounded ZIP contents without extracting or trusting its descriptor."""
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("add-on ZIP exceeds compressed limit")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise ValueError("add-on ZIP has too many entries")
        for item in members:
            name = item.filename
            if (
                not name
                or "\\" in name
                or "\x00" in name
                or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
                or len(name.encode("utf-8")) > 512
                or len(name.rstrip("/").split("/")) > 12
            ):
                raise ValueError("add-on ZIP path is unsafe")
            folded = name.rstrip("/").casefold()
            if folded in seen:
                raise ValueError("add-on ZIP contains duplicate paths")
            seen.add(folded)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("add-on ZIP contains special file or link")
            if item.is_dir() != (kind == stat.S_IFDIR) and kind != 0:
                raise ValueError("add-on ZIP member type differs")
            if item.file_size > MAX_MEMBER_BYTES or item.compress_size > MAX_ZIP_BYTES:
                raise ValueError("add-on ZIP member exceeds size limit")
            total += item.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("add-on ZIP exceeds uncompressed limit")
            if item.file_size > max(1024 * 1024, item.compress_size * 200):
                raise ValueError("add-on ZIP compression ratio exceeds limit")
            content = archive.read(item)
            if item.is_dir():
                if content:
                    raise ValueError("add-on ZIP directory contains bytes")
            else:
                files[name] = content
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("add-on ZIP is malformed") from error
    roots = {name.split("/", 1)[0] for name in seen}
    if roots != {expected_id} or f"{expected_id}/addon.xml" not in files:
        raise ValueError("add-on ZIP root/descriptor differs")
    descriptor_bytes = files[f"{expected_id}/addon.xml"]
    if len(descriptor_bytes) > 1024 * 1024 or any(
        token in descriptor_bytes.upper() for token in (b"<!DOCTYPE", b"<!ENTITY")
    ):
        raise ValueError("add-on descriptor is unsafe")
    try:
        descriptor = ET.fromstring(descriptor_bytes)
    except ET.ParseError as error:
        raise ValueError("add-on descriptor is malformed") from error
    addon_id = descriptor.attrib.get("id", "")
    version = descriptor.attrib.get("version", "")
    if (
        descriptor.tag != "addon"
        or not ID_PATTERN.fullmatch(addon_id)
        or not VERSION_PATTERN.fullmatch(version)
        or addon_id != expected_id
        or version != expected_version
    ):
        raise ValueError("add-on ID/version differs from reviewed manifest")
    for asset in descriptor.findall(
        "./extension[@point='xbmc.addon.metadata']/assets/*"
    ):
        if not asset.text:
            continue
        path = PurePosixPath(asset.text)
        if path.is_absolute() or ".." in path.parts or str(path) != asset.text:
            raise ValueError("declared add-on asset path is unsafe")
        if f"{expected_id}/{path}" not in files:
            raise ValueError("declared add-on asset is missing")
    tree = canonical(
        [
            {"path": name.removeprefix(expected_id + "/"), "sha256": sha256(content)}
            for name, content in sorted(files.items())
        ]
    )
    return AddonPackage(addon_id, version, descriptor, sha256(data), sha256(tree), data)


def descriptor_xml(base_url: str) -> bytes:
    addon = ET.Element(
        "addon",
        {
            "id": REPOSITORY_ID,
            "name": "Kodi Lab",
            "version": REPOSITORY_VERSION,
            "provider-name": "Kodi Lab",
        },
    )
    extension = ET.SubElement(
        addon, "extension", {"point": "xbmc.addon.repository", "name": "Kodi Lab"}
    )
    directory = ET.SubElement(extension, "dir", {"minversion": "19.0.0"})
    ET.SubElement(directory, "info", {"compressed": "false"}).text = (
        base_url + "/stable/addons.xml"
    )
    ET.SubElement(directory, "checksum", {"verify": "sha256"}).text = (
        base_url + "/stable/addons.xml.sha256"
    )
    ET.SubElement(directory, "datadir", {"zip": "true"}).text = (
        base_url + "/stable/zips/"
    )
    ET.SubElement(directory, "hashes").text = "sha256"
    metadata = ET.SubElement(addon, "extension", {"point": "xbmc.addon.metadata"})
    ET.SubElement(
        metadata, "summary", {"lang": "en_GB"}
    ).text = "Reviewed Kodi Lab companion add-ons"
    ET.SubElement(
        metadata, "description", {"lang": "en_GB"}
    ).text = "Stable reviewed Kodi Lab infrastructure."
    ET.SubElement(metadata, "platform").text = "all"
    ET.SubElement(metadata, "license").text = "MIT"
    return bytes(ET.tostring(addon, encoding="utf-8", xml_declaration=True)) + b"\n"


def bootstrap_zip(base_url: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content, directory in (
            (f"{REPOSITORY_ID}/", b"", True),
            (f"{REPOSITORY_ID}/addon.xml", descriptor_xml(base_url), False),
        ):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.create_system = 3
            info.external_attr = ((0o40755 if directory else 0o100644) << 16) | (
                0x10 if directory else 0
            )
            info.compress_type = (
                zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
            )
            archive.writestr(
                info, content, compress_type=info.compress_type, compresslevel=9
            )
    return output.getvalue()


def _write_exact(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError("published add-on ID/version has different bytes")
        return
    fd, temp_name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o644)
        try:
            os.link(temp, path, follow_symlinks=False)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(
                    "published add-on ID/version has different bytes"
                ) from None
    finally:
        temp.unlink(missing_ok=True)


def _read_reviewed_bridge(
    bundle: Path,
) -> tuple[bytes, dict[str, object], dict[str, object]]:
    manifest_bytes = (bundle / "BUNDLE.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if (
        manifest.get("schema") != "kodi-lab-linux-v1-bundle-v3"
        or canonical(manifest) != manifest_bytes
    ):
        raise ValueError("candidate bundle manifest is invalid")
    stable_bytes = (bundle / "repository/STABLE.json").read_bytes()
    stable_record = manifest["files"]["repository/STABLE.json"]
    stable = json.loads(stable_bytes)
    if (
        canonical(stable) != stable_bytes
        or stable_record
        != {"sha256": "sha256:" + sha256(stable_bytes), "size": len(stable_bytes)}
        or stable.get("repository_id") != REPOSITORY_ID
        or stable.get("repository_version") != REPOSITORY_VERSION
        or stable.get("generator") != GENERATOR_ID
        or stable.get("bridge_id") != BRIDGE_ID
        or stable.get("bridge_version") != BRIDGE_VERSION
        or stable.get("bridge_zip_sha256") != BRIDGE_SHA256
        or stable.get("bridge_semantic_fingerprint") != BRIDGE_FINGERPRINT
    ):
        raise ValueError("stable repository reviewed inputs differ")
    bridge_name = "bridge/service.kodi_lab_bridge-1.1.13.zip"
    bridge = (bundle / bridge_name).read_bytes()
    record = manifest["files"][bridge_name]
    provenance_bytes = (bundle / "bridge/BRIDGE.json").read_bytes()
    provenance = json.loads(provenance_bytes)
    provenance_record = manifest["files"]["bridge/BRIDGE.json"]
    if (
        record != {"sha256": "sha256:" + sha256(bridge), "size": len(bridge)}
        or provenance_record
        != {
            "sha256": "sha256:" + sha256(provenance_bytes),
            "size": len(provenance_bytes),
        }
        or sha256(bridge) != BRIDGE_SHA256
        or provenance.get("sha256") != BRIDGE_SHA256
        or provenance.get("semantic_fingerprint") != BRIDGE_FINGERPRINT
        or provenance.get("addon_id") != BRIDGE_ID
        or provenance.get("version") != BRIDGE_VERSION
        or provenance.get("source_commit") != manifest.get("source_commit")
        or provenance.get("source_tree") != manifest.get("source_tree")
    ):
        raise ValueError("reviewed bridge provenance differs")
    return bridge, manifest, provenance


def generate(bundle: Path, data_root: Path, base_url: str) -> dict[str, object]:
    """Publish a complete stable generation after validating exact candidate inputs."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("repository base URL is invalid")
    bridge_bytes, bundle_manifest, _ = _read_reviewed_bridge(bundle)
    bridge = inspect_addon_zip(bridge_bytes, BRIDGE_ID, BRIDGE_VERSION)
    repository = inspect_addon_zip(
        bootstrap_zip(base_url), REPOSITORY_ID, REPOSITORY_VERSION
    )
    packages = sorted((bridge, repository), key=lambda item: item.addon_id)
    index = ET.Element("addons")
    for package in packages:
        index.append(package.descriptor)
    index_bytes = ET.tostring(index, encoding="utf-8", xml_declaration=True) + b"\n"
    index_hash = sha256(index_bytes)
    package_records = [
        {
            "id": package.addon_id,
            "version": package.version,
            "sha256": package.sha256,
            "tree_sha256": package.tree_sha256,
        }
        for package in packages
    ]
    bundle_hash = sha256((bundle / "BUNDLE.json").read_bytes())
    feed_tree = sha256(
        canonical(
            {
                "bundle_sha256": bundle_hash,
                "source_commit": bundle_manifest["source_commit"],
                "source_tree": bundle_manifest["source_tree"],
                "generator": GENERATOR_ID,
                "base_url": base_url,
                "addons_xml_sha256": index_hash,
                "packages": package_records,
            }
        )
    )
    provenance = {
        "schema": "kodi-lab-installed-stable-repository-v1",
        "bundle_sha256": bundle_hash,
        "source_commit": bundle_manifest["source_commit"],
        "source_tree": bundle_manifest["source_tree"],
        "generator": GENERATOR_ID,
        "base_url": base_url,
        "repository_id": REPOSITORY_ID,
        "repository_version": REPOSITORY_VERSION,
        "bootstrap_zip_sha256": repository.sha256,
        "addons_xml_sha256": index_hash,
        "tree_sha256": feed_tree,
        "packages": package_records,
    }
    data_root.mkdir(parents=True, exist_ok=True)
    pointer = data_root / "CURRENT"
    if pointer.exists():
        _, prior = load_current(data_root)
        if (
            prior.get("base_url") != base_url
            or prior.get("bundle_sha256") != provenance["bundle_sha256"]
        ):
            raise ValueError(
                "repository is already configured for another origin/candidate"
            )
    for package in packages:
        relative = f"{package.addon_id}/{package.addon_id}-{package.version}.zip"
        path = data_root / "packages" / relative
        _immutable(path, package.bytes)
        _immutable(
            path.with_name(path.name + ".sha256"), (package.sha256 + "\n").encode()
        )
    generations = data_root / "generations"
    generations.mkdir(exist_ok=True)
    final = generations / feed_tree
    if final.exists():
        if (
            final.is_symlink()
            or (final / "PROVENANCE.json").read_bytes() != canonical(provenance)
            or (final / "stable/addons.xml").read_bytes() != index_bytes
            or (final / "stable/addons.xml.sha256").read_bytes()
            != (index_hash + "\n").encode()
            or (
                final / "install" / f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip"
            ).read_bytes()
            != repository.bytes
        ):
            raise ValueError("existing repository generation differs")
    else:
        temp = Path(tempfile.mkdtemp(prefix=".generation-", dir=generations))
        try:
            os.chmod(temp, 0o755)
            install = temp / "install"
            stable = temp / "stable"
            install.mkdir()
            stable.mkdir()
            _write_exact(
                install / f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip", repository.bytes
            )
            _write_exact(stable / "addons.xml", index_bytes)
            _write_exact(stable / "addons.xml.sha256", (index_hash + "\n").encode())
            _write_exact(temp / "PROVENANCE.json", canonical(provenance))
            os.rename(temp, final)
        finally:
            if temp.exists():
                for path in sorted(temp.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    else:
                        path.rmdir()
                temp.rmdir()
    if pointer.exists() and pointer.read_text().strip() == feed_tree:
        return provenance
    descriptor, temp_name = tempfile.mkstemp(prefix=".CURRENT-", dir=data_root)
    temp_pointer = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((feed_tree + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_pointer, 0o644)
        os.replace(temp_pointer, pointer)
        directory = os.open(data_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp_pointer.unlink(missing_ok=True)
    return provenance


def load_current(
    data_root: Path,
    *,
    expected_base_url: str | None = None,
    expected_bundle_sha256: str | None = None,
) -> tuple[Path, dict[str, object]]:
    """Verify a complete selected generation before any file is served."""
    if (data_root / "CURRENT").is_symlink() or (data_root / "generations").is_symlink():
        raise ValueError("repository state path is unsafe")
    pointer = (data_root / "CURRENT").read_text().strip()
    if not HEX_PATTERN.fullmatch(pointer):
        raise ValueError("repository generation pointer is invalid")
    root = data_root / "generations" / pointer
    if root.is_symlink():
        raise ValueError("repository generation path is unsafe")
    provenance_bytes = (root / "PROVENANCE.json").read_bytes()
    provenance = json.loads(provenance_bytes)
    if (
        canonical(provenance) != provenance_bytes
        or provenance.get("schema") != "kodi-lab-installed-stable-repository-v1"
        or provenance.get("tree_sha256") != pointer
        or provenance.get("generator") != GENERATOR_ID
        or provenance.get("repository_id") != REPOSITORY_ID
        or provenance.get("repository_version") != REPOSITORY_VERSION
        or not isinstance(provenance.get("base_url"), str)
        or not isinstance(provenance.get("bundle_sha256"), str)
        or (
            expected_base_url is not None
            and provenance["base_url"] != expected_base_url
        )
        or (
            expected_bundle_sha256 is not None
            and provenance["bundle_sha256"] != expected_bundle_sha256
        )
    ):
        raise ValueError("repository provenance is invalid")
    records = provenance.get("packages")
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("repository package inventory differs")
    packages = []
    for record, expected_id, expected_version in zip(
        records,
        (REPOSITORY_ID, BRIDGE_ID),
        (REPOSITORY_VERSION, BRIDGE_VERSION),
        strict=True,
    ):
        if (
            not isinstance(record, dict)
            or record.get("id") != expected_id
            or record.get("version") != expected_version
            or not isinstance(record.get("sha256"), str)
            or not HEX_PATTERN.fullmatch(record["sha256"])
            or not isinstance(record.get("tree_sha256"), str)
            or not HEX_PATTERN.fullmatch(record["tree_sha256"])
        ):
            raise ValueError("repository package inventory differs")
        package_path = (
            data_root
            / "packages"
            / expected_id
            / f"{expected_id}-{expected_version}.zip"
        )
        if (
            (data_root / "packages").is_symlink()
            or package_path.parent.is_symlink()
            or package_path.is_symlink()
        ):
            raise ValueError("repository package path is unsafe")
        package_bytes = package_path.read_bytes()
        package = inspect_addon_zip(package_bytes, expected_id, expected_version)
        if (
            package.sha256 != record["sha256"]
            or package.tree_sha256 != record["tree_sha256"]
        ):
            raise ValueError("repository package bytes differ")
        if (
            package_path.with_name(package_path.name + ".sha256").read_bytes()
            != (record["sha256"] + "\n").encode()
        ):
            raise ValueError("repository package checksum differs")
        packages.append(package)
    if packages[1].sha256 != BRIDGE_SHA256 or packages[0].bytes != bootstrap_zip(
        provenance["base_url"]
    ):
        raise ValueError("repository reviewed package identity differs")
    index = ET.Element("addons")
    for package in packages:
        index.append(package.descriptor)
    expected_index = (
        bytes(ET.tostring(index, encoding="utf-8", xml_declaration=True)) + b"\n"
    )
    expected_tree = sha256(
        canonical(
            {
                "bundle_sha256": provenance["bundle_sha256"],
                "source_commit": provenance["source_commit"],
                "source_tree": provenance["source_tree"],
                "generator": GENERATOR_ID,
                "base_url": provenance["base_url"],
                "addons_xml_sha256": sha256(expected_index),
                "packages": records,
            }
        )
    )
    if expected_tree != pointer or provenance.get("addons_xml_sha256") != sha256(
        expected_index
    ):
        raise ValueError("repository generation identity differs")
    for relative, expected in (
        ("stable/addons.xml", provenance["addons_xml_sha256"]),
        (
            f"install/{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip",
            provenance["bootstrap_zip_sha256"],
        ),
    ):
        if sha256((root / relative).read_bytes()) != expected:
            raise ValueError("repository generation bytes differ")
    if (root / "stable/addons.xml").read_bytes() != expected_index:
        raise ValueError("repository index content differs")
    if (
        provenance.get("bootstrap_zip_sha256") != packages[0].sha256
        or (root / "install" / f"{REPOSITORY_ID}-{REPOSITORY_VERSION}.zip").read_bytes()
        != packages[0].bytes
    ):
        raise ValueError("repository bootstrap bytes differ")
    if (root / "stable/addons.xml.sha256").read_bytes() != (
        provenance["addons_xml_sha256"] + "\n"
    ).encode():
        raise ValueError("repository index checksum differs")
    return root, provenance

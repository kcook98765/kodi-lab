"""Bounded package intake and exact private repository publication."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from .runtime import BRIDGE, REPO, Refusal, atomic

ADDON_ID = re.compile(r"(?:script|plugin|skin|resource)\.[a-z0-9_.-]{1,90}")
HEX = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9][a-zA-Z0-9.+~-]{0,39}")
TYPES = {
    "xbmc.python.script",
    "xbmc.python.pluginsource",
    "xbmc.python.module",
    "xbmc.gui.skin",
    "kodi.resource.images",
}
MAX_ZIP = 32 * 1024**2
MAX_TREE = 64 * 1024**2


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def addon_id(value):
    if (
        not isinstance(value, str)
        or not ADDON_ID.fullmatch(value)
        or value in {BRIDGE, REPO, "skin.estuary"}
    ):
        raise Refusal("invalid_addon_id")
    return value


def safe_name(name):
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or any(x in (".", "..") for x in path.parts)
        or "\\" in name
        or len(name) > 240
        or str(path) != name
    ):
        raise Refusal("unsafe_package_path")
    return path


def manifest(data):
    if (
        len(data) > 128 * 1024
        or b"<!DOCTYPE" in data.upper()
        or b"<!ENTITY" in data.upper()
    ):
        raise Refusal("unsafe_manifest")
    try:
        m = ET.fromstring(data)
    except ET.ParseError as error:
        raise Refusal("invalid_manifest") from error
    addon_id(m.get("id"))
    if (
        m.tag != "addon"
        or not VERSION.fullmatch(m.get("version", ""))
        or not 1 <= len(m.get("name", "")) <= 128
    ):
        raise Refusal("invalid_manifest")
    extensions = [
        node.get("point")
        for node in m.findall("extension")
        if node.get("point") != "xbmc.addon.metadata"
    ]
    if len(extensions) != 1 or extensions[0] not in TYPES:
        raise Refusal("unsupported_extension")
    return m, extensions[0]


@dataclass
class Package:
    data: bytes
    node: ET.Element
    category: str
    members: dict[str, str]

    @property
    def id(self):
        return self.node.attrib["id"]

    @property
    def version(self):
        return self.node.attrib["version"]

    @property
    def digest(self):
        return sha(self.data)


def read_zip(data):
    import io

    if len(data) > MAX_ZIP:
        raise Refusal("package_limit")
    with ZipFile(io.BytesIO(data)) as archive:
        items = archive.infolist()
        if len(items) > 3000 or sum(x.file_size for x in items) > MAX_TREE:
            raise Refusal("package_limit")
        seen = set()
        members = {}
        root = None
        for item in items:
            name = safe_name(item.filename.rstrip("/"))
            kind = stat.S_IFMT(item.external_attr >> 16)
            if name in seen or kind not in (
                0,
                stat.S_IFDIR if item.is_dir() else stat.S_IFREG,
            ):
                raise Refusal("unsafe_zip_entry")
            seen.add(name)
            root = root or name.parts[0]
            if root != name.parts[0] or (not item.is_dir() and len(name.parts) < 2):
                raise Refusal("package_root_mismatch")
            if not item.is_dir():
                members[str(name.relative_to(root))] = sha(archive.read(item))
        if "addon.xml" not in members:
            raise Refusal("invalid_manifest")
        node, category = manifest(archive.read(root + "/addon.xml"))
    if node.attrib["id"] != root:
        raise Refusal("package_root_mismatch")
    return Package(data, node, category, members)


def workspace_files(root):
    if not root.is_dir() or root.is_symlink():
        raise Refusal("workspace_missing")
    result = []
    total = 0
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        safe_name(name)
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise Refusal("nonregular_tree_entry")
        if path.is_file():
            total += path.stat().st_size
            if len(result) >= 3000 or total > MAX_TREE:
                raise Refusal("workspace_limit")
            result.append((name, path))
    return result


class Packages:
    def __init__(self, root, feed):
        self.approved = root / "approved"
        self.workspaces = root / "workspaces"
        self.feed = feed
        self.catalog_file = root / "catalog.json"

    def approve(self, data):
        """Operator intake for existing ZIPs and a captured approved repository ZIP."""
        pkg = read_zip(data)
        atomic(self.approved / (pkg.digest + ".zip"), data)
        return pkg

    def workspace_package(self, addon):
        import io

        addon_id(addon)
        tree = self.workspaces / addon
        files = workspace_files(tree)
        node, _ = manifest((tree / "addon.xml").read_bytes())
        if node.attrib["id"] != addon:
            raise Refusal("workspace_identity")
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            # Kodi's package reader expects a distinct top-level directory.
            archive.mkdir(addon)
            for name, path in files:
                archive.write(path, addon + "/" + name)
        return self.approve(output.getvalue())

    def get(self, digest):
        if not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise Refusal("invalid_package_digest")
        file = self.approved / (digest + ".zip")
        if not file.is_file() or file.is_symlink() or file.stat().st_size > MAX_ZIP:
            raise Refusal("package_not_approved")
        package = read_zip(file.read_bytes())
        if package.digest != digest:
            raise Refusal("package_digest_mismatch")
        return package

    def catalog(self):
        raw = (
            json.loads(self.catalog_file.read_text())
            if self.catalog_file.exists()
            else []
        )
        return {p.id: p for digest in raw for p in [self.get(digest)]}

    def publish(self, package):
        candidates = self.catalog()
        candidates[package.id] = package
        index = ET.Element("addons")
        for pkg in sorted(candidates.values(), key=lambda item: item.id):
            folder = self.feed / "zips" / pkg.id
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{pkg.id}-{pkg.version}.zip"
            # Both files are private to this one runtime and mutation gate.
            atomic(target, pkg.data)
            atomic(target.with_suffix(".zip.sha256"), (pkg.digest + "\n").encode())
            index.append(pkg.node)
        catalog = ET.tostring(index, encoding="utf-8", xml_declaration=True) + b"\n"
        checksum = sha(catalog)
        atomic(self.feed / "addons.xml", catalog)
        atomic(self.feed / "addons.xml.sha256", (checksum + "\n").encode())
        return checksum, candidates

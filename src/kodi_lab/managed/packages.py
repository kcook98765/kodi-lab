"""Bounded package intake and exact private repository publication."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import re
import shutil
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from .runtime import BRIDGE, REPO, Refusal, atomic

ADDON_ID = re.compile(r"[a-z][a-z0-9_.-]{1,95}")
HEX = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"[0-9][a-zA-Z0-9.+~-]{0,39}")
TYPES = {
    "xbmc.python.script",
    "xbmc.python.pluginsource",
    "xbmc.python.module",
    "xbmc.gui.skin",
    "kodi.resource.images",
    "kodi.resource.language",
    "kodi.resource.uisounds",
    "kodi.context.item",
    "xbmc.python.library",
    "xbmc.service",
    "xbmc.addon.metadata",
}
MAX_ZIP = 32 * 1024**2
MAX_TREE = 64 * 1024**2


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def addon_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not ADDON_ID.fullmatch(value)
        or value.startswith(("repository.", "xbmc.", "kodi."))
        or value in {BRIDGE, REPO, "skin.estuary"}
    ):
        raise Refusal("invalid_addon_id")
    return value


def safe_name(name: str) -> PurePosixPath:
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


def manifest(data: bytes, *, repository: bool = False) -> tuple[ET.Element, str]:
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
    if repository:
        identity = m.get("id", "")
        if not identity.startswith("repository.") or not ADDON_ID.fullmatch(identity):
            raise Refusal("invalid_addon_id")
    else:
        addon_id(m.get("id"))
    if (
        m.tag != "addon"
        or not VERSION.fullmatch(m.get("version", ""))
        or not 1 <= len(m.get("name", "")) <= 128
    ):
        raise Refusal("invalid_manifest")
    extensions = m.findall("extension")
    allowed = {"xbmc.addon.repository", "xbmc.addon.metadata"} if repository else TYPES
    if not extensions or any(node.get("point") not in allowed for node in extensions):
        raise Refusal("unsupported_extension")
    for entry in m.findall("requires/import"):
        if not ADDON_ID.fullmatch(entry.get("addon", "")):
            raise Refusal("invalid_dependency_id")
    # Kodi uses the first non-metadata extension as the add-on's listing type.
    category = next(
        (
            node.attrib["point"]
            for node in extensions
            if node.attrib["point"] != "xbmc.addon.metadata"
        ),
        "",
    )
    if not category:
        raise Refusal("missing_functional_extension")
    return m, category


@dataclass
class Package:
    data: bytes
    node: ET.Element
    category: str
    members: dict[str, str]

    @property
    def id(self) -> str:
        return self.node.attrib["id"]

    @property
    def version(self) -> str:
        return self.node.attrib["version"]

    @property
    def digest(self) -> str:
        return sha(self.data)


def read_zip(data: bytes, *, repository: bool = False) -> Package:
    if len(data) > MAX_ZIP:
        raise Refusal("package_limit")
    try:
        return _read_zip(data, repository=repository)
    except (BadZipFile, EOFError, NotImplementedError, RuntimeError, zlib.error) as exc:
        raise Refusal("invalid_package_zip") from exc


def _read_zip(data: bytes, *, repository: bool) -> Package:
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
        if root is None or "addon.xml" not in members:
            raise Refusal("invalid_manifest")
        if any(
            ancestor.as_posix() in members
            for path in members
            for ancestor in PurePosixPath(path).parents
        ):
            raise Refusal("unsafe_zip_entry")
        node, category = manifest(
            archive.read(root + "/addon.xml"), repository=repository
        )
    if node.attrib["id"] != root:
        raise Refusal("package_root_mismatch")
    return Package(data, node, category, members)


def workspace_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir() or root.is_symlink():
        raise Refusal("workspace_missing")
    result: list[tuple[str, Path]] = []
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
    def __init__(
        self, root: Path, feed: Path, *, catalog_root: Path | None = None
    ) -> None:
        self.approved = root / "approved"
        self.workspaces = root / "workspaces"
        self.feed = feed
        self.catalog_root = catalog_root or root
        self.catalog_file = self.catalog_root / "catalog.json"

    def approve(self, data: bytes) -> Package:
        """Operator intake for existing ZIPs and a captured approved repository ZIP."""
        pkg = read_zip(data)
        atomic(self.approved / (pkg.digest + ".zip"), data)
        return pkg

    def workspace_package(self, addon: str) -> Package:
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

    def open_workspace(self, digest: str, replace: str = "false") -> dict[str, str]:
        package = self.get(digest)
        target = self.workspaces / package.id
        if target.is_symlink():
            raise Refusal("workspace_identity")
        exists = target.exists()
        if exists and replace != "true":
            raise Refusal("workspace_exists")
        if replace == "true" and not target.is_dir():
            raise Refusal("workspace_missing")
        if (
            not exists
            and sum(path.is_dir() for path in self.workspaces.iterdir()) >= 16
        ):
            raise Refusal("workspace_limit")
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(dir=self.workspaces) as temporary:
            stage = Path(temporary) / "source"
            stage.mkdir()
            with ZipFile(io.BytesIO(package.data)) as archive:
                for name in package.members:
                    output = stage / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.open(package.id + "/" + name) as source,
                        output.open("xb") as dest,
                    ):
                        shutil.copyfileobj(source, dest)
            if exists:
                # The broker runs on Linux with both paths on the same volume.
                # Exchange keeps the current tree intact if publication fails.
                exchange = ctypes.CDLL(None).renameat2
                if exchange(-100, bytes(stage), -100, bytes(target), 2) != 0:
                    raise Refusal("workspace_replace_unavailable")
            else:
                stage.rename(target)
        return {"addon": package.id, "version": package.version}

    def tree(self, addon: str) -> list[str]:
        return [name for name, _ in workspace_files(self.workspaces / addon_id(addon))]

    def read(self, addon: str, path: str) -> dict[str, str]:
        files = dict(workspace_files(self.workspaces / addon_id(addon)))
        file = files.get(str(safe_name(path)))
        if file is None or file.stat().st_size > 24_000:
            raise Refusal("workspace_file_unavailable")
        content = file.read_bytes()
        try:
            return {"content": content.decode("utf-8"), "digest": sha(content)}
        except UnicodeError as exc:
            raise Refusal("workspace_text_required") from exc

    def edit(
        self, addon: str, path: str, content: str, expected_digest: str
    ) -> dict[str, str]:
        root = self.workspaces / addon_id(addon)
        files = dict(workspace_files(root))
        name = str(safe_name(path))
        new = content.encode()
        file = files.get(name)
        if file is None or file.stat().st_size > 24_000 or len(new) > 24_000:
            raise Refusal("workspace_file_unavailable")
        if sha(file.read_bytes()) != expected_digest:
            raise Refusal("workspace_edit_conflict")
        if (
            sum(item.stat().st_size for item in files.values())
            - file.stat().st_size
            + len(new)
            > MAX_TREE
        ):
            raise Refusal("workspace_limit")
        atomic(file, new)
        return {"digest": sha(new)}

    def get(self, digest: str) -> Package:
        if not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise Refusal("invalid_package_digest")
        file = self.approved / (digest + ".zip")
        if not file.is_file() or file.is_symlink() or file.stat().st_size > MAX_ZIP:
            raise Refusal("package_not_approved")
        package = read_zip(file.read_bytes())
        if package.digest != digest:
            raise Refusal("package_digest_mismatch")
        return package

    def catalog(self) -> dict[str, Package]:
        raw = (
            json.loads(self.catalog_file.read_text())
            if self.catalog_file.exists()
            else []
        )
        reviewed = Packages(self.catalog_root, self.feed)
        return {p.id: p for digest in raw for p in [reviewed.get(digest)]}

    def publish(self, package: Package) -> tuple[str, dict[str, Package]]:
        candidates = self.catalog()
        candidates[package.id] = package
        index = ET.Element("addons")
        # An equal-version package can change without changing addon.xml.
        # Bind the catalogue checksum to the selected archive bytes.
        index.append(ET.Comment("package sha256 " + package.digest))
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

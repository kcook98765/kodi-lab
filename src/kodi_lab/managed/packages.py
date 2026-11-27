"""Bounded package intake and exact private repository publication."""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import zlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from kodi_lab.package_hygiene import has_vcs_admin_component

from .runtime import BRIDGE, REPO, Refusal, atomic, sync_dir

if TYPE_CHECKING:
    from .feed_retirement import FeedRetirement

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
FEED_METADATA_ALLOWANCE = 1024**2
ARTWORK_TAGS = {"icon", "screenshot", "fanart", "banner", "clearlogo", "thumb"}
ARTWORK_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tbn"}
ARTWORK_DIRECTORY = "artwork"


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


def catalog_entry(
    package: Package, archive: ZipFile
) -> tuple[ET.Element, dict[str, str]]:
    """Project approved artwork into a feed-only URL namespace."""
    node = deepcopy(package.node)
    artwork: dict[str, str] = {}
    for extension in node.findall("extension[@point='xbmc.addon.metadata']"):
        # The private publisher owns the repository path for each exact ZIP.
        for path in extension.findall("path"):
            extension.remove(path)
        for assets in extension.findall("assets"):
            for asset in list(assets):
                member = asset.text or ""
                extension_name = PurePosixPath(member).suffix.lower()
                digest = package.members.get(member)
                if (
                    asset.tag not in ARTWORK_TAGS
                    or extension_name not in ARTWORK_EXTENSIONS
                    or digest is None
                ):
                    assets.remove(asset)
                    continue
                source = f"{package.id}/{member}"
                if archive.getinfo(source).file_size == 0:
                    assets.remove(asset)
                    continue
                filename = digest + extension_name
                artwork[filename] = source
                asset.text = f"{ARTWORK_DIRECTORY}/{filename}"
    return node, artwork


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
            if has_vcs_admin_component(name.parts):
                raise Refusal("unsafe_package_path")
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

    @staticmethod
    def require_publication_space(directory: Path, lengths: list[int]) -> None:
        try:
            usage = os.statvfs(directory)
        except OSError as error:
            raise Refusal("disk_headroom_unavailable") from error
        fragment = usage.f_frsize
        if fragment <= 0:
            raise Refusal("disk_headroom_unavailable")
        # Treat every write as adding blocks. This covers new paths and the
        # overlap while atomic replacements still retain the original bytes.
        # Leave room for directory entries and small lifecycle records.
        peak_blocks = sum((length + fragment - 1) // fragment for length in lengths)
        if usage.f_bavail * fragment < FEED_METADATA_ALLOWANCE + peak_blocks * fragment:
            raise Refusal("disk_headroom")

    def publish(
        self,
        package: Package,
        *,
        retirement: FeedRetirement | None = None,
        owner: str = "admin",
    ) -> tuple[str, dict[str, Package]]:
        candidates = self.catalog()
        candidates[package.id] = package
        packages = sorted(candidates.values(), key=lambda item: item.id)
        index = ET.Element("addons")
        # Bind the catalog checksum to each exact archive, including changes
        # to the bytes of a dependency at the same version.
        artwork: dict[str, dict[str, str]] = {}
        lengths = [65]
        for pkg in packages:
            with ZipFile(io.BytesIO(pkg.data)) as archive:
                node, assets = catalog_entry(pkg, archive)
                artwork[pkg.id] = assets
                lengths.extend(
                    archive.getinfo(source).file_size for source in assets.values()
                )
            index.append(ET.Comment("package sha256 " + pkg.digest))
            index.append(node)
            lengths.extend((len(pkg.data), 65))
        catalog = ET.tostring(index, encoding="utf-8", xml_declaration=True) + b"\n"
        checksum = sha(catalog)
        lengths.append(len(catalog))
        if retirement is None:
            self.require_publication_space(self.feed, lengths)
        else:
            from .feed_retirement import RECEIPT_LIMIT, FeedCapacity

            # The committed receipt is staged alongside the intent receipt.
            receipts = [RECEIPT_LIMIT, RECEIPT_LIMIT]
            if self.feed.stat().st_dev == retirement.root.stat().st_dev:
                self.require_publication_space(self.feed, [*lengths, *receipts])
            else:
                self.require_publication_space(self.feed, lengths)
                self.require_publication_space(retirement.root, receipts)
            try:
                retirement.plan(package, candidates, checksum, owner)
            except FeedCapacity:
                raise
            except (Refusal, OSError, ValueError):
                retirement = None
                logging.warning("Managed feed retirement plan unavailable")
        for pkg in packages:
            folder = self.feed / "zips" / pkg.id
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{pkg.id}-{pkg.version}.zip"
            # Both files are private to this one runtime and mutation gate.
            atomic(target, pkg.data)
            atomic(target.with_suffix(".zip.sha256"), (pkg.digest + "\n").encode())
            if assets := artwork[pkg.id]:
                directory = folder / ARTWORK_DIRECTORY
                directory.mkdir(exist_ok=True)
                with ZipFile(io.BytesIO(pkg.data)) as archive:
                    for filename, source in assets.items():
                        atomic(directory / filename, archive.read(source))
        if retirement is not None:
            sync_dir(self.feed / "zips")
        atomic(self.feed / "addons.xml", catalog)
        atomic(self.feed / "addons.xml.sha256", (checksum + "\n").encode())
        if retirement is not None:
            try:
                retirement.confirm_publication()
            except (Refusal, OSError, ValueError):
                logging.warning("Managed feed retirement publication pending")
        for addon, assets in artwork.items():
            directory = self.feed / "zips" / addon / ARTWORK_DIRECTORY
            for path in directory.iterdir() if directory.is_dir() else ():
                if (
                    path.name not in assets
                    and HEX.fullmatch(path.stem)
                    and path.suffix in ARTWORK_EXTENSIONS
                ):
                    try:
                        path.unlink()
                    except OSError:
                        logging.warning("Managed feed artwork retirement pending")
        return checksum, candidates

"""Deterministic workspace ZIPs with an expected narrow-bridge tree manifest."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kodi_lab.development.deployment import DevelopmentTreeManifest
from kodi_lab.repository.stable import ZIP_DATE, inspect_addon_zip


@dataclass(frozen=True, slots=True)
class WorkspaceRepositoryPackage:
    content: bytes
    zip_sha256: str
    package_tree_sha256: str
    installed_manifest_digest: str
    file_count: int
    total_bytes: int


def bridge_manifest_for_zip(content: bytes, addon_id: str) -> tuple[str, int, int]:
    """Predict the existing bridge inspect_addon manifest for normal extraction."""

    inspect_addon_zip(content, addon_id)
    entries: list[dict[str, object]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for item in archive.infolist():
            relative = item.filename.removeprefix(addon_id + "/").rstrip("/")
            if not relative:
                continue
            if relative == "__pycache__" or "/__pycache__/" in f"/{relative}/":
                raise ValueError("repository package contains bytecode cache")
            if item.is_dir():
                entries.append(
                    {"mode": 0o755, "path": relative, "size": 0, "type": "directory"}
                )
            else:
                if item.external_attr >> 16 & 0o111:
                    raise ValueError("repository package contains executable file")
                data = archive.read(item)
                entries.append(
                    {
                        "mode": 0o644,
                        "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                        "type": "file",
                    }
                )
    entries.sort(key=lambda item: str(item["path"]).encode("utf-8"))
    encoded = json.dumps(
        {"entries": entries, "exclusion_policy_version": 1, "manifest_version": 1},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    files = [item for item in entries if item["type"] == "file"]
    return (
        "sha256:" + hashlib.sha256(encoded).hexdigest(),
        len(files),
        sum(cast(int, item["size"]) for item in files),
    )


def build_workspace_package(
    addon_id: str, manifest: DevelopmentTreeManifest, stage_root: Path
) -> WorkspaceRepositoryPackage:
    """Package captured bytes; predict inspect_addon's normalized tree evidence.

    Repository mode deliberately admits only regular 0644 files. Kodi's ZIP
    extraction may choose its own modes; executable source files therefore need
    the direct controlled deployment path until that behavior is proven.
    """

    if not manifest.entries or any(
        item.entry_type == "file" and item.mode != 0o644 for item in manifest.entries
    ):
        raise ValueError("repository deployment requires non-executable regular files")
    if any(
        item.entry_type == "directory" and item.mode != 0o755
        for item in manifest.entries
    ):
        raise ValueError("repository deployment directory mode is unsupported")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        root = zipfile.ZipInfo(f"{addon_id}/", ZIP_DATE)
        root.create_system = 3
        root.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
        root.compress_type = zipfile.ZIP_STORED
        archive.writestr(root, b"")
        for item in manifest.entries:
            name = f"{addon_id}/{item.path}"
            directory = item.entry_type == "directory"
            info = zipfile.ZipInfo(name + ("/" if directory else ""), ZIP_DATE)
            info.create_system = 3
            info.external_attr = (
                ((stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)) << 16
            ) | (0x10 if directory else 0)
            info.compress_type = (
                zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
            )
            content = b"" if directory else (stage_root / item.path).read_bytes()
            if not directory and (
                item.sha256 != hashlib.sha256(content).hexdigest()
                or item.size != len(content)
            ):
                raise ValueError("captured workspace bytes changed before packaging")
            archive.writestr(
                info, content, compress_type=info.compress_type, compresslevel=9
            )
    content = output.getvalue()
    checked = inspect_addon_zip(content, addon_id)
    digest, file_count, total_bytes = bridge_manifest_for_zip(content, addon_id)
    if (digest, file_count, total_bytes) != (
        manifest.digest,
        sum(item.entry_type == "file" for item in manifest.entries),
        manifest.included_bytes,
    ):
        raise ValueError("captured workspace manifest differs from bridge format")
    return WorkspaceRepositoryPackage(
        content=content,
        zip_sha256=checked.sha256,
        package_tree_sha256=checked.tree_sha256,
        installed_manifest_digest=digest,
        file_count=file_count,
        total_bytes=total_bytes,
    )

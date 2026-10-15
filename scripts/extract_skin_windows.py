#!/usr/bin/env python3
"""Extract review-only custom-window candidates from a local Kodi skin tree.

No skin code is imported or executed. The report deliberately uses a different
kind from resolver mapping documents: extraction does not establish semantics,
Kodi compatibility, or verification. See docs/WINDOW-MAPPINGS.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

WINDOW_HOME = 10000
WINDOW_INVALID = 9999
LOADER_SOURCE = (
    "https://github.com/xbmc/xbmc/blob/21.3-Omega/"
    "xbmc/application/ApplicationSkinHandling.cpp#L238-L300"
)
_LEGACY_FOLDERS = frozenset({"pal", "pal16x9", "ntsc", "ntsc16x9", "720p", "1080i"})
_REFERENCES = re.compile(
    r"\b(ActivateWindow(?:AndFocus)?|ReplaceWindow|"
    r"Window\.(?:IsActive|IsVisible|IsTopMost))\(\s*([^,\s)]+)",
    re.IGNORECASE,
)


class ExtractionError(ValueError):
    """Unsafe, malformed, or excessively large source input."""


class _TotalSizeExceeded(ExtractionError):
    pass


@dataclass
class _ByteBudget:
    remaining: int

    def consume(self, size: int) -> None:
        if size > self.remaining:
            raise _TotalSizeExceeded("total XML size limit exceeded")
        self.remaining -= size


@dataclass(frozen=True)
class ExtractionLimits:
    max_file_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_files: int = 2048
    max_directory_entries: int = 8192
    max_resolution_folders: int = 32
    max_xml_nodes: int = 100_000
    max_xml_depth: int = 64
    max_references: int = 20_000

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("extraction limits must be positive integers")


class _BoundedTreeBuilder(ET.TreeBuilder):
    def __init__(self, limits: ExtractionLimits) -> None:
        super().__init__()
        self.limits = limits
        self.depth = 0
        self.nodes = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.depth += 1
        self.nodes += 1
        if self.depth > self.limits.max_xml_depth:
            raise ExtractionError("XML depth limit exceeded")
        if self.nodes > self.limits.max_xml_nodes:
            raise ExtractionError("XML node limit exceeded")
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        element = super().end(tag)
        self.depth -= 1
        return element

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ExtractionError("DTD and entity declarations are not allowed")


def _read_xml(
    path: Path, limits: ExtractionLimits, budget: _ByteBudget
) -> tuple[ET.Element, bytes]:
    """Read a regular file without following its final symlink or blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ExtractionError("XML input must be a regular file")
            if info.st_size > limits.max_file_bytes:
                raise ExtractionError("XML file size limit exceeded")
            if info.st_size > budget.remaining:
                raise _TotalSizeExceeded("total XML size limit exceeded")
            data = stream.read(min(limits.max_file_bytes, budget.remaining) + 1)
    except OSError as exc:
        raise ExtractionError("XML file is unreadable or a symlink") from exc
    if len(data) > limits.max_file_bytes:
        raise ExtractionError("XML file size limit exceeded")
    # Account for malformed XML too, and for files which grew during the read.
    budget.consume(len(data))
    parser = ET.XMLParser(target=_BoundedTreeBuilder(limits))
    try:
        root = ET.fromstring(data, parser=parser)
    except (ET.ParseError, LookupError, UnicodeError) as exc:
        raise ExtractionError("malformed or unsupported XML") from exc
    return root, data


def _directory_entries(path: Path, limit: int) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(path) as iterator:
            entries: list[os.DirEntry[str]] = []
            for entry in iterator:
                if len(entries) >= limit:
                    raise ExtractionError("directory entry limit exceeded")
                entries.append(entry)
    except OSError as exc:
        raise ExtractionError("skin resolution directory is unreadable") from exc
    return sorted(entries, key=lambda entry: entry.name)


def _resolution_folders(
    source: Path, skin: ET.Element, limits: ExtractionLimits
) -> list[Path]:
    folders = {res.get("folder", "") for res in skin.findall("res")}
    if not folders:
        # Kodi's legacy resolution fallback recognizes only these directory names.
        folders = {
            entry.name
            for entry in _directory_entries(source, limits.max_directory_entries)
            if entry.name.lower() in _LEGACY_FOLDERS
            and entry.is_dir(follow_symlinks=False)
        }
    if len(folders) > limits.max_resolution_folders:
        raise ExtractionError("resolution folder limit exceeded")
    paths = []
    for folder in sorted(folders):
        relative = PurePosixPath(folder)
        if (
            not folder
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in folder
        ):
            raise ExtractionError("unsafe resolution folder")
        path = source
        for part in relative.parts:
            path /= part
            if path.is_symlink():
                raise ExtractionError("symlink resolution folders are not allowed")
        if not path.is_dir():
            raise ExtractionError(f"missing resolution folder: {folder}")
        paths.append(path)
    if not paths:
        raise ExtractionError("no declared or legacy skin resolution folders found")
    return paths


def _direct_text(root: ET.Element, name: str) -> str | None:
    child = root.find(name)
    if child is None or not child.text:
        return None
    return child.text.strip()[:240] or None


def _filename_name(path: Path, xml_id: int) -> str:
    text = re.sub(r"^custom", "", path.stem, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"[\W_]+", " ", text).strip()
    text = re.sub(r"^\d+\s*|\s*\d+$", "", text).strip()
    return text[:240] or f"Custom window {xml_id}"


def _definition(root: ET.Element, relative: Path, data: bytes) -> dict[str, Any]:
    raw_id = root.get("id")
    if raw_id is None:
        raw_id = _direct_text(root, "id")
    if raw_id is None or not re.fullmatch(r"\d{1,10}", raw_id.strip()):
        raise ExtractionError("custom window requires an explicit decimal ID")
    xml_id = int(raw_id)
    if xml_id == WINDOW_INVALID:
        raise ExtractionError("XML ID 9999 is Kodi's invalid-window sentinel")
    if xml_id > 2_147_483_647 - WINDOW_HOME:
        raise ExtractionError("custom window runtime ID would exceed a signed integer")
    declared_type = (root.get("type") or _direct_text(root, "type") or "window").lower()
    names = [_filename_name(relative, xml_id)]
    for key in ("label", "title", "heading"):
        value = _direct_text(root, key)
        if value and value not in names:
            names.append(value)
    issues = ["semantics_require_review", "Kodi_compatibility_requires_review"]
    if not 9000 <= xml_id <= 9998:
        issues.append("outside_documented_custom_XML_ID_range")
    if any("$" in name for name in names):
        issues.append("dynamic_name_candidate")
    return {
        "xml_id": xml_id,
        "window_id": xml_id + WINDOW_HOME,
        "declared_type": declared_type,
        "name_candidates": names,
        "source_file": relative.as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "issues": issues,
    }


def extract_skin_windows(
    source: Path,
    *,
    source_url: str | None = None,
    source_revision: str | None = None,
    limits: ExtractionLimits | None = None,
) -> dict[str, Any]:
    """Return bounded, deterministic candidates; no entry is marked verified.

    Malformed addon.xml and global processing limits fail the extraction. Invalid
    individual XML files are reported and skipped; a partial report is explicit.
    Repeated IDs remain one candidate with all definitions and conflict flags.
    Compatibility is intentionally not inferred from an xbmc.gui dependency.
    """
    bounds = limits or ExtractionLimits()
    if source.is_symlink() or not source.is_dir():
        raise ExtractionError("source must be a local directory, not a symlink")
    source = source.resolve(strict=True)
    budget = _ByteBudget(bounds.max_total_bytes)
    addon, addon_data = _read_xml(source / "addon.xml", bounds, budget)
    skin_id, version = addon.get("id", ""), addon.get("version", "")
    if addon.tag != "addon" or not re.fullmatch(r"skin\.[a-zA-Z0-9._-]+", skin_id):
        raise ExtractionError("addon.xml must declare a skin add-on ID")
    if not version or len(version) > 128 or len(skin_id) > 128:
        raise ExtractionError("addon.xml must declare a bounded skin version and ID")
    skins = [
        item
        for item in addon.findall("extension")
        if item.get("point") == "xbmc.gui.skin"
    ]
    if len(skins) != 1:
        raise ExtractionError("addon.xml must contain exactly one skin extension")
    folders = _resolution_folders(source, skins[0], bounds)
    files: set[Path] = set()
    warnings: list[dict[str, str]] = []
    for folder in folders:
        for entry in _directory_entries(folder, bounds.max_directory_entries):
            if Path(entry.name).suffix.lower() != ".xml":
                continue
            relative = Path(entry.path).relative_to(source).as_posix()
            if not entry.is_file(follow_symlinks=False):
                warnings.append({"file": relative, "issue": "non_regular_XML_skipped"})
                continue
            files.add(Path(entry.path))
            if len(files) > bounds.max_files:
                raise ExtractionError("XML file count limit exceeded")
    definitions: dict[int, list[dict[str, Any]]] = {}
    references: list[dict[str, Any]] = []
    for path in sorted(files):
        relative_path = path.relative_to(source)
        relative = relative_path.as_posix()
        try:
            root, data = _read_xml(path, bounds, budget)
        except _TotalSizeExceeded:
            raise
        except ExtractionError as exc:
            warnings.append({"file": relative, "issue": str(exc)})
            continue
        if path.name.lower().startswith("custom"):
            try:
                if root.tag.lower() != "window":
                    raise ExtractionError("custom window root must be window")
                definition = _definition(root, relative_path, data)
                definitions.setdefault(definition["window_id"], []).append(definition)
            except ExtractionError as exc:
                warnings.append({"file": relative, "issue": str(exc)})
        # References supply review context only; their labels never prove semantics.
        for element in root.iter():
            texts = [element.text or "", *element.attrib.values()]
            for text in texts:
                for match in _REFERENCES.finditer(text):
                    if len(references) >= bounds.max_references:
                        raise ExtractionError("activation reference limit exceeded")
                    target = match[2].strip("'\"")[:240]
                    reference_id = None
                    if target.isdecimal() and len(target) <= 10:
                        numeric_id = int(target)
                        reference_id = (
                            numeric_id
                            if numeric_id > 9999
                            else numeric_id + WINDOW_HOME
                        )
                    references.append(
                        {
                            "file": relative,
                            "action": match[1],
                            "target": target,
                            "window_id": reference_id,
                            "context": text.strip()[:240],
                        }
                    )
    entries: list[dict[str, Any]] = []
    for window_id, items in sorted(definitions.items()):
        names = sorted({name for item in items for name in item["name_candidates"]})
        types = {item["declared_type"] for item in items}
        issues = {issue for item in items for issue in item["issues"]}
        if len(items) > 1:
            issues.add("duplicate_window_id")
            if (
                len(types) > 1
                or len({tuple(item["name_candidates"]) for item in items}) > 1
            ):
                issues.add("conflicting_definitions")
        filenames = {
            value.lower()
            for item in items
            for value in (
                Path(item["source_file"]).name,
                Path(item["source_file"]).stem,
            )
        }
        entries.append(
            {
                "xml_id": items[0]["xml_id"],
                "window_id": window_id,
                "candidate_name": names[0],
                "name_candidates": names,
                "role": "dialog"
                if len(types) == 1 and types <= {"dialog", "submenu", "buttonmenu"}
                else "unknown",
                "verification": "unverified",
                "source_files": [item["source_file"] for item in items],
                "definitions": items,
                "activation_references": [
                    ref
                    for ref in references
                    if ref["window_id"] == window_id
                    or ref["target"].lower() in filenames
                ],
                "issues": sorted(issues),
            }
        )
    return {
        "schema_version": 1,
        "kind": "skin_mapping_candidates",
        "skin_id": skin_id,
        "skin_version": version,
        "skin_name": addon.get("name"),
        "kodi_versions": [],
        "dependencies": [
            dict(item.attrib) for item in addon.findall("requires/import")
        ],
        "source": {
            "repository": source_url,
            "revision": source_revision,
            "addon_xml_sha256": hashlib.sha256(addon_data).hexdigest(),
            "runtime_translation_source": LOADER_SOURCE,
        },
        "resolution_folders": [path.relative_to(source).as_posix() for path in folders],
        "files_scanned": len(files),
        "status": "partial" if warnings else "candidates_require_review",
        "warnings": warnings,
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="local skin source directory")
    parser.add_argument("--source-url", help="upstream repository URL (recorded only)")
    parser.add_argument("--source-revision", help="reviewed upstream commit or tag")
    args = parser.parse_args(argv)
    try:
        result = extract_skin_windows(
            args.source,
            source_url=args.source_url,
            source_revision=args.source_revision,
        )
    except (ExtractionError, OSError) as exc:
        print(f"skin extraction failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

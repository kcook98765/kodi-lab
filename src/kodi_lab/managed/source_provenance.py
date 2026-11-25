"""Broker-owned source membership for one stopped profile."""

import json
from pathlib import Path

from .packages import HEX, addon_id, safe_name
from .protocol import Refusal
from .runtime import atomic

type SourceFiles = dict[str, str]
type ProfileSources = dict[str, SourceFiles]
MAX_SOURCE_METADATA = 2 * 1024**2


def validate_sources(value: object) -> ProfileSources:
    if not isinstance(value, dict):
        raise Refusal("invalid_source_provenance")
    result: ProfileSources = {}
    for addon, entries in value.items():
        addon_id(addon)
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 3000:
            raise Refusal("invalid_source_provenance")
        if "addon.xml" not in entries:
            raise Refusal("invalid_source_provenance")
        files: SourceFiles = {}
        for name, digest in entries.items():
            if (
                not isinstance(name, str)
                or not isinstance(digest, str)
                or not HEX.fullmatch(digest)
            ):
                raise Refusal("invalid_source_provenance")
            files[str(safe_name(name))] = digest
        result[addon] = files
    return result


def source_file(root: Path, slot: str) -> Path:
    return root / "source-provenance" / (slot + ".json")


def read_sources(root: Path, slot: str) -> ProfileSources:
    file = source_file(root, slot)
    if not file.exists() and not file.is_symlink():
        return {}
    if (
        file.is_symlink()
        or not file.is_file()
        or file.stat().st_size > MAX_SOURCE_METADATA
    ):
        raise Refusal("invalid_source_provenance")
    try:
        return validate_sources(json.loads(file.read_bytes()))
    except (ValueError, UnicodeError) as error:
        raise Refusal("invalid_source_provenance") from error


def write_sources(root: Path, slot: str, sources: ProfileSources) -> None:
    data = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_SOURCE_METADATA:
        raise Refusal("source_provenance_limit")
    if not sources:
        return
    file = source_file(root, slot)
    file.parent.mkdir(mode=0o700, exist_ok=True)
    atomic(file, data)

#!/usr/bin/env python3
"""Canonicalize the OCI-style tar produced by Docker save for a V1 candidate.

Root JSON and tar framing are transport metadata. Content-addressed image
configs, manifests, and layers are copied byte for byte and checked by digest.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from typing import Any

BLOB = re.compile(r"blobs/sha256/([0-9a-f]{64})\Z")
ROOT_JSON = frozenset({"manifest.json", "index.json", "oci-layout", "repositories"})
MAX_ROOT_JSON = 4 * 1024 * 1024
COPY_BYTES = 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Docker archive JSON contains duplicate keys")
        result[key] = value
    return result


def _json(archive: tarfile.TarFile, member: tarfile.TarInfo) -> Any:
    if member.size > MAX_ROOT_JSON:
        raise ValueError("Docker archive root JSON is too large")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("Docker archive root JSON is absent")
    return json.loads(stream.read(), object_pairs_hook=_object)


def _members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive:
        name = member.name.rstrip("/") if member.isdir() else member.name
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or name in members
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError("Docker archive member is unsafe or duplicated")
        members[name] = member
    if "manifest.json" not in members or not members["manifest.json"].isfile():
        raise ValueError("Docker archive has no manifest")
    for name, member in members.items():
        if member.isfile() and name not in ROOT_JSON and BLOB.fullmatch(name) is None:
            raise ValueError("unsupported Docker archive member")
    return members


def _blob(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], path: str
) -> None:
    match = BLOB.fullmatch(path)
    member = members.get(path)
    if match is None or member is None or not member.isfile():
        raise ValueError("Docker archive blob is absent or invalid")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("Docker archive blob is absent")
    digest = hashlib.sha256()
    while block := stream.read(COPY_BYTES):
        digest.update(block)
    if digest.hexdigest() != match[1]:
        raise ValueError("Docker archive blob digest differs")


def _root_content(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo]
) -> dict[str, bytes]:
    manifest = _json(archive, members["manifest.json"])
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Docker archive manifest is invalid")
    tags: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("Config"), str):
            raise ValueError("Docker archive image descriptor is invalid")
        config = item["Config"]
        if BLOB.fullmatch(config) is None or config not in members:
            raise ValueError("Docker archive image config is absent")
        image_tags = item.get("RepoTags")
        if image_tags is None:
            image_tags = []
        layers = item.get("Layers")
        if layers is None:
            layers = []
        if (
            not isinstance(image_tags, list)
            or not all(isinstance(tag, str) and tag for tag in image_tags)
            or not isinstance(layers, list)
            or not all(isinstance(layer, str) and layer in members for layer in layers)
            or any(BLOB.fullmatch(layer) is None for layer in layers)
        ):
            raise ValueError("Docker archive image tags or layers are invalid")
        if tags.intersection(image_tags) or len(image_tags) != len(set(image_tags)):
            raise ValueError("Docker archive image tag is duplicated")
        tags.update(image_tags)
        if "RepoTags" in item and item["RepoTags"] is not None:
            item["RepoTags"] = sorted(image_tags)
    content = {"manifest.json": _canonical(sorted(manifest, key=_canonical))}
    if "index.json" in members:
        index = _json(archive, members["index.json"])
        if not isinstance(index, dict) or not isinstance(index.get("manifests"), list):
            raise ValueError("Docker archive OCI index is invalid")
        for item in index["manifests"]:
            if not isinstance(item, dict) or not isinstance(item.get("digest"), str):
                raise ValueError("Docker archive OCI descriptor is invalid")
            digest = item["digest"]
            path = "blobs/sha256/" + digest.removeprefix("sha256:")
            if (
                not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or path not in members
                or not members[path].isfile()
                or item.get("size") != members[path].size
            ):
                raise ValueError("Docker archive OCI descriptor differs")
        index["manifests"] = sorted(index["manifests"], key=_canonical)
        content["index.json"] = _canonical(index)
    if ("index.json" in members) != ("oci-layout" in members):
        raise ValueError("Docker archive OCI index and layout differ")
    if "oci-layout" in members:
        layout = _json(archive, members["oci-layout"])
        if layout != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Docker archive OCI layout is invalid")
        content["oci-layout"] = _canonical(layout)
    if "repositories" in members:
        repositories = _json(archive, members["repositories"])
        if not isinstance(repositories, dict):
            raise ValueError("Docker archive repositories metadata is invalid")
        content["repositories"] = _canonical(repositories)
    return content


def canonicalize(source: Path, destination: Path) -> None:
    """Write one canonical archive; retain every content-addressed blob byte."""
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("Docker archive output must be a new path")
    with tarfile.open(source, "r:") as original:
        members = _members(original)
        root = _root_content(original, members)
        for name in members:
            if BLOB.fullmatch(name):
                _blob(original, members, name)
        files = {name for name, member in members.items() if member.isfile()}
        dirs = {
            "/".join(name.split("/")[:depth])
            for name in files
            for depth in range(1, len(name.split("/")))
        }
        if any(member.isdir() and name not in dirs for name, member in members.items()):
            raise ValueError("Docker archive has an unsupported directory")
        try:
            with tarfile.open(destination, "w:", format=tarfile.PAX_FORMAT) as output:
                for name in sorted(dirs, key=lambda value: (value.count("/"), value)):
                    entry = tarfile.TarInfo(name)
                    entry.type = tarfile.DIRTYPE
                    entry.mode = 0o755
                    output.addfile(entry)
                for name in sorted(files):
                    entry = tarfile.TarInfo(name)
                    entry.mode = 0o644
                    if name in root:
                        content = root[name]
                        entry.size = len(content)
                        output.addfile(entry, io.BytesIO(content))
                    else:
                        member = members[name]
                        entry.size = member.size
                        stream = original.extractfile(member)
                        if stream is None:
                            raise ValueError("Docker archive member is unreadable")
                        output.addfile(entry, stream)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    canonicalize(args.source, args.destination)


if __name__ == "__main__":
    main()

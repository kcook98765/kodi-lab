"""Passive repository descriptors admitted independently of video plugins."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from kodi_lab.development.addon_repository import (
    _canonicalize_https_url,
    canonicalize_repository_url,
)
from kodi_lab.development.sources import parse_captured_addon_xml

_VERSION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _version(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise ValueError("repository directory version is unsupported")
    parts = tuple(int(item) for item in value.split("."))
    return parts + (0,) * (4 - len(parts))


def repository_zip_urls(addon_id: str, descriptor: bytes) -> tuple[str, ...]:
    """Validate a bounded data-only descriptor; never resolve or fetch URLs here.

    The common parser has already rejected entities/DTDs, malformed identity,
    excessive extensions and dependencies. Returning URLs does not trust future
    catalogs or establish a network sandbox for Kodi.
    """

    observed = parse_captured_addon_xml(addon_id, descriptor)
    if not _IDENTITY.fullmatch(observed.observed_version):
        raise ValueError("repository version is unsafe")
    if any(item.dependency_id != "xbmc.addon" for item in observed.dependencies):
        raise ValueError("repository requires a non-system dependency")
    document = ET.fromstring(descriptor)
    extensions = document.findall("extension")
    executable = [e for e in extensions if e.get("point") != "xbmc.addon.metadata"]
    if (
        len(executable) != 1
        or executable[0].get("point") != "xbmc.addon.repository"
        or set(executable[0].attrib) - {"point", "name"}
        or len(executable[0].get("name", "")) > 128
    ):
        raise ValueError("repository must have exactly one passive extension")
    if any(
        e.get("point") == "xbmc.addon.metadata"
        and (set(e.attrib) != {"point"} or e.find("provides") is not None)
        for e in extensions
    ):
        raise ValueError("repository metadata must be passive")
    extension = executable[0]
    directories = list(extension)
    if not 1 <= len(directories) <= 16 or any(d.tag != "dir" for d in directories):
        raise ValueError("repository requires bounded explicit directory blocks")
    urls: list[str] = []
    ranges: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for directory in directories:
        if set(directory.attrib) - {"minversion", "maxversion"}:
            raise ValueError("repository directory attributes are unsupported")
        lower = _version(directory.get("minversion", "0"))
        upper = _version(directory.get("maxversion", "99999.99999.99999.99999"))
        if lower > upper or any(
            lower <= end and start <= upper for start, end in ranges
        ):
            raise ValueError("repository directory bounds overlap or are inverted")
        ranges.append((lower, upper))
        fields = list(directory)
        names = [f.tag for f in fields]
        if (
            len(set(names)) != len(names)
            or set(names) - {"info", "checksum", "datadir", "artdir", "hashes"}
            or not {"info", "checksum", "datadir", "hashes"} <= set(names)
        ):
            raise ValueError("repository directory fields are invalid")
        for field in fields:
            value = (field.text or "").strip()
            if list(field) or not value or len(value.encode()) > 2048:
                raise ValueError("repository field is malformed")
            attributes = dict(field.attrib)
            if field.tag == "hashes":
                if attributes or value not in {"sha256", "sha512"}:
                    raise ValueError("repository package hashes must be strong")
                continue
            if field.tag == "checksum":
                if attributes not in ({"verify": "sha256"}, {"verify": "sha512"}):
                    raise ValueError("repository checksum verification is required")
            elif field.tag == "info":
                if attributes not in (
                    {},
                    {"compressed": "false"},
                    {"compressed": "true"},
                ):
                    raise ValueError("repository info attributes are unsupported")
            elif field.tag == "datadir":
                if attributes not in ({}, {"zip": "true"}):
                    raise ValueError(
                        "repository package directory attributes are unsupported"
                    )
            elif attributes:
                raise ValueError("repository URL attributes are unsupported")
            if field.tag in {"datadir", "artdir"} and not value.endswith("/"):
                raise ValueError("repository directory URL must end with a slash")
            parsed = (
                canonicalize_repository_url(value)
                if field.tag == "info"
                else _canonicalize_https_url(
                    value + ".directory"
                    if field.tag in {"datadir", "artdir"}
                    else value,
                    require_index=False,
                )
            )
            if "." not in parsed.hostname or parsed.hostname.endswith(
                (".local", ".localhost", ".internal", ".invalid", ".test")
            ):
                raise ValueError("repository hostname is not public")
            urls.append(value)
    return tuple(urls)

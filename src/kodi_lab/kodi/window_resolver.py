"""Offline, version-scoped window semantics. Mapping files are data, never code."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kodi_lab.domain.kodi_release import KodiReleaseIdentity

type WindowRole = Literal[
    "home",
    "library",
    "dialog",
    "settings",
    "playback",
    "navigation",
    "system",
    "unknown",
]

BUILTIN_ROOT = Path(__file__).resolve().parent.parent / "window_mappings"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 256


class _SkinFields(TypedDict):
    skin_id: str | None
    skin_version: str | None


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MappingSource(_Record):
    repository: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    urls: list[str] = Field(min_length=1, max_length=20)


class MappingEntry(_Record):
    window_id: int = Field(ge=1, le=2**31 - 1)
    name: str = Field(min_length=1, max_length=160, pattern=r"^\S(?:[^\r\n]*\S)?$")
    role: WindowRole
    verification: Literal["verified", "ambiguous", "unverified"]
    symbolic_name: str | None = Field(default=None, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    notes: str | None = Field(default=None, max_length=4000)
    source_files: list[str] = Field(default_factory=list, max_length=100)
    kodi_min_version: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _release_bound(self) -> Self:
        if self.kodi_min_version:
            KodiReleaseIdentity.parse(self.kodi_min_version)
        return self


class MappingDocument(_Record):
    schema_version: Literal[1]
    kind: Literal["kodi", "skin"]
    kodi_versions: list[int] = Field(min_length=1, max_length=4)
    skin_id: str | None = Field(default=None, pattern=r"^skin\.[a-z0-9._-]+$")
    skin_versions: list[str] = Field(default_factory=list, max_length=100)
    source: MappingSource
    entries: list[MappingEntry] = Field(max_length=4096)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (
            len(set(self.kodi_versions)) != len(self.kodi_versions)
            or any(major not in (19, 20, 21, 22) for major in self.kodi_versions)
            or len({entry.window_id for entry in self.entries}) != len(self.entries)
        ):
            raise ValueError("unsupported Kodi version or duplicate version/window ID")
        if self.kind == "skin":
            if (
                not self.skin_id
                or not self.skin_versions
                or len(set(self.skin_versions)) != len(self.skin_versions)
                or any(not v or len(v) > 80 for v in self.skin_versions)
            ):
                raise ValueError("skin mappings require exact skin ID and versions")
        elif self.skin_id is not None or self.skin_versions:
            raise ValueError("core mappings cannot be skin scoped")
        return self


class WindowResolution(_Record):
    name: str | None = Field(default=None, max_length=160)
    role: WindowRole = "unknown"
    source: Literal[
        "reported", "core_mapping", "skin_mapping", "classification", "unknown"
    ]
    status: Literal[
        "reported", "verified", "ambiguous", "unverified", "classified", "unknown"
    ]
    skin_id: str | None = Field(default=None, max_length=160)
    skin_version: str | None = Field(default=None, max_length=80)


def kodi_major(version: str | None) -> int | None:
    match = re.match(r"^(19|20|21|22)(?:\.|$)", version or "")
    return int(match[1]) if match else None


def release_matches(entry: MappingEntry, version: str | None) -> bool:
    if entry.kodi_min_version is None:
        return True
    try:
        current = KodiReleaseIdentity.parse(version or "")
        minimum = KodiReleaseIdentity.parse(entry.kodi_min_version)
    except ValueError:
        return False

    def rank(release: KodiReleaseIdentity) -> tuple[int, int, int, int]:
        return (
            release.major,
            release.minor,
            {"alpha": 0, "beta": 1, "rc": 2, "stable": 3}[release.channel],
            release.prerelease_ordinal or 0,
        )

    return rank(current) >= rank(minimum)


def useful_reported_name(label: str | None, window_id: int | None = None) -> bool:
    # System.CurrentWindow localizes the numeric ID. Dynamic IDs collide with
    # unrelated UI strings (e.g. 13000 = "System", 14000 = "Stack").
    if window_id is not None and (
        13000 <= window_id <= 13099 or 14000 <= window_id <= 14099
    ):
        return False
    if not label or not label.strip():
        return False
    value = label.strip().casefold()
    return (
        not value.endswith(".xml")
        and value
        not in {
            "unknown",
            "none",
            "null",
            "window",
            "dialog",
            "addon",
            "custom",
        }
        and not re.fullmatch(
            r"(?:window|dialog|custom(?: window)?)?\s*[-_#:]?\s*\d+(?:\.xml)?", value
        )
    )


def read_document(path: Path) -> MappingDocument:
    """Read regular bounded JSON; reject duplicate keys, links and special files."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            raise ValueError("mapping is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("mapping exceeds byte limit")
    finally:
        os.close(descriptor)

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return MappingDocument.model_validate(json.loads(raw, object_pairs_hook=unique))


class WindowResolver:
    """Immutable snapshot. Restart/reconstruct to reload local administrator data."""

    def __init__(
        self, *, builtin_root: Path = BUILTIN_ROOT, local_root: Path | None = None
    ) -> None:
        self._core: dict[tuple[int, int], tuple[MappingDocument, MappingEntry]] = {}
        self._skins: dict[
            tuple[str, str, int, int], tuple[MappingDocument, MappingEntry]
        ] = {}
        self.documents: list[MappingDocument] = []
        self.errors: list[str] = []
        for folder in (builtin_root / "kodi", builtin_root / "skins"):
            for path in sorted(folder.glob("*.json")):
                document = read_document(
                    path
                )  # bundled invalid data is a development error
                self._add(document, local=False)
        protected = {key[1] for key in self._core}
        if local_root is not None:
            try:
                # Bounded directory enumeration, including invalid files in the limit.
                with os.scandir(local_root) as iterator:
                    paths = []
                    for item in iterator:
                        if item.name.endswith(".json"):
                            paths.append(Path(item.path))
                            if len(paths) > MAX_FILES:
                                raise ValueError("too many local mapping files")
                for path in sorted(paths):
                    try:
                        document = read_document(path)
                        if document.kind != "skin" or any(
                            e.window_id in protected for e in document.entries
                        ):
                            raise ValueError(
                                "local mappings cannot override core definitions"
                            )
                        self._add(document, local=True)
                    except (OSError, ValueError, RecursionError, ValidationError):
                        self.errors.append(f"rejected local mapping: {path.name[:160]}")
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                self.errors.append("local mapping directory unavailable or over limit")
        self._skin_candidates = {(key[2], key[3]) for key in self._skins}
        self._skin_id_candidates = {(key[0], key[2], key[3]) for key in self._skins}
        self._skin_major_candidates = {(key[0], key[2]) for key in self._skins}
        if self.errors:
            logging.getLogger(__name__).warning(
                "Rejected %d local window mapping inputs", len(self.errors)
            )

    def _add(self, document: MappingDocument, *, local: bool) -> None:
        for entry in document.entries:
            for major in document.kodi_versions:
                if document.kind == "kodi":
                    key = (major, entry.window_id)
                    if key in self._core:
                        raise ValueError("overlapping bundled core mapping")
                    self._core[key] = document, entry
                else:
                    for version in document.skin_versions:
                        skin_key = (
                            document.skin_id or "",
                            version,
                            major,
                            entry.window_id,
                        )
                        if not local and skin_key in self._skins:
                            raise ValueError("overlapping bundled skin mapping")
                        self._skins[skin_key] = document, entry
        self.documents.append(document)

    def needs_skin(
        self, window_id: int | None, label: str | None, *, kodi_version: str | None
    ) -> bool:
        return bool(
            window_id is not None
            and not useful_reported_name(label, window_id)
            and (kodi_major(kodi_version), window_id) not in self._core
            and (kodi_major(kodi_version), window_id) in self._skin_candidates
        )

    def has_skin_mapping(
        self,
        window_id: int | None,
        *,
        kodi_version: str,
        skin_id: str,
        skin_version: str,
    ) -> bool:
        major = kodi_major(kodi_version)
        if major is None or window_id is None or (major, window_id) in self._core:
            return False
        found = self._skins.get((skin_id, skin_version, major, window_id))
        return bool(found and release_matches(found[1], kodi_version))

    def has_skin_id(
        self, window_id: int | None, *, kodi_version: str, skin_id: str
    ) -> bool:
        """Reject unrelated skins; None checks any ID for warm scheduling only.

        This does not establish exact version compatibility or window semantics;
        callers still require a freshly bracketed, matching full observation.
        """
        if window_id is None:
            return (skin_id, kodi_major(kodi_version)) in self._skin_major_candidates
        return (
            skin_id,
            kodi_major(kodi_version),
            window_id,
        ) in self._skin_id_candidates

    def resolve(
        self,
        window_id: int | None,
        label: str | None,
        *,
        kodi_version: str | None = None,
        skin_id: str | None = None,
        skin_version: str | None = None,
    ) -> WindowResolution:
        major = kodi_major(kodi_version)
        core = (
            self._core.get((major, window_id))
            if major is not None and window_id is not None
            else None
        )
        protected = core is not None
        if core and not release_matches(core[1], kodi_version):
            core = None
        context: _SkinFields = {"skin_id": skin_id, "skin_version": skin_version}
        if useful_reported_name(label, window_id):
            return WindowResolution(
                name=label,
                role=core[1].role if core else "unknown",
                source="reported",
                status="reported",
                **context,
            )
        found = core
        if (
            found is None
            and not protected
            and skin_id
            and skin_version
            and major is not None
            and window_id is not None
        ):
            found = self._skins.get((skin_id, skin_version, major, window_id))
            if found and not release_matches(found[1], kodi_version):
                found = None
        if found:
            _, entry = found
            return WindowResolution(
                name=entry.name if entry.verification == "verified" else None,
                role=entry.role if entry.verification == "verified" else "unknown",
                source="core_mapping" if core else "skin_mapping",
                status=entry.verification,
                **context,
            )
        if major is not None and window_id is not None:
            for low, high, name in (
                (11100, 11199, "Custom skin window"),
                (13000, 13099, "Python window"),
                (14000, 14099, "Add-on window"),
            ):
                if low <= window_id <= high:
                    return WindowResolution(
                        name=name,
                        source="classification",
                        status="classified",
                        **context,
                    )
        return WindowResolution(source="unknown", status="unknown", **context)


@lru_cache(maxsize=1)
def builtin_resolver() -> WindowResolver:
    return WindowResolver()

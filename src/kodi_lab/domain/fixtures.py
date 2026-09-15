"""Immutable predefined synthetic-fixture domain records."""

import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from kodi_lab.domain.kodi_release import KodiReleaseIdentity
from kodi_lab.domain.runtime_catalog import normalize_sha256_digest

_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class FixtureSizeCategory(StrEnum):
    SMALL = "small"


class FixtureContentCategory(StrEnum):
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class FixtureDefinition:
    """One finalized fixture version backed by trusted package resources."""

    id: UUID
    slug: str
    fixture_version: int
    size_category: FixtureSizeCategory
    content_category: FixtureContentCategory
    content_digest: str
    compatible_kodi_version: str
    description: str

    def __post_init__(self) -> None:
        if _SLUG_PATTERN.fullmatch(self.slug) is None:
            raise ValueError(
                "fixture slug must use lowercase letters, numbers, and '-'"
            )
        if self.fixture_version < 1:
            raise ValueError("fixture version must be positive")
        if not 1 <= len(self.description.strip()) <= 512:
            raise ValueError("fixture description must contain 1 to 512 characters")
        object.__setattr__(
            self,
            "content_digest",
            normalize_sha256_digest(self.content_digest, "fixture content digest"),
        )
        release = KodiReleaseIdentity.parse(self.compatible_kodi_version)
        object.__setattr__(self, "compatible_kodi_version", release.canonical)

    @property
    def compatible_kodi_release(self) -> KodiReleaseIdentity:
        return KodiReleaseIdentity.parse(self.compatible_kodi_version)

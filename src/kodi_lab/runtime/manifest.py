"""Validation for repository-owned runtime build provenance manifests."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kodi_lab.domain import KodiReleaseIdentity, normalize_sha256_digest


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BuildParameters(_StrictModel):
    context: str = "."
    dockerfile: str = "Dockerfile"
    platform: str = "linux/amd64"
    pull: bool = False
    no_cache: bool = False


class RuntimeBuildResult(_StrictModel):
    built_at: datetime
    image_id: str
    build_log_reference: str = Field(min_length=1, max_length=256)
    build_log_digest: str

    @field_validator("image_id", "build_log_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        normalized = normalize_sha256_digest(value)
        if value != normalized:
            raise ValueError("digest must use canonical lowercase sha256 form")
        return value


class SourceBuildProvenance(_StrictModel):
    repository_url: str = Field(min_length=1, max_length=256)
    release_tag: str = Field(min_length=1, max_length=128)
    commit_sha: str
    source_archive_url: str = Field(min_length=1, max_length=256)
    source_archive_digest: str
    kodi_version: str
    toolchain_packages: tuple[str, ...]
    cmake_options: tuple[str, ...]

    @field_validator("commit_sha")
    @classmethod
    def _commit_sha(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("source commit must be a full lowercase Git SHA")
        return value

    @field_validator("source_archive_digest")
    @classmethod
    def _archive_digest(cls, value: str) -> str:
        if value != normalize_sha256_digest(value):
            raise ValueError("source archive digest must be canonical")
        return value

    @field_validator("kodi_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != KodiReleaseIdentity.parse(value).canonical:
            raise ValueError("source release must use canonical Kodi identity")
        return value

    @field_validator("toolchain_packages")
    @classmethod
    def _toolchain(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() or len(item) > 128 for item in value):
            raise ValueError("source toolchain packages must be bounded and nonempty")
        return value

    @field_validator("cmake_options")
    @classmethod
    def _cmake_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(
            not item.startswith("-D") or len(item) > 160 for item in value
        ):
            raise ValueError("source CMake options must be bounded -D options")
        return value


class RuntimeBuildManifest(_StrictModel):
    schema_version: int = 1
    runtime_kind: str
    kodi_version: str
    base_image_reference: str = Field(min_length=1, max_length=256)
    base_image_digest: str
    package_sources: tuple[str, ...]
    kodi_package_version: str | None = None
    package_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    source_build: SourceBuildProvenance | None = None
    compatibility_assets: tuple[str, ...] = ()
    repository_file_hashes: dict[str, str]
    build: BuildParameters
    result: RuntimeBuildResult

    @field_validator("runtime_kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value != "kodi":
            raise ValueError("runtime_kind must be kodi")
        return value

    @field_validator("kodi_version")
    @classmethod
    def _version(cls, value: str) -> str:
        release = KodiReleaseIdentity.parse(value)
        if value != release.canonical:
            raise ValueError("kodi_version must use canonical release identity")
        return value

    @field_validator("base_image_digest")
    @classmethod
    def _base_digest(cls, value: str) -> str:
        normalized = normalize_sha256_digest(value)
        if value != normalized:
            raise ValueError("base digest must be canonical")
        return value

    @field_validator("package_sources")
    @classmethod
    def _sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value):
            raise ValueError("package_sources must be nonempty")
        return value

    @field_validator("compatibility_assets")
    @classmethod
    def _compatibility_assets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("compatibility_assets must be bounded and nonblank")
        return value

    @field_validator("package_artifact_hashes")
    @classmethod
    def _package_artifact_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        for name, digest in value.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or len(name) > 160:
                raise ValueError("package artifact names must be relative and bounded")
            if digest != normalize_sha256_digest(digest):
                raise ValueError("package artifact digests must be canonical")
        return value

    @model_validator(mode="after")
    def _base_reference_is_pinned(self) -> RuntimeBuildManifest:
        if f"@{self.base_image_digest}" not in self.base_image_reference:
            raise ValueError("base_image_reference must include base_image_digest")
        if not self.repository_file_hashes:
            raise ValueError("repository_file_hashes must be nonempty")
        for name, digest in self.repository_file_hashes.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("repository file paths must be relative and bounded")
            if digest != normalize_sha256_digest(digest):
                raise ValueError("repository file digests must be canonical")
        if self.source_build is not None:
            if self.kodi_package_version is not None or self.package_artifact_hashes:
                raise ValueError("source builds cannot declare a Kodi package artifact")
            if self.source_build.kodi_version != self.kodi_version:
                raise ValueError(
                    "source build release must match exact release identity"
                )
        else:
            if self.kodi_package_version is None:
                raise ValueError(
                    "package runtime requires an exact Kodi package version"
                )
            expected_ubuntu_prefix = f"2:{self.kodi_version}+dfsg-"
            team_kodi_old_source = any(
                "ppa.launchpadcontent.net/team-xbmc/kodi-old/ubuntu/" in source
                for source in self.package_sources
            )
            expected_team_kodi_old_prefix = f"6:{self.kodi_version}+git"
            package_matches_release = self.kodi_package_version.startswith(
                expected_ubuntu_prefix
            ) or (
                team_kodi_old_source
                and self.kodi_package_version.startswith(expected_team_kodi_old_prefix)
            )
            if not package_matches_release:
                raise ValueError(
                    "Kodi package version must match exact release identity"
                )
        return self

    def verify_repository_files(self, manifest_directory: Path) -> None:
        """Prove all declared repository-owned inputs match their hashes."""

        for relative_name, expected in self.repository_file_hashes.items():
            candidate = manifest_directory / relative_name
            if not candidate.is_file() or candidate.is_symlink():
                raise ValueError(f"runtime input {relative_name} is not a regular file")
            actual = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"runtime input {relative_name} digest mismatch")


def load_runtime_build_manifest(path: Path) -> RuntimeBuildManifest:
    """Load and validate one strict JSON runtime-build manifest."""

    data = json.loads(path.read_text(encoding="utf-8"))
    profile = RuntimeBuildManifest.model_validate(data)
    profile.verify_repository_files(path.parent)
    return profile

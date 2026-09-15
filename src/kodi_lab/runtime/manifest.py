"""Validation for repository-owned runtime build provenance manifests."""

from __future__ import annotations

import hashlib
import json
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


class RuntimeBuildManifest(_StrictModel):
    schema_version: int = 1
    runtime_kind: str
    kodi_version: str
    base_image_reference: str = Field(min_length=1, max_length=256)
    base_image_digest: str
    package_sources: tuple[str, ...]
    kodi_package_version: str
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
        expected_package_prefix = f"2:{self.kodi_version}+dfsg-"
        if not self.kodi_package_version.startswith(expected_package_prefix):
            raise ValueError("Kodi package version must match exact release identity")
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

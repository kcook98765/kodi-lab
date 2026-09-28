"""Wheel-owned, reviewed binary-package runtime recipes.

Recipe selection is a generation name. No caller supplies a Dockerfile, URL,
package, or build argument. A new provider type may add another discriminated
provider model without changing persisted catalog or installation schemas.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kodi_lab.domain import KodiReleaseIdentity, normalize_sha256_digest
from kodi_lab.runtime.manifest import RuntimeProfileManifest

_NAME = re.compile(r"[a-z][a-z0-9+.-]{0,63}\Z")
_PACKAGE_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]{0,95}\Z")
_FILE = re.compile(r"[a-z0-9][a-z0-9.+_-]{0,127}\Z")
# Each generation retains prior reviewed revisions. The last filename is the
# current install selection; existing exact pins resolve their recorded file.
_RECIPES = {"matrix": ("matrix-19.4-r1.json",)}
_ASSETS = (
    "entrypoint.sh",
    "healthcheck.sh",
    "profile/guisettings.xml",
)
REVIEWED_SIGNED_DISTRO_SOURCE_KIND = "reviewed-signed-distro-package"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CriticalPackage(_ClosedModel):
    name: str
    version: str
    architecture: Literal["amd64", "all"]
    archive_name: str
    sha256: str

    @model_validator(mode="after")
    def _exact_package(self) -> CriticalPackage:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError("critical package name is invalid")
        if _PACKAGE_VERSION.fullmatch(self.version) is None:
            raise ValueError("critical package version is invalid")
        if (
            not self.archive_name.startswith(f"{self.name}_")
            or not self.archive_name.endswith(f"_{self.architecture}.deb")
            or _FILE.fullmatch(self.archive_name) is None
        ):
            raise ValueError("critical package archive name is invalid")
        if self.sha256 != normalize_sha256_digest(self.sha256):
            raise ValueError("critical package digest is invalid")
        return self


class SignedDistroAptProvider(_ClosedModel):
    type: Literal["signed_distro_apt"]
    distribution: str = Field(min_length=1, max_length=64)
    suite: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    repository_url: str = Field(min_length=1, max_length=128)
    security_repository_url: str = Field(min_length=1, max_length=128)
    components: tuple[str, ...]
    pockets: tuple[str, ...]
    keyring_path: str
    keyring_sha256: str
    verification_policy: Literal["apt-signed-metadata-and-critical-sha256-v1"]

    @model_validator(mode="after")
    def _closed_sources(self) -> SignedDistroAptProvider:
        if self.distribution != "Ubuntu" or self.components != ("main", "universe"):
            raise ValueError("unsupported reviewed Ubuntu repository identity")
        if self.pockets != (
            self.suite,
            f"{self.suite}-updates",
            f"{self.suite}-security",
        ):
            raise ValueError("repository pockets differ from reviewed suite")
        if (
            self.repository_url != "http://archive.ubuntu.com/ubuntu"
            or self.security_repository_url != "http://security.ubuntu.com/ubuntu"
            or self.keyring_path != "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
            or self.keyring_sha256 != normalize_sha256_digest(self.keyring_sha256)
        ):
            raise ValueError("Ubuntu archive trust anchor differs")
        return self


class RuntimeRecipe(_ClosedModel):
    schema_version: Literal[1]
    recipe_id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,63}$")
    recipe_revision: int = Field(gt=0)
    generation: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    display_generation: str = Field(min_length=1, max_length=64)
    kodi_version: str
    platform: Literal["linux/amd64"]
    definition_id: UUID
    definition_slug: str
    base_image_reference: str = Field(min_length=1, max_length=256)
    base_image_digest: str
    provider: SignedDistroAptProvider
    critical_packages: tuple[CriticalPackage, ...]
    support_packages: tuple[str, ...]
    asset_hashes: dict[str, str]
    execution_profile: RuntimeProfileManifest
    validation_contract: Literal["kodi-runtime-v1"]
    source_references: tuple[str, ...]
    license_references: tuple[str, ...]

    @field_validator("kodi_version")
    @classmethod
    def _release(cls, value: str) -> str:
        if value != KodiReleaseIdentity.parse(value).canonical:
            raise ValueError("Kodi version is not canonical")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RuntimeRecipe:
        if (
            self.base_image_digest != normalize_sha256_digest(self.base_image_digest)
            or not self.base_image_reference.endswith("@" + self.base_image_digest)
            or _NAME.fullmatch(self.definition_slug) is None
            or self.execution_profile.expected_kodi_version != self.kodi_version
            or self.execution_profile.expected_version_tag != "stable"
            or self.execution_profile.runtime_uid != 1000
            or self.execution_profile.runtime_gid != 1000
            or self.execution_profile.jsonrpc_port != 8080
            or self.execution_profile.jsonrpc_path != "/jsonrpc"
            or self.execution_profile.python_bytecode_policy != "suppress"
        ):
            raise ValueError("recipe base or execution profile differs")
        names = [item.name for item in self.critical_packages]
        if len(names) != len(set(names)) or not {
            "kodi",
            "kodi-bin",
            "kodi-data",
        } <= set(names):
            raise ValueError("recipe critical package set is invalid")
        if len(self.support_packages) != len(set(self.support_packages)) or not all(
            _NAME.fullmatch(name) for name in self.support_packages
        ):
            raise ValueError("recipe support package set is invalid")
        if set(self.asset_hashes) != set(_ASSETS) or any(
            value != normalize_sha256_digest(value)
            for value in self.asset_hashes.values()
        ):
            raise ValueError("recipe asset identity is invalid")
        if (
            not self.source_references
            or not self.license_references
            or any(
                not value.startswith("https://") or len(value) > 256
                for value in (*self.source_references, *self.license_references)
            )
        ):
            raise ValueError("recipe references are invalid")
        return self


@dataclass(frozen=True, slots=True)
class ReviewedRecipe:
    recipe: RuntimeRecipe
    digest: str
    reference: str
    assets: dict[str, bytes]
    raw: bytes


def supported_generations() -> tuple[str, ...]:
    return tuple(_RECIPES)


def load_reviewed_recipe(generation: str) -> ReviewedRecipe:
    """Resolve the current packaged generation recipe for a new installation."""

    try:
        filename = _RECIPES[generation][-1]
    except KeyError as error:
        raise ValueError("generation has no reviewed runtime recipe") from error
    return _load_recipe_file(generation, filename)


def load_recorded_reviewed_recipe(
    generation: str, recipe_id: str, recipe_revision: int, digest: str
) -> ReviewedRecipe:
    """Resolve only a wheel-owned historical recipe matching a durable install."""

    try:
        filenames = _RECIPES[generation]
    except KeyError as error:
        raise ValueError("generation has no reviewed runtime recipe") from error
    for filename in filenames:
        reviewed = _load_recipe_file(generation, filename)
        if (
            reviewed.recipe.recipe_id == recipe_id
            and reviewed.recipe.recipe_revision == recipe_revision
            and reviewed.digest == digest
        ):
            return reviewed
    raise ValueError("recorded runtime recipe is no longer reviewed")


def _load_recipe_file(generation: str, filename: str) -> ReviewedRecipe:
    package = resources.files("kodi_lab.runtime")
    raw = package.joinpath("recipes", filename).read_bytes()
    recipe = RuntimeRecipe.model_validate_json(raw)
    if recipe.generation != generation:
        raise ValueError("recipe generation differs from reviewed selection")
    asset_root = package.joinpath("assembly_assets", filename.removesuffix(".json"))
    if not asset_root.is_dir():
        asset_root = package.joinpath("assembly_assets")
    assets = {
        name: asset_root.joinpath(*name.split("/")).read_bytes() for name in _ASSETS
    }
    for name, payload in assets.items():
        if "sha256:" + hashlib.sha256(payload).hexdigest() != recipe.asset_hashes[name]:
            raise ValueError("reviewed assembly asset differs")
    return ReviewedRecipe(
        recipe=recipe,
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        reference=f"kodi-lab-wheel:runtime/recipes/{filename}",
        assets=assets,
        raw=raw,
    )

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
from typing import Annotated, Final, Literal
from uuid import UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from kodi_lab.domain import KodiReleaseIdentity, normalize_sha256_digest
from kodi_lab.runtime.display_capabilities import EXPERIMENTAL_OMEGA_DISPLAY
from kodi_lab.runtime.manifest import RuntimeProfileManifest

_NAME = re.compile(r"[a-z][a-z0-9+.-]{0,63}\Z")
_PACKAGE_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]{0,95}\Z")
_FILE = re.compile(r"[a-z0-9][a-z0-9.+_-]{0,127}\Z")
# Each generation retains prior reviewed revisions. The last filename is the
# current install selection; existing exact pins resolve their recorded file.
_RECIPES = {
    "matrix": (
        (
            "matrix-19.4-r1.json",
            "sha256:306b3b22ce57d23987b20dd37738a253ef4511ae1754f231d3738efb0db3a482",
        ),
        (
            "matrix-19.4-r2.json",
            "sha256:2f962f50580c53e9aa98447975617b2bc18552f6d8a7e645f07d90fa19641e70",
        ),
    ),
    "nexus": (
        (
            "nexus-20.5-r1.json",
            "sha256:2cec5cbd0f72cd6c973bfa91091f6d340eddcd08336a500176ee91dcdfcac842",
        ),
        (
            "nexus-20.5-r2.json",
            "sha256:9845a930212f91c20e1313f03cc289882b8555f6067a5bf22e11e724b9821631",
        ),
    ),
    "omega": (
        (
            "omega-21.3-r1.json",
            "sha256:df950df28255f2ec40f2e69b812bb58f2fb8902fddbccf3a89da7a75e0af62c8",
        ),
        (
            "omega-21.3-r2.json",
            "sha256:4cb4a003da6d119018738db06aed6789c0acaee2b5ed89b210b93db5526d7140",
        ),
    ),
}
_ASSETS = (
    "entrypoint.sh",
    "healthcheck.sh",
    "profile/guisettings.xml",
)
_DISPLAY_GUARD_ASSET = "xvfb_guard.py"
REVIEWED_SIGNED_DISTRO_SOURCE_KIND = "reviewed-signed-distro-package"
REVIEWED_OSTREE_SOURCE_KIND = "reviewed-flathub-ostree"
NOVNC_PHASE1_DIGEST: Final = EXPERIMENTAL_OMEGA_DISPLAY.recipe_digest
HISTORICAL_PREVIEW_INSTALLATION_DIGEST: Final = (
    "sha256:9679a31a2ff4007c63e117319b7893054e475d5a24e69240a2c2e21adbfdd793"
)
PREVIEW_INSTALLATION_DIGEST: Final = (
    "sha256:75d32434f6208efc051bc71ff0d501f07e91a1fcd666eed6f8806d49d43e3454"
)


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


FLATHUB_PRIMARY_KEY: Final = "6E5C05D979C76DAF93C081354184DD4D907A7CAE"
FLATHUB_SIGNING_SUBKEY: Final = "54A6CDDD8919FB204200D8AC562702E9E3ED7EE8"
FLATHUB_KEY_SHA256 = "8bdc20abc4e19c0796460beb5bfe0e7aa4138716999e19c6f2dbdd78cc41aeaa"
KODI_RC1_SOURCE_REVISION: Final = "28ea2eac1eb7af8fdcbd2672d933ce87f594be79"
_INACTIVE_OSTREE_DIGEST = (
    "sha256:f1abc5dc6f04d143881b3da58fe745da0409273f7b0823c4121a18ebd017cd28"
)
# These are acquisition inputs, not installation or capability approvals.
_OSTREE_COMPONENTS = {
    "app": (
        "https://dl.flathub.org/beta-repo/",
        "app/tv.kodi.Kodi/x86_64/beta",
        "0a381e42781914ae76ed338d3aa11f9403c55c57f5c9c194e1f4fb437cf71630",
        "org.flathub.Beta",
        "d22840d60457caa455f01a90a9262d8f699d333ea13ed47b5a82f04af41dd0f6",
    ),
    "platform": (
        "https://dl.flathub.org/repo/",
        "runtime/org.freedesktop.Platform/x86_64/26.08",
        "92659297ffdb4f9f14dd1562bd919b48160b0dc42c99653dcfc0688096af41df",
        "org.flathub.Stable",
        "0ab22b227c5658600cb197ea0463e6735355d0adcd92303ba7d89c6a367758fc",
    ),
    "gl": (
        "https://dl.flathub.org/repo/",
        "runtime/org.freedesktop.Platform.GL.default/x86_64/26.08",
        "badc1097c419e73781cf098413ffa8e7bf7db3528572c11712265b9193921842",
        "org.flathub.Stable",
        "8802a8c0a93df4c521a62b9be924574c2c40bc8c03e48075387c795d308d781b",
    ),
}


class OstreeComponent(_ClosedModel):
    role: Literal["app", "platform", "gl"]
    repository_url: str = Field(max_length=64)
    ref: str = Field(max_length=128)
    commit: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection: str = Field(max_length=64)
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _reviewed_identity(self) -> OstreeComponent:
        if (
            self.repository_url,
            self.ref,
            self.commit,
            self.collection,
            self.metadata_sha256,
        ) != _OSTREE_COMPONENTS[self.role]:
            raise ValueError("OSTree component differs from reviewed RC1 identity")
        return self


class FlathubOstreeProvider(_ClosedModel):
    type: Literal["flathub_ostree"]
    architecture: Literal["x86_64"]
    primary_key_fingerprint: str = Field(pattern=r"^[0-9A-F]{40}$")
    signing_subkey_fingerprint: str = Field(pattern=r"^[0-9A-F]{40}$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_policy: Literal["ostree-gpg-pinned-closure-v1"]
    components: tuple[OstreeComponent, OstreeComponent, OstreeComponent]

    @model_validator(mode="after")
    def _closed_trust_and_closure(self) -> FlathubOstreeProvider:
        if (
            self.primary_key_fingerprint != FLATHUB_PRIMARY_KEY
            or self.signing_subkey_fingerprint != FLATHUB_SIGNING_SUBKEY
            or self.key_sha256 != FLATHUB_KEY_SHA256
            or tuple(item.role for item in self.components) != ("app", "platform", "gl")
        ):
            raise ValueError("OSTree trust or component closure differs")
        return self


# Independent parsing surface: existing v1 APT models and serialization stay intact.
AcquisitionProvider = Annotated[
    SignedDistroAptProvider | FlathubOstreeProvider, Field(discriminator="type")
]


class InactiveOstreeRecipe(_ClosedModel):
    schema_version: Literal[2]
    recipe_id: Literal["piers-22.0-rc1-r1"]
    recipe_revision: Literal[1]
    generation: Literal["piers"]
    kodi_version: Literal["22.0-rc.1"]
    kodi_source_revision: Literal["28ea2eac1eb7af8fdcbd2672d933ce87f594be79"]
    platform: Literal["linux/amd64"]
    support_tier: Literal["preview"]
    development_only: Literal[True]
    acquisition_trigger: Literal["explicit-user-install"]
    installation_status: Literal["inactive"]
    support_dependencies_status: Literal["unqualified"]
    image_assets_status: Literal["unqualified"]
    validation_status: Literal["unqualified"]
    provider: FlathubOstreeProvider


def parse_acquisition_provider(value: object) -> AcquisitionProvider:
    """Parse closed provider data; this does not select an installable recipe."""

    return TypeAdapter(AcquisitionProvider).validate_python(value)


class RuntimeRecipe(_ClosedModel):
    schema_version: Literal[1, 3]
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
    provider: AcquisitionProvider
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
        if isinstance(self.provider, FlathubOstreeProvider):
            from kodi_lab.runtime.ostree_assembly import load_assembly_assets
            from kodi_lab.runtime.ubuntu_support import BASE_IMAGE

            assets = load_assembly_assets(revision=self.recipe_revision)
            if (
                self.schema_version != 3
                or (self.recipe_id, self.recipe_revision)
                not in {("piers-22.0-rc1-r1", 1), ("piers-22.0-rc1-r2", 2)}
                or self.generation != "piers"
                or self.kodi_version != "22.0-rc.1"
                or self.base_image_reference != BASE_IMAGE
                or self.base_image_digest != BASE_IMAGE.split("@")[1]
                or self.critical_packages
                or self.support_packages
                or self.asset_hashes
                != {
                    k: "sha256:" + hashlib.sha256(v).hexdigest()
                    for k, v in assets.items()
                }
                or self.execution_profile.expected_kodi_version != "22.0"
                or self.execution_profile.expected_version_tag != "rc"
                or self.execution_profile.expected_prerelease_ordinal != 1
                or self.execution_profile.runtime_uid != 1000
                or self.execution_profile.runtime_gid != 1000
            ):
                raise ValueError("reviewed Preview installation identity differs")
            return self
        if self.schema_version != 1:
            raise ValueError("APT recipe schema differs")
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
        expected_assets = set(_ASSETS)
        if self.recipe_revision >= 2:
            expected_assets.add(_DISPLAY_GUARD_ASSET)
        from kodi_lab.runtime.display_capabilities import VIEWER_ASSET_RECIPES

        if (self.recipe_id, self.recipe_revision) in VIEWER_ASSET_RECIPES:
            expected_assets.add("novnc_view_helper.py")
        if set(self.asset_hashes) != expected_assets or any(
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


class AptRuntimeRecipe(RuntimeRecipe):
    """Preserve the typed stable recipe interface for existing callers."""

    schema_version: Literal[1]
    provider: SignedDistroAptProvider


@dataclass(frozen=True, slots=True)
class ReviewedAptRecipe(ReviewedRecipe):
    recipe: AptRuntimeRecipe


_TARGET_NAMESPACE = UUID("dc264ebb-d7c8-4ea7-8d54-77d5e7624e09")


def recipe_target_id(reviewed: ReviewedRecipe) -> UUID:
    """A new exact Kodi release gets a new target; old instance pins survive."""

    recipe = reviewed.recipe
    suffix = "-experimental-novnc" if reviewed.digest == NOVNC_PHASE1_DIGEST else ""
    return uuid5(
        _TARGET_NAMESPACE, f"managed-{recipe.generation}-{recipe.kodi_version}{suffix}"
    )


def supported_generations() -> tuple[str, ...]:
    return tuple(_RECIPES)


def load_reviewed_recipe(generation: str) -> ReviewedAptRecipe:
    """Resolve the current packaged generation recipe for a new installation."""

    return _reviewed_for_generation(generation)[-1]


def load_recorded_reviewed_recipe(
    generation: str, recipe_id: str, recipe_revision: int, digest: str
) -> ReviewedRecipe:
    """Resolve only a wheel-owned historical recipe matching a durable install."""

    entries: tuple[ReviewedRecipe, ...] = (
        (
            _load_recipe_file(
                "piers",
                "piers-22.0-rc1-installation-r1.json",
                HISTORICAL_PREVIEW_INSTALLATION_DIGEST,
            ),
            load_installation_recipe(generation),
        )
        if generation == "piers"
        else _reviewed_for_generation(generation)
    )
    if generation == "omega" and recipe_revision == 4 and digest == NOVNC_PHASE1_DIGEST:
        entries = (*entries, load_novnc_phase1_recipe())
    if generation in _DISPLAY_CANDIDATES:
        entries = (*entries, load_display_candidate_recipe(generation))
    for reviewed in entries:
        if (
            reviewed.recipe.recipe_id == recipe_id
            and reviewed.recipe.recipe_revision == recipe_revision
            and reviewed.digest == digest
        ):
            return reviewed
    raise ValueError("recorded runtime recipe is no longer reviewed")


_DISPLAY_CANDIDATES = {
    "omega": (
        "omega-21.3-display-r5.json",
        "sha256:505c5e7bf43e9b61e74250477c0dab5ee8fe4bd3ac8482d1b83222aff95ae8b9",
    ),
}


def load_display_candidate_recipe(generation: str) -> ReviewedRecipe:
    """Explicit reviewed packaging candidate; ordinary install defaults stay put."""
    try:
        filename, digest = _DISPLAY_CANDIDATES[generation]
    except KeyError as error:
        raise ValueError("generation has no reviewed display candidate") from error
    return _load_recipe_file(generation, filename, digest)


def load_novnc_phase1_recipe() -> ReviewedAptRecipe:
    """Explicit candidate only; ordinary Omega installation remains revision 2."""
    reviewed = _load_recipe_file(
        "omega", "omega-21.3-novnc-phase2-r4.json", NOVNC_PHASE1_DIGEST
    )
    if not isinstance(reviewed, ReviewedAptRecipe):
        raise ValueError("Phase 1 requires the APT provider")
    return reviewed


def installation_generations() -> tuple[str, ...]:
    """Closed backend selectors. Public stable/Web recipe lists stay unchanged."""
    return (*supported_generations(), "piers")


def load_installation_recipe(generation: str) -> ReviewedRecipe:
    if generation != "piers":
        return load_reviewed_recipe(generation)
    return _load_recipe_file(
        generation,
        "piers-22.0-rc1-installation-r2.json",
        PREVIEW_INSTALLATION_DIGEST,
    )


def _reviewed_for_generation(generation: str) -> tuple[ReviewedAptRecipe, ...]:
    try:
        entries = _RECIPES[generation]
    except KeyError as error:
        raise ValueError("generation has no reviewed runtime recipe") from error
    reviewed = tuple(
        _load_recipe_file(generation, filename, digest) for filename, digest in entries
    )
    identities = [
        (item.recipe.recipe_id, item.recipe.recipe_revision) for item in reviewed
    ]
    if not reviewed or len(identities) != len(set(identities)):
        raise ValueError("reviewed recipe identity is duplicated")
    if any(not isinstance(item, ReviewedAptRecipe) for item in reviewed):
        raise ValueError("stable registry contains non-APT recipe")
    return tuple(item for item in reviewed if isinstance(item, ReviewedAptRecipe))


def _load_recipe_file(
    generation: str, filename: str, expected_digest: str
) -> ReviewedRecipe:
    package = resources.files("kodi_lab.runtime")
    raw = package.joinpath("recipes", filename).read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if digest != expected_digest:
        raise ValueError("reviewed recipe resource differs from pinned digest")
    recipe = RuntimeRecipe.model_validate_json(raw)
    if recipe.generation != generation:
        raise ValueError("recipe generation differs from reviewed selection")
    asset_root = package.joinpath(
        "assembly_assets",
        recipe.recipe_id if generation == "piers" else filename.removesuffix(".json"),
    )
    if not asset_root.is_dir():
        asset_root = package.joinpath("assembly_assets")
    if generation == "piers":
        from kodi_lab.runtime.ostree_assembly import load_assembly_assets

        assets = load_assembly_assets(revision=recipe.recipe_revision)
    else:
        assets = {
            name: asset_root.joinpath(*name.split("/")).read_bytes()
            for name in recipe.asset_hashes
        }
    for name, payload in assets.items():
        if "sha256:" + hashlib.sha256(payload).hexdigest() != recipe.asset_hashes[name]:
            raise ValueError("reviewed assembly asset differs")
    if generation != "piers":
        return ReviewedAptRecipe(
            recipe=AptRuntimeRecipe.model_validate_json(raw),
            digest=digest,
            reference=f"kodi-lab-wheel:runtime/recipes/{filename}",
            assets=assets,
            raw=raw,
        )
    return ReviewedRecipe(
        recipe=recipe,
        digest=digest,
        reference=f"kodi-lab-wheel:runtime/recipes/{filename}",
        assets=assets,
        raw=raw,
    )


def parse_runtime_recipe(value: bytes) -> RuntimeRecipe | InactiveOstreeRecipe:
    """Parse recipe formats without adding inactive definitions to the registry."""

    return TypeAdapter(
        Annotated[
            RuntimeRecipe | InactiveOstreeRecipe, Field(discriminator="schema_version")
        ]
    ).validate_json(value)


@dataclass(frozen=True, slots=True)
class InactiveOstreeDefinition:
    recipe: InactiveOstreeRecipe
    digest: str


def load_inactive_ostree_recipe() -> InactiveOstreeDefinition:
    """Acquisition-only RC1 input; intentionally absent from _RECIPES."""

    raw = (
        resources.files("kodi_lab.runtime")
        .joinpath("recipes", "piers-22.0-rc1-r1.json")
        .read_bytes()
    )
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if digest != _INACTIVE_OSTREE_DIGEST:
        raise ValueError("inactive OSTree recipe differs from pinned digest")
    return InactiveOstreeDefinition(
        InactiveOstreeRecipe.model_validate_json(raw), digest
    )

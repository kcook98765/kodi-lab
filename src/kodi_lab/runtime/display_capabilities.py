"""Closed display recipes; packaging is distinct from standard qualification.

Geometry is wheel-owned, never inferred from a version or screenshot support.
Callers admitting an installed runtime must also consume its sealed validation.
No entry in this registry grants a viewer, controller or installation authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from kodi_lab.runtime.recipes import ReviewedRecipe


@dataclass(frozen=True, slots=True)
class DisplayCapability:
    recipe_id: str
    recipe_revision: int
    recipe_digest: str
    width: int
    height: int
    helper_digest: str
    package: tuple[str, str, str]
    expected_kodi_version: str
    expected_version_tag: str
    expected_prerelease_ordinal: int | None
    qualification: Literal["experimental", "candidate", "standard"]
    clipboard_policy: Literal["server-disabled", "unimplemented"] = "server-disabled"
    fps: int = 2
    absolute_seconds: int = 600
    runtime_uid: int = 1000
    runtime_gid: int = 1000
    expected_application_name: str = "Kodi"


# Historical r3 parsing remains supported; it does not gain an admission entry.
VIEWER_ASSET_RECIPES = frozenset(
    {("ubuntu-resolute-omega-21.3", revision) for revision in (3, 4, 5)}
    | {("ubuntu-noble-nexus-20.5", 3), ("ubuntu-jammy-matrix-19.4", 3)}
)
EXPERIMENTAL_OMEGA_DISPLAY = DisplayCapability(
    recipe_id="ubuntu-resolute-omega-21.3",
    recipe_revision=4,
    recipe_digest="sha256:44ff250db8587265c74ecf7114a74f51646f7f25ab4199c29c3181c02d9f2d15",
    width=640,
    height=360,
    helper_digest="sha256:be90f0c00e36bcbe4e7d905c2a7c3d0ab6645cca42fa5b5ef830e67d338775db",
    package=("tigervnc-scraping-server", "1.15.0+dfsg-2build1", "amd64"),
    expected_kodi_version="21.3",
    expected_version_tag="stable",
    expected_prerelease_ordinal=None,
    qualification="experimental",
)
OMEGA_DISPLAY_CANDIDATE = DisplayCapability(
    recipe_id="ubuntu-resolute-omega-21.3",
    recipe_revision=5,
    recipe_digest="sha256:505c5e7bf43e9b61e74250477c0dab5ee8fe4bd3ac8482d1b83222aff95ae8b9",
    width=640,
    height=360,
    helper_digest=EXPERIMENTAL_OMEGA_DISPLAY.helper_digest,
    package=EXPERIMENTAL_OMEGA_DISPLAY.package,
    expected_kodi_version="21.3",
    expected_version_tag="stable",
    expected_prerelease_ordinal=None,
    qualification="candidate",
)
NEXUS_DISPLAY_CANDIDATE = DisplayCapability(
    recipe_id="ubuntu-noble-nexus-20.5",
    recipe_revision=3,
    recipe_digest="sha256:3bc201b543498072d8217085bf667960c95a95280b9b83ad66f783bef52bed3d",
    width=640,
    height=360,
    helper_digest="sha256:1969df8a90b0e722f1b40bad9015a42d5a44debe29acc7c390b7f5fced089dcc",
    package=("tigervnc-scraping-server", "1.13.1+dfsg-2build2", "amd64"),
    expected_kodi_version="20.5",
    expected_version_tag="stable",
    expected_prerelease_ordinal=None,
    qualification="candidate",
    clipboard_policy="unimplemented",
)
MATRIX_DISPLAY_CANDIDATE = DisplayCapability(
    recipe_id="ubuntu-jammy-matrix-19.4",
    recipe_revision=3,
    recipe_digest="sha256:d3eff61619329c0aeb2be44623542a486b2b669f3aeedda6803bd54a2d9d47d1",
    width=640,
    height=360,
    helper_digest="sha256:c986d0f23698153c261a12068025977ee4e8a2685ad2cc92fec6b1d559eed4b3",
    package=("tigervnc-scraping-server", "1.12.0+dfsg-4ubuntu0.22.04.1", "amd64"),
    expected_kodi_version="19.4",
    expected_version_tag="stable",
    expected_prerelease_ordinal=None,
    qualification="candidate",
    clipboard_policy="unimplemented",
)
_DISPLAY_RECIPES = (
    EXPERIMENTAL_OMEGA_DISPLAY,
    OMEGA_DISPLAY_CANDIDATE,
    NEXUS_DISPLAY_CANDIDATE,
    MATRIX_DISPLAY_CANDIDATE,
)


def display_capability_for_digest(digest: str) -> DisplayCapability | None:
    """Recipe metadata only; a digest lookup is not installed-runtime proof."""
    return next(
        (entry for entry in _DISPLAY_RECIPES if entry.recipe_digest == digest), None
    )


def reviewed_display_capability(reviewed: ReviewedRecipe) -> DisplayCapability | None:
    from kodi_lab.runtime.recipes import load_recorded_reviewed_recipe

    capability = display_capability_for_digest(reviewed.digest)
    if capability is None or (
        reviewed.recipe.recipe_id,
        reviewed.recipe.recipe_revision,
    ) != (capability.recipe_id, capability.recipe_revision):
        return None
    # Re-read immutable wheel assets, including startup geometry and packages.
    # A substituted in-memory recipe with an unchanged digest is not reviewed.
    trusted = load_recorded_reviewed_recipe(
        reviewed.recipe.generation,
        capability.recipe_id,
        capability.recipe_revision,
        capability.recipe_digest,
    )
    if reviewed != trusted:
        return None
    helper = reviewed.assets.get("novnc_view_helper.py")
    if (
        helper is None
        or "sha256:" + hashlib.sha256(helper).hexdigest() != capability.helper_digest
    ):
        return None
    if capability.package not in {
        (package.name, package.version, package.architecture)
        for package in reviewed.recipe.critical_packages
    }:
        return None
    return capability


def installed_display_capability(
    session: Session, revision_id: UUID
) -> DisplayCapability | None:
    """Require exact recipe and current, HMAC-sealed image/validation evidence."""
    from kodi_lab.runtime.recipes import load_recorded_reviewed_recipe
    from kodi_lab.runtime.reviewed_capabilities import (
        supports_reviewed_recipe_capability,
    )
    from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

    installation = RuntimeRecipeInstallationRepository(session).ready_for_revision(
        revision_id
    )
    if (
        installation is None
        or display_capability_for_digest(installation.recipe_digest) is None
    ):
        return None
    if not supports_reviewed_recipe_capability(
        session, revision_id, "screenshot"
    ) or not supports_reviewed_recipe_capability(session, revision_id, "navigation"):
        return None
    try:
        reviewed = load_recorded_reviewed_recipe(
            installation.generation,
            installation.recipe_id,
            installation.recipe_revision,
            installation.recipe_digest,
        )
        capability = reviewed_display_capability(reviewed)
        if capability is not None and capability.qualification != "experimental":
            import json

            from kodi_lab.runtime.display_validation import display_validation_matches

            report = json.loads(installation.validation_json or "")
            if not display_validation_matches(report.get("display"), capability):
                return None
        return capability
    except ValueError:
        return None

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
    fps: int = 2
    absolute_seconds: int = 600
    runtime_uid: int = 1000
    runtime_gid: int = 1000
    expected_application_name: str = "Kodi"


# Historical r3 parsing remains supported; it does not gain an admission entry.
VIEWER_ASSET_RECIPES = frozenset(
    ("ubuntu-resolute-omega-21.3", revision) for revision in (3, 4)
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
_DISPLAY_RECIPES = (EXPERIMENTAL_OMEGA_DISPLAY,)


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
        return reviewed_display_capability(reviewed)
    except ValueError:
        return None

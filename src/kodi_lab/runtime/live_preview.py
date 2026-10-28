"""Reviewed live-preview prerequisites, distinct from screenshot capability."""

from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.runtime.recipes import NOVNC_PHASE1_DIGEST
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

# This closed review admits only these exact recipes as candidates for an
# explicit administrator-triggered native JPEG probe. Every actual revision,
# container, session and runtime-state token is separately bound by the grant.
# Screenshot support alone never enables streaming. Other recipes fail closed.
LIVE_PREVIEW_RECIPES = {
    ("ubuntu-resolute-omega-21.3", 4): NOVNC_PHASE1_DIGEST,
    (
        "ubuntu-resolute-omega-21.3",
        2,
    ): "sha256:4cb4a003da6d119018738db06aed6789c0acaee2b5ed89b210b93db5526d7140",
    (
        "ubuntu-noble-nexus-20.5",
        2,
    ): "sha256:9845a930212f91c20e1313f03cc289882b8555f6067a5bf22e11e724b9821631",
}


def reviewed_live_preview_prerequisites(session: Session, revision_id: UUID) -> bool:
    installation = RuntimeRecipeInstallationRepository(session).ready_for_revision(
        revision_id
    )
    if (
        installation is None
        or LIVE_PREVIEW_RECIPES.get(
            (installation.recipe_id, installation.recipe_revision)
        )
        != installation.recipe_digest
    ):
        return False
    # These are prerequisites of the separately reviewed live recipe and its
    # native-frame authorization, not a substitute live capability grant.
    return supports_reviewed_recipe_capability(
        session, revision_id, "screenshot"
    ) and supports_reviewed_recipe_capability(session, revision_id, "navigation")

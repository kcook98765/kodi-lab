"""Pure canonical runtime admission shared by management and development."""

from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.domain import InstanceType
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

# Issue #51 admits this already sealed installation, independently of the
# optional human-viewer setting. New images/revisions require separate review.
APPROVED_OMEGA_VIEW_REVISION = UUID("9ee490cd-8eee-424b-b3b2-06c2757ccaa8")
APPROVED_OMEGA_VIEW_IMAGE = (
    "sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b"
)


def development_target_ids() -> frozenset[UUID]:
    from kodi_lab.runtime.lab import KNOWN_SELECTORS, canonical_id
    from kodi_lab.runtime.recipes import (
        load_display_candidate_recipe,
        load_novnc_phase1_recipe,
        recipe_target_id,
    )

    return frozenset(
        {canonical_id(selector) for selector in KNOWN_SELECTORS}
        | {
            recipe_target_id(load_novnc_phase1_recipe()),
            recipe_target_id(load_display_candidate_recipe("piers")),
        }
    )


def require_managed_runtime(
    session: Session, target_id: UUID, revision_id: UUID, *, data_root: Path
) -> None:
    from kodi_lab.domain import TargetType
    from kodi_lab.runtime.recipes import (
        PIERS_DISPLAY_INSTALLATION_DIGEST,
        load_novnc_phase1_recipe,
        load_recorded_reviewed_recipe,
        recipe_target_id,
    )
    from kodi_lab.storage.retained_development import retained_targets

    retained = str(target_id) in retained_targets(session)
    if target_id not in development_target_ids() and not retained:
        raise DevelopmentSessionConflict("development_session_noncanonical_target")
    target = TargetRepository(session).get(target_id)
    revision = RuntimeRevisionRepository(session).get(revision_id)
    definition = (
        None
        if revision is None
        else RuntimeDefinitionRepository(session).get(revision.runtime_definition_id)
    )
    if target is None or revision is None or definition is None:
        raise DevelopmentSessionConflict("development_session_runtime_unavailable")
    reviewed = load_novnc_phase1_recipe()
    qualified_omega = target_id == recipe_target_id(reviewed)
    if qualified_omega:
        instance = ManagedInstanceRepository(session).get(target_id)
        if (
            instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or instance.runtime_revision_id != revision_id
            or revision_id != APPROVED_OMEGA_VIEW_REVISION
            or target.target_type is not TargetType.MANAGED
            or target.name
            != f"{reviewed.recipe.display_generation} Lab · experimental noVNC"
            or target.kodi_version != reviewed.recipe.kodi_version
            or definition.id != reviewed.recipe.definition_id
            or revision.manifest_reference != reviewed.reference
            or revision.manifest_digest != reviewed.digest
            or revision.content_digest != APPROVED_OMEGA_VIEW_IMAGE
        ):
            raise DevelopmentSessionConflict("development_session_runtime_ineligible")
    preview_digest = (
        revision.manifest_digest
        if retained and target.kodi_version in {"22.0-rc.1", "22.0-rc.2"}
        else None
    )
    if revision.manifest_digest == PIERS_DISPLAY_INSTALLATION_DIGEST:
        candidate = load_recorded_reviewed_recipe(
            "piers", "piers-22.0-rc1-r3", 3, PIERS_DISPLAY_INSTALLATION_DIGEST
        )
        if (
            target_id == recipe_target_id(candidate)
            and definition.id == candidate.recipe.definition_id
            and revision.manifest_reference == candidate.reference
        ):
            # Selection uses the same exact sealed Preview approval as its
            # installer. It does not grant viewing or control qualification.
            preview_digest = candidate.digest
    eligibility = RuntimeRevisionEligibilityPolicy(data_root=data_root).evaluate(
        session,
        target=target,
        definition=definition,
        revision=revision,
        policy=RuntimeSelectionPolicy(
            include_legacy=True,
            novnc_phase1=qualified_omega,
            include_prerelease=preview_digest is not None,
            preview_recipe_digest=preview_digest,
        ),
    )
    if not eligibility.eligible:
        raise DevelopmentSessionConflict("development_session_runtime_ineligible")

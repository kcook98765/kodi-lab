"""One shared policy for selecting new managed-instance runtime pins."""

import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    KodiReleaseIdentity,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationResult,
    Target,
)
from kodi_lab.runtime.recipes import (
    REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
    load_recorded_reviewed_recipe,
)
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeValidationEvidenceRepository,
)


class RuntimeIneligibilityReason(StrEnum):
    INCOMPATIBLE_RELEASE = "incompatible_release"
    SUPPORT_TIER_NOT_PERMITTED = "support_tier_not_permitted"
    REVISION_NOT_APPROVED = "revision_not_approved"
    TRUSTED_CUSTOM_BUILDS_DISABLED = "trusted_custom_builds_disabled"
    TRUSTED_BUILD_CONFIRMATION_REQUIRED = "trusted_build_confirmation_required"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    EXECUTION_PROFILE_UNAVAILABLE = "execution_profile_unavailable"
    EXECUTION_PROFILE_INCOMPATIBLE = "execution_profile_incompatible"
    UNVALIDATED = "unvalidated"
    VALIDATION_FAILED = "validation_failed"
    RECIPE_PROVENANCE_UNAVAILABLE = "recipe_provenance_unavailable"


@dataclass(frozen=True, slots=True)
class RuntimeSelectionPolicy:
    include_legacy: bool = False
    include_prerelease: bool = False
    trusted_custom_builds_enabled: bool = False
    validation_contract: str = KODI_RUNTIME_VALIDATION_CONTRACT

    def permits(self, tier: RuntimeSupportTier) -> bool:
        if tier is RuntimeSupportTier.CURRENT:
            return True
        if tier is RuntimeSupportTier.LEGACY:
            return self.include_legacy
        if tier is RuntimeSupportTier.PREVIEW:
            return self.include_prerelease
        return False


@dataclass(frozen=True, slots=True)
class RuntimeEligibility:
    eligible: bool
    reason: RuntimeIneligibilityReason | None = None


class RuntimeRevisionEligibilityPolicy:
    """Evaluate every trusted condition for creating or changing a pin."""

    def evaluate(
        self,
        session: Session,
        *,
        target: Target,
        definition: RuntimeDefinition,
        revision: RuntimeRevision,
        policy: RuntimeSelectionPolicy,
    ) -> RuntimeEligibility:
        return self.evaluate_for_release(
            session,
            required_release=target.kodi_release,
            definition=definition,
            revision=revision,
            policy=policy,
        )

    def evaluate_for_release(
        self,
        session: Session,
        *,
        required_release: KodiReleaseIdentity,
        definition: RuntimeDefinition,
        revision: RuntimeRevision,
        policy: RuntimeSelectionPolicy,
    ) -> RuntimeEligibility:
        """Evaluate a new pin against one trusted exact release requirement."""

        if required_release != definition.kodi_release:
            return self._no(RuntimeIneligibilityReason.INCOMPATIBLE_RELEASE)
        if not policy.permits(definition.support_tier):
            return self._no(RuntimeIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED)
        if revision.approval_state is not RevisionApprovalState.APPROVED:
            return self._no(RuntimeIneligibilityReason.REVISION_NOT_APPROVED)
        if revision.requires_trusted_build_confirmation:
            if not policy.trusted_custom_builds_enabled:
                return self._no(
                    RuntimeIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED
                )
            if revision.trusted_build_confirmed_at is None:
                return self._no(
                    RuntimeIneligibilityReason.TRUSTED_BUILD_CONFIRMATION_REQUIRED
                )
        artifact = RuntimeContainerArtifactRepository(session).get(revision.id)
        if artifact is None:
            return self._no(RuntimeIneligibilityReason.ARTIFACT_UNAVAILABLE)
        profile = RuntimeExecutionProfileRepository(session).get(revision.id)
        if profile is None:
            return self._no(RuntimeIneligibilityReason.EXECUTION_PROFILE_UNAVAILABLE)
        if profile.kodi_release != definition.kodi_release:
            return self._no(RuntimeIneligibilityReason.EXECUTION_PROFILE_INCOMPATIBLE)
        if revision.source_kind == REVIEWED_SIGNED_DISTRO_SOURCE_KIND:
            installation = RuntimeRecipeInstallationRepository(
                session
            ).ready_for_revision(revision.id)
            if installation is None:
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
            try:
                reviewed = load_recorded_reviewed_recipe(
                    installation.generation,
                    installation.recipe_id,
                    installation.recipe_revision,
                    installation.recipe_digest,
                )
                attestation = json.loads(installation.attestation_json or "")
                validation = json.loads(installation.validation_json or "")
                expected_profile = RuntimeExecutionProfile(
                    runtime_revision_id=revision.id,
                    **reviewed.recipe.execution_profile.model_dump(),
                )
            except (ValueError, OSError):
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
            if (
                installation.recipe_id != reviewed.recipe.recipe_id
                or installation.recipe_revision != reviewed.recipe.recipe_revision
                or installation.recipe_digest != reviewed.digest
                or installation.image_id != artifact.image_id
                or revision.manifest_reference != reviewed.reference
                or revision.manifest_digest != reviewed.digest
                or revision.content_digest != artifact.image_id
                or definition.id != reviewed.recipe.definition_id
                or definition.kodi_version != reviewed.recipe.kodi_version
                or profile != expected_profile
                or not isinstance(attestation, dict)
                or attestation.get("acquisition_result") != "passed"
                or attestation.get("recipe_digest") != reviewed.digest
                or attestation.get("image_id") != artifact.image_id
                or not isinstance(validation, dict)
                or validation.get("result") != "passed"
                or validation.get("image_id") != artifact.image_id
                or validation.get("recipe_digest") != reviewed.digest
            ):
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
        evidence = RuntimeValidationEvidenceRepository(session).latest_for_contract(
            revision.id, policy.validation_contract
        )
        if evidence is None:
            return self._no(RuntimeIneligibilityReason.UNVALIDATED)
        if evidence.result is RuntimeValidationResult.FAILED:
            return self._no(RuntimeIneligibilityReason.VALIDATION_FAILED)
        return RuntimeEligibility(eligible=True)

    @staticmethod
    def _no(reason: RuntimeIneligibilityReason) -> RuntimeEligibility:
        return RuntimeEligibility(eligible=False, reason=reason)

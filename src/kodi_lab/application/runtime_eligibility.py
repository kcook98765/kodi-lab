"""One shared policy for selecting new managed-instance runtime pins."""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    KodiReleaseIdentity,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationResult,
    Target,
)
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeExecutionProfileRepository,
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

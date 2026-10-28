"""One shared policy for selecting new managed-instance runtime pins."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.config import Settings
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
from kodi_lab.runtime.attestation_integrity import (
    installed_package_digest,
    verify_attestation,
)
from kodi_lab.runtime.recipes import (
    NOVNC_PHASE1_DIGEST,
    REVIEWED_OSTREE_SOURCE_KIND,
    REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
    FlathubOstreeProvider,
    ReviewedRecipe,
    SignedDistroAptProvider,
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
    preview_recipe_digest: str | None = None
    trusted_custom_builds_enabled: bool = False
    validation_contract: str = KODI_RUNTIME_VALIDATION_CONTRACT
    novnc_phase1: bool = False

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


def _reviewed_attestation_matches(
    attestation: object, reviewed: ReviewedRecipe, image_id: str
) -> bool:
    if not isinstance(attestation, dict):
        return False
    recipe = reviewed.recipe
    if isinstance(recipe.provider, FlathubOstreeProvider):
        from kodi_lab.runtime.ostree_installation import ostree_attestation_matches

        return ostree_attestation_matches(attestation, reviewed, image_id)
    if not isinstance(recipe.provider, SignedDistroAptProvider):
        return False
    expected = {
        "schema": "kodi-binary-recipe-acquisition-v1",
        "recipe_id": recipe.recipe_id,
        "recipe_revision": recipe.recipe_revision,
        "recipe_digest": reviewed.digest,
        "generation": recipe.generation,
        "kodi_version": recipe.kodi_version,
        "platform": recipe.platform,
        "base_image_reference": recipe.base_image_reference,
        "provider": recipe.provider.model_dump(mode="json"),
        "critical_packages": [
            item.model_dump(mode="json") for item in recipe.critical_packages
        ],
        "image_id": image_id,
        "validation_contract": recipe.validation_contract,
        "acquisition_result": "passed",
    }
    if any(attestation.get(key) != value for key, value in expected.items()):
        return False
    packages = attestation.get("installed_packages")
    if (
        not isinstance(packages, list)
        or not packages
        or any(
            not isinstance(row, list)
            or len(row) != 3
            or not all(isinstance(item, str) and item for item in row)
            for row in packages
        )
    ):
        return False
    installed = {row[0]: row[1:] for row in packages}
    if len(installed) != len(packages) or any(
        installed.get(item.name) != [item.version, item.architecture]
        for item in recipe.critical_packages
    ):
        return False
    if attestation.get("installed_packages_digest") != installed_package_digest(
        packages
    ):
        return False
    metadata = attestation.get("signed_metadata_digests")
    return (
        isinstance(metadata, list)
        and len(metadata) == len(recipe.provider.pockets)
        and all(
            isinstance(row, list)
            and len(row) == 2
            and isinstance(row[0], str)
            and isinstance(row[1], str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", row[1]) is not None
            for row in metadata
        )
        and all(
            sum(row[0].endswith(f"_dists_{pocket}_InRelease") for row in metadata) == 1
            for pocket in recipe.provider.pockets
        )
    )


def _reviewed_validation_matches(
    validation: object, reviewed: ReviewedRecipe, image_id: str
) -> bool:
    if not isinstance(validation, dict):
        return False
    if isinstance(reviewed.recipe.provider, FlathubOstreeProvider):
        from kodi_lab.runtime.ostree_validation import validation_matches

        if not validation_matches(validation):
            return False
    screenshot = validation.get("screenshot")
    cycles = validation.get("cycles")
    return (
        validation.get("schema") == reviewed.recipe.validation_contract
        and validation.get("result") == "passed"
        and validation.get("recipe_digest") == reviewed.digest
        and validation.get("release") == reviewed.recipe.kodi_version
        and validation.get("image_id") == image_id
        and validation.get("network_mode") == "isolated_internal_bridge"
        and validation.get("non_root_uid") == 1000
        and validation.get("jsonrpc") is True
        and validation.get("navigation") is True
        and validation.get("library_query") is True
        and isinstance(screenshot, dict)
        and screenshot.get("width") == 1280
        and screenshot.get("height") == 720
        and screenshot.get("software_renderer") == "llvmpipe"
        and isinstance(screenshot.get("png_bytes"), int)
        and screenshot["png_bytes"] > 0
        and isinstance(screenshot.get("png_sha256"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", screenshot["png_sha256"]) is not None
        and isinstance(cycles, list)
        and len(cycles) == 2
        and all(
            isinstance(cycle, dict)
            and cycle.get("cycle") == index
            and cycle.get("readiness") is True
            and cycle.get("clean_stop") is True
            for index, cycle in enumerate(cycles, 1)
        )
    )


class RuntimeRevisionEligibilityPolicy:
    """Evaluate every trusted condition for creating or changing a pin."""

    def __init__(self, *, data_root: Path | None = None) -> None:
        self._data_root = data_root or Settings().data_root

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

        if required_release.canonical == "22.0-rc.1" and (
            revision.source_kind != REVIEWED_OSTREE_SOURCE_KIND
            or definition.support_tier is not RuntimeSupportTier.PREVIEW
        ):
            return self._no(RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE)
        if required_release != definition.kodi_release:
            return self._no(RuntimeIneligibilityReason.INCOMPATIBLE_RELEASE)
        if (
            revision.manifest_digest
            in {
                NOVNC_PHASE1_DIGEST,
                "sha256:a81b7786b6df3c4cf0fde57b117ee87b5911eb8df1f17c74e2cee45685337a46",
            }
            and not policy.novnc_phase1
        ):
            return self._no(RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE)
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
        if revision.source_kind in (
            REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
            REVIEWED_OSTREE_SOURCE_KIND,
        ):
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
                (
                    revision.source_kind == REVIEWED_OSTREE_SOURCE_KIND
                    and (
                        not isinstance(reviewed.recipe.provider, FlathubOstreeProvider)
                        or policy.preview_recipe_digest != reviewed.digest
                        or attestation.get("operation_id") != UUID(installation.id).hex
                    )
                )
                or installation.recipe_id != reviewed.recipe.recipe_id
                or installation.recipe_revision != reviewed.recipe.recipe_revision
                or installation.recipe_digest != reviewed.digest
                or installation.image_id != artifact.image_id
                or revision.manifest_reference != reviewed.reference
                or revision.manifest_digest != reviewed.digest
                or revision.content_digest != artifact.image_id
                or definition.id != reviewed.recipe.definition_id
                or definition.kodi_version != reviewed.recipe.kodi_version
                or profile != expected_profile
                or not _reviewed_attestation_matches(
                    attestation, reviewed, artifact.image_id
                )
                or not _reviewed_validation_matches(
                    validation, reviewed, artifact.image_id
                )
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
        if revision.source_kind in (
            REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
            REVIEWED_OSTREE_SOURCE_KIND,
        ):
            assert installation is not None
            if installation.completed_at is None:
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
            try:
                created = datetime.fromisoformat(installation.created_at)
                completed = datetime.fromisoformat(installation.completed_at)
                operation_id = UUID(installation.id)
            except (TypeError, ValueError):
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
            expected_reference = (
                f"kodi-lab-db:runtime-recipe-installation/{installation.id}#validation"
            )
            if (
                created.tzinfo is None
                or completed.tzinfo is None
                or policy.validation_contract != reviewed.recipe.validation_contract
                or evidence.result is not RuntimeValidationResult.PASSED
                or evidence.evidence_reference != expected_reference
                or evidence.started_at != revision.built_at
                or evidence.recorded_at != evidence.completed_at
                or not created
                <= evidence.started_at
                <= evidence.completed_at
                <= completed
                or not verify_attestation(
                    installation.attestation_json or "",
                    installation.validation_json or "",
                    data_root=self._data_root,
                    operation_id=operation_id,
                    revision_id=revision.id,
                    image_id=artifact.image_id,
                    evidence=evidence,
                )
            ):
                return self._no(
                    RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
                )
        return RuntimeEligibility(eligible=True)

    @staticmethod
    def _no(reason: RuntimeIneligibilityReason) -> RuntimeEligibility:
        return RuntimeEligibility(eligible=False, reason=reason)

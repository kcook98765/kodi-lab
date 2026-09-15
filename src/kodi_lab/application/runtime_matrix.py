"""Bounded semantic presentation and Target-scoped runtime resolution."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import TargetNotFoundError
from kodi_lab.application.runtime_eligibility import (
    RuntimeIneligibilityReason,
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.domain import (
    EffectiveValidationState,
    KodiReleaseChannel,
    KodiReleaseIdentity,
    RuntimeDefinition,
    RuntimeRevision,
    RuntimeSupportTier,
    effective_validation_state,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)


class MatrixAvailability(StrEnum):
    """Administrator-facing availability classification."""

    SELECTABLE = "selectable"
    OPT_IN_REQUIRED = "opt_in_required"
    UNAVAILABLE = "unavailable"
    HISTORICAL_ONLY = "historical_only"


class MatrixIneligibilityReason(StrEnum):
    """Closed presentation reason set; never contains exception text."""

    NO_REVISION_AVAILABLE = "no_revision_available"
    RUNTIME_DEFINITION_UNAVAILABLE = "runtime_definition_unavailable"
    AMBIGUOUS_RUNTIME_DEFINITION = "ambiguous_runtime_definition"
    INCOMPATIBLE_RELEASE = RuntimeIneligibilityReason.INCOMPATIBLE_RELEASE
    SUPPORT_TIER_NOT_PERMITTED = RuntimeIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED
    REVISION_NOT_APPROVED = RuntimeIneligibilityReason.REVISION_NOT_APPROVED
    TRUSTED_CUSTOM_BUILDS_DISABLED = (
        RuntimeIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED
    )
    TRUSTED_BUILD_CONFIRMATION_REQUIRED = (
        RuntimeIneligibilityReason.TRUSTED_BUILD_CONFIRMATION_REQUIRED
    )
    ARTIFACT_UNAVAILABLE = RuntimeIneligibilityReason.ARTIFACT_UNAVAILABLE
    EXECUTION_PROFILE_UNAVAILABLE = (
        RuntimeIneligibilityReason.EXECUTION_PROFILE_UNAVAILABLE
    )
    EXECUTION_PROFILE_INCOMPATIBLE = (
        RuntimeIneligibilityReason.EXECUTION_PROFILE_INCOMPATIBLE
    )
    UNVALIDATED = RuntimeIneligibilityReason.UNVALIDATED
    VALIDATION_FAILED = RuntimeIneligibilityReason.VALIDATION_FAILED


class RuntimeMatrixEntry(BaseModel):
    """Semantic catalog projection without artifact or revision authority."""

    model_config = ConfigDict(extra="forbid")

    runtime_definition_id: UUID
    canonical_release: str = Field(max_length=32)
    display_version: str = Field(max_length=64)
    release_channel: KodiReleaseChannel
    prerelease_ordinal: int | None
    support_tier: RuntimeSupportTier
    newest_revision_number: int | None
    selected_revision_number: int | None
    effective_validation_state: EffectiveValidationState
    selectable: bool
    availability: MatrixAvailability
    ineligibility_reason: MatrixIneligibilityReason | None
    availability_detail: str = Field(max_length=256)
    requires_legacy_opt_in: bool
    requires_prerelease_opt_in: bool
    requires_custom_build_trust: bool
    pinned_instance_count: int = Field(ge=0)


class RuntimeMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_legacy: bool
    include_prerelease: bool
    trusted_custom_builds_enabled: bool
    runtimes: list[RuntimeMatrixEntry]


class TargetRuntimeSelection(BaseModel):
    """Read-only resolution result for one trusted Target identity."""

    model_config = ConfigDict(extra="forbid")

    target_id: UUID
    target_name: str = Field(max_length=256)
    required_canonical_release: str = Field(max_length=32)
    required_display_version: str = Field(max_length=64)
    runtime_definition_id: UUID | None
    selected_revision_number: int | None
    selectable: bool
    ineligibility_reason: MatrixIneligibilityReason | None
    availability_detail: str = Field(max_length=256)


class RuntimeMatrixService:
    """Compose catalog presentation through the shared new-pin policy."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        trusted_custom_builds_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._trusted_custom_builds_enabled = trusted_custom_builds_enabled
        self._eligibility = RuntimeRevisionEligibilityPolicy()

    def list_matrix(
        self, *, include_legacy: bool = False, include_prerelease: bool = False
    ) -> RuntimeMatrixResponse:
        policy = self._policy(include_legacy, include_prerelease)
        with self._session_factory() as session:
            definitions = RuntimeDefinitionRepository(session).list()
            pin_counts = self._pin_counts(session)
            entries = [
                self._entry(session, definition, policy, pin_counts[definition.id])
                for definition in definitions
            ]
        entries.sort(key=self._entry_sort_key)
        return RuntimeMatrixResponse(
            include_legacy=include_legacy,
            include_prerelease=include_prerelease,
            trusted_custom_builds_enabled=self._trusted_custom_builds_enabled,
            runtimes=entries,
        )

    def resolve_target(
        self,
        target_id: UUID,
        *,
        include_legacy: bool = False,
        include_prerelease: bool = False,
    ) -> TargetRuntimeSelection:
        policy = self._policy(include_legacy, include_prerelease)
        with self._session_factory() as session:
            target = TargetRepository(session).get(target_id)
            if target is None:
                raise TargetNotFoundError(target_id)
            matches = [
                item
                for item in RuntimeDefinitionRepository(session).list()
                if item.kodi_release == target.kodi_release
            ]
            if not matches:
                return TargetRuntimeSelection(
                    target_id=target.id,
                    target_name=target.name,
                    required_canonical_release=target.kodi_release.canonical,
                    required_display_version=self._display(target.kodi_release),
                    runtime_definition_id=None,
                    selected_revision_number=None,
                    selectable=False,
                    ineligibility_reason=(
                        MatrixIneligibilityReason.RUNTIME_DEFINITION_UNAVAILABLE
                    ),
                    availability_detail="No curated runtime matches this Target.",
                )
            if len(matches) > 1:
                return TargetRuntimeSelection(
                    target_id=target.id,
                    target_name=target.name,
                    required_canonical_release=target.kodi_release.canonical,
                    required_display_version=self._display(target.kodi_release),
                    runtime_definition_id=None,
                    selected_revision_number=None,
                    selectable=False,
                    ineligibility_reason=(
                        MatrixIneligibilityReason.AMBIGUOUS_RUNTIME_DEFINITION
                    ),
                    availability_detail=(
                        "The curated catalog has conflicting exact runtimes."
                    ),
                )
            definition = matches[0]
            entry = self._entry(
                session,
                definition,
                policy,
                self._pin_counts(session)[definition.id],
            )
            return TargetRuntimeSelection(
                target_id=target.id,
                target_name=target.name,
                required_canonical_release=target.kodi_release.canonical,
                required_display_version=self._display(target.kodi_release),
                runtime_definition_id=definition.id,
                selected_revision_number=entry.selected_revision_number,
                selectable=entry.selectable,
                ineligibility_reason=entry.ineligibility_reason,
                availability_detail=entry.availability_detail,
            )

    def _entry(
        self,
        session: Session,
        definition: RuntimeDefinition,
        policy: RuntimeSelectionPolicy,
        pinned_instance_count: int,
    ) -> RuntimeMatrixEntry:
        revisions = RuntimeRevisionRepository(session).list_for_definition_descending(
            definition.id
        )
        selected: RuntimeRevision | None = None
        selected_result = None
        for revision in revisions:
            result = self._eligibility.evaluate_for_release(
                session,
                required_release=definition.kodi_release,
                definition=definition,
                revision=revision,
                policy=policy,
            )
            if result.eligible:
                selected = revision
                selected_result = result
                break
        candidate = selected or (revisions[0] if revisions else None)
        if candidate is None:
            reason = MatrixIneligibilityReason.NO_REVISION_AVAILABLE
        elif selected_result is not None:
            reason = None
        else:
            evaluated = self._eligibility.evaluate_for_release(
                session,
                required_release=definition.kodi_release,
                definition=definition,
                revision=candidate,
                policy=policy,
            )
            assert evaluated.reason is not None
            reason = MatrixIneligibilityReason(evaluated.reason.value)
        evidence = (
            None
            if candidate is None
            else RuntimeValidationEvidenceRepository(session).latest_for_contract(
                candidate.id, policy.validation_contract
            )
        )
        availability = self._availability(definition.support_tier, reason)
        return RuntimeMatrixEntry(
            runtime_definition_id=definition.id,
            canonical_release=definition.kodi_release.canonical,
            display_version=self._display(definition.kodi_release),
            release_channel=definition.kodi_release.channel,
            prerelease_ordinal=definition.kodi_release.prerelease_ordinal,
            support_tier=definition.support_tier,
            newest_revision_number=(
                None if not revisions else revisions[0].revision_number
            ),
            selected_revision_number=(
                None if selected is None else selected.revision_number
            ),
            effective_validation_state=effective_validation_state(evidence),
            selectable=selected is not None,
            availability=availability,
            ineligibility_reason=reason,
            availability_detail=self._detail(definition.support_tier, reason),
            requires_legacy_opt_in=definition.support_tier is RuntimeSupportTier.LEGACY,
            requires_prerelease_opt_in=definition.support_tier
            is RuntimeSupportTier.PREVIEW,
            requires_custom_build_trust=(
                False
                if candidate is None
                else candidate.requires_trusted_build_confirmation
            ),
            pinned_instance_count=pinned_instance_count,
        )

    def _policy(
        self, include_legacy: bool, include_prerelease: bool
    ) -> RuntimeSelectionPolicy:
        return RuntimeSelectionPolicy(
            include_legacy=include_legacy,
            include_prerelease=include_prerelease,
            trusted_custom_builds_enabled=self._trusted_custom_builds_enabled,
        )

    @staticmethod
    def _pin_counts(session: Session) -> Counter[UUID]:
        revisions = RuntimeRevisionRepository(session)
        counts: Counter[UUID] = Counter()
        for instance in ManagedInstanceRepository(session).list():
            revision = revisions.get(instance.runtime_revision_id)
            if revision is not None:
                counts[revision.runtime_definition_id] += 1
        return counts

    @staticmethod
    def _display(release: KodiReleaseIdentity) -> str:
        channel = release.channel
        if channel is KodiReleaseChannel.STABLE:
            return f"Kodi {release.major}.{release.minor}"
        label = "RC" if channel is KodiReleaseChannel.RC else channel.value.title()
        return (
            f"Kodi {release.major}.{release.minor} {label} {release.prerelease_ordinal}"
        )

    @staticmethod
    def _availability(
        tier: RuntimeSupportTier, reason: MatrixIneligibilityReason | None
    ) -> MatrixAvailability:
        if tier is RuntimeSupportTier.RETIRED:
            return MatrixAvailability.HISTORICAL_ONLY
        if reason is None:
            return MatrixAvailability.SELECTABLE
        if reason is MatrixIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED and tier in {
            RuntimeSupportTier.LEGACY,
            RuntimeSupportTier.PREVIEW,
        }:
            return MatrixAvailability.OPT_IN_REQUIRED
        return MatrixAvailability.UNAVAILABLE

    @staticmethod
    def _detail(
        tier: RuntimeSupportTier, reason: MatrixIneligibilityReason | None
    ) -> str:
        if reason is None:
            return "Available for a compatible Target."
        if reason is MatrixIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED:
            if tier is RuntimeSupportTier.LEGACY:
                return "Legacy runtime; enable Include Legacy."
            if tier is RuntimeSupportTier.PREVIEW:
                return "Preview runtime; enable Include Pre-release."
            return "Runtime is retired and historical only."
        details = {
            MatrixIneligibilityReason.NO_REVISION_AVAILABLE: (
                "No runtime revision is registered."
            ),
            MatrixIneligibilityReason.INCOMPATIBLE_RELEASE: (
                "Runtime is incompatible with this Target."
            ),
            MatrixIneligibilityReason.REVISION_NOT_APPROVED: (
                "No approved eligible revision is available."
            ),
            MatrixIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED: (
                "Trusted custom builds are disabled."
            ),
            MatrixIneligibilityReason.TRUSTED_BUILD_CONFIRMATION_REQUIRED: (
                "Trusted-build confirmation is required."
            ),
            MatrixIneligibilityReason.ARTIFACT_UNAVAILABLE: (
                "The immutable runtime artifact is unavailable."
            ),
            MatrixIneligibilityReason.EXECUTION_PROFILE_UNAVAILABLE: (
                "The runtime execution profile is unavailable."
            ),
            MatrixIneligibilityReason.EXECUTION_PROFILE_INCOMPATIBLE: (
                "The runtime execution profile is incompatible."
            ),
            MatrixIneligibilityReason.UNVALIDATED: (
                "Runtime has not passed validation."
            ),
            MatrixIneligibilityReason.VALIDATION_FAILED: (
                "Latest runtime validation failed."
            ),
        }
        return details.get(reason, "Runtime is unavailable.")

    @staticmethod
    def _entry_sort_key(entry: RuntimeMatrixEntry) -> tuple[int, int, int, int]:
        channel_order = {
            KodiReleaseChannel.ALPHA: 0,
            KodiReleaseChannel.BETA: 1,
            KodiReleaseChannel.RC: 2,
            KodiReleaseChannel.STABLE: 3,
        }
        major, minor = (
            int(part) for part in entry.canonical_release.split("-")[0].split(".")
        )
        return (
            major,
            minor,
            channel_order[entry.release_channel],
            entry.prerelease_ordinal or 0,
        )

"""Typed application-service failures independent of transport and storage."""

from uuid import UUID


class ApplicationServiceError(Exception):
    """Base class for expected application-operation failures."""


class EntityNotFoundError(ApplicationServiceError):
    """Base class for missing application entities."""


class TargetNotFoundError(EntityNotFoundError):
    def __init__(self, target_id: UUID) -> None:
        self.target_id = target_id
        super().__init__(f"target {target_id} does not exist")


class RuntimeDefinitionNotFoundError(EntityNotFoundError):
    def __init__(self, runtime_definition_id: UUID) -> None:
        self.runtime_definition_id = runtime_definition_id
        super().__init__(f"runtime definition {runtime_definition_id} does not exist")


class RuntimeRevisionNotFoundError(EntityNotFoundError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(f"runtime revision {runtime_revision_id} does not exist")


class ManagedInstanceNotFoundError(EntityNotFoundError):
    def __init__(self, target_id: UUID) -> None:
        self.target_id = target_id
        super().__init__(f"managed instance for target {target_id} does not exist")


class FixtureDefinitionNotFoundError(EntityNotFoundError):
    def __init__(self, fixture_definition_id: UUID) -> None:
        self.fixture_definition_id = fixture_definition_id
        super().__init__(f"fixture definition {fixture_definition_id} does not exist")


class RuntimeSelectionError(ApplicationServiceError):
    """Base class for revisions that cannot be selected under current policy."""


class RuntimeRevisionDefinitionMismatchError(RuntimeSelectionError):
    def __init__(
        self,
        *,
        runtime_revision_id: UUID,
        expected_runtime_definition_id: UUID,
        actual_runtime_definition_id: UUID,
    ) -> None:
        self.runtime_revision_id = runtime_revision_id
        self.expected_runtime_definition_id = expected_runtime_definition_id
        self.actual_runtime_definition_id = actual_runtime_definition_id
        super().__init__(
            f"runtime revision {runtime_revision_id} belongs to runtime definition "
            f"{actual_runtime_definition_id}, not {expected_runtime_definition_id}"
        )


class NoApprovedRevisionError(RuntimeSelectionError):
    def __init__(self, runtime_definition_id: UUID) -> None:
        self.runtime_definition_id = runtime_definition_id
        super().__init__(
            "runtime definition "
            f"{runtime_definition_id} has no approved runtime revision"
        )


class NoEligibleRevisionError(NoApprovedRevisionError):
    def __init__(self, runtime_definition_id: UUID) -> None:
        self.runtime_definition_id = runtime_definition_id
        RuntimeSelectionError.__init__(
            self,
            "runtime definition "
            f"{runtime_definition_id} has no eligible runtime revision",
        )


class RuntimeRevisionIneligibleError(RuntimeSelectionError):
    def __init__(self, runtime_revision_id: UUID, reason: str) -> None:
        self.runtime_revision_id = runtime_revision_id
        self.reason = reason
        super().__init__(
            f"runtime revision {runtime_revision_id} is ineligible: {reason}"
        )


class RevisionNotApprovedError(RuntimeSelectionError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(f"runtime revision {runtime_revision_id} is not approved")


class TrustedCustomBuildDisabledError(RuntimeSelectionError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(
            "Trusted Custom Build global opt-in is disabled for runtime revision "
            f"{runtime_revision_id}"
        )


class TrustedCustomBuildConfirmationRequiredError(RuntimeSelectionError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(
            "Trusted Custom Build confirmation is required for runtime revision "
            f"{runtime_revision_id}"
        )


class KodiVersionIncompatibleError(RuntimeSelectionError):
    def __init__(
        self,
        *,
        target_id: UUID,
        runtime_definition_id: UUID,
        target_kodi_version: str,
        runtime_kodi_version: str,
    ) -> None:
        self.target_id = target_id
        self.runtime_definition_id = runtime_definition_id
        self.target_kodi_version = target_kodi_version
        self.runtime_kodi_version = runtime_kodi_version
        super().__init__(
            f"target {target_id} Kodi version {target_kodi_version!r} is incompatible "
            f"with runtime definition {runtime_definition_id} Kodi version "
            f"{runtime_kodi_version!r}"
        )


class PersistenceConflictError(ApplicationServiceError):
    """A storage invariant prevented an application operation."""


class ManagedInstanceConflictError(PersistenceConflictError):
    def __init__(self, target_id: UUID) -> None:
        self.target_id = target_id
        super().__init__(f"managed instance for target {target_id} already exists")


class ManagedInstanceDataValidationError(ApplicationServiceError):
    """Managed-instance user metadata failed bounded domain validation."""


class FixtureDefinitionConflictError(PersistenceConflictError):
    def __init__(self, *, fixture_definition_id: UUID, slug: str) -> None:
        self.fixture_definition_id = fixture_definition_id
        self.slug = slug
        super().__init__(
            f"fixture definition identity {fixture_definition_id} or slug {slug!r} "
            "already exists"
        )


class FixtureAssignmentError(ApplicationServiceError):
    """A fixture cannot be assigned under the approved compatibility policy."""


class PersistentFixtureUnsupportedError(FixtureAssignmentError):
    def __init__(self, managed_instance_id: UUID) -> None:
        self.managed_instance_id = managed_instance_id
        super().__init__("fixtures are supported only for disposable managed instances")


class FixtureIncompatibleError(FixtureAssignmentError):
    def __init__(
        self,
        *,
        fixture_definition_id: UUID,
        fixture_kodi_version: str,
        runtime_kodi_version: str,
    ) -> None:
        self.fixture_definition_id = fixture_definition_id
        self.fixture_kodi_version = fixture_kodi_version
        self.runtime_kodi_version = runtime_kodi_version
        super().__init__(
            f"fixture {fixture_definition_id} for Kodi {fixture_kodi_version} "
            f"is incompatible with runtime Kodi {runtime_kodi_version}"
        )


class RuntimeDefinitionConflictError(PersistenceConflictError):
    def __init__(self, *, runtime_definition_id: UUID, slug: str) -> None:
        self.runtime_definition_id = runtime_definition_id
        self.slug = slug
        super().__init__(
            f"runtime definition identity {runtime_definition_id} or slug {slug!r} "
            "already exists"
        )


class RuntimeRevisionConflictError(PersistenceConflictError):
    def __init__(
        self,
        *,
        runtime_revision_id: UUID,
        runtime_definition_id: UUID,
        revision_number: int,
    ) -> None:
        self.runtime_revision_id = runtime_revision_id
        self.runtime_definition_id = runtime_definition_id
        self.revision_number = revision_number
        super().__init__(
            f"runtime revision identity {runtime_revision_id} or revision number "
            f"{revision_number} for runtime definition {runtime_definition_id} "
            "already exists"
        )


class RuntimeContainerArtifactConflictError(PersistenceConflictError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(
            f"runtime container artifact for revision {runtime_revision_id} "
            "already exists"
        )


class RuntimeExecutionProfileConflictError(PersistenceConflictError):
    def __init__(self, runtime_revision_id: UUID) -> None:
        self.runtime_revision_id = runtime_revision_id
        super().__init__(
            "runtime execution profile for revision "
            f"{runtime_revision_id} already exists"
        )


class RuntimeValidationEvidenceConflictError(PersistenceConflictError):
    def __init__(self, evidence_id: UUID) -> None:
        self.evidence_id = evidence_id
        super().__init__(f"runtime validation evidence {evidence_id} already exists")


class CatalogDataValidationError(ApplicationServiceError):
    """Catalog registration data failed domain validation."""


class InvalidRevisionLifecycleTransitionError(ApplicationServiceError):
    def __init__(self, *, runtime_revision_id: UUID, reason: str) -> None:
        self.runtime_revision_id = runtime_revision_id
        self.reason = reason
        super().__init__(
            f"invalid lifecycle transition for runtime revision "
            f"{runtime_revision_id}: {reason}"
        )


class InvalidSupportTierTransitionError(ApplicationServiceError):
    def __init__(self, *, runtime_definition_id: UUID, reason: str) -> None:
        self.runtime_definition_id = runtime_definition_id
        self.reason = reason
        super().__init__(
            f"invalid support transition for runtime definition "
            f"{runtime_definition_id}: {reason}"
        )

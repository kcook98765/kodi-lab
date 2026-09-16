"""Transactional managed-instance creation and runtime selection."""

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    FixtureDefinitionNotFoundError,
    FixtureIncompatibleError,
    KodiVersionIncompatibleError,
    ManagedInstanceConflictError,
    ManagedInstanceNotFoundError,
    NoEligibleRevisionError,
    RevisionNotApprovedError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionDefinitionMismatchError,
    RuntimeRevisionIneligibleError,
    RuntimeRevisionNotFoundError,
    TargetNotFoundError,
    TrustedCustomBuildConfirmationRequiredError,
    TrustedCustomBuildDisabledError,
)
from kodi_lab.application.mutations import ManagedInstanceMutationCoordinator
from kodi_lab.application.reset_operations import ResetOperationCoordinator
from kodi_lab.application.runtime_eligibility import (
    RuntimeIneligibilityReason,
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    ManagedInstanceMutationKind,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


@dataclass(frozen=True, slots=True)
class ExactRevisionSelection:
    """Select one exact revision within an intended runtime definition."""

    runtime_definition_id: UUID
    runtime_revision_id: UUID


@dataclass(frozen=True, slots=True)
class NewestEligibleRevisionSelection:
    """Select the greatest currently eligible revision within a definition."""

    runtime_definition_id: UUID


NewestApprovedRevisionSelection = NewestEligibleRevisionSelection

type RuntimeSelection = ExactRevisionSelection | NewestEligibleRevisionSelection


class ManagedInstanceService:
    """Application boundary for managed-instance selection and persistence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        trusted_custom_builds_enabled: bool = False,
        include_legacy: bool = False,
        include_prerelease: bool = False,
        reset_operations: ResetOperationCoordinator | None = None,
        mutation_coordinator: ManagedInstanceMutationCoordinator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._selection_policy = RuntimeSelectionPolicy(
            trusted_custom_builds_enabled=trusted_custom_builds_enabled,
            include_legacy=include_legacy,
            include_prerelease=include_prerelease,
        )
        self._eligibility = RuntimeRevisionEligibilityPolicy()
        self._reset_operations = reset_operations
        self._mutations = mutation_coordinator or ManagedInstanceMutationCoordinator(
            session_factory
        )

    def create(
        self,
        *,
        target_id: UUID,
        selection: RuntimeSelection,
        instance_type: InstanceType,
        start_on_host_boot: bool = False,
        notes: str | None = None,
    ) -> ManagedInstance:
        """Create a stopped managed instance in one transaction."""

        try:
            with self._session_factory.begin() as session:
                target = self._require_target(session, target_id)
                instances = ManagedInstanceRepository(session)
                if instances.get(target_id) is not None:
                    raise ManagedInstanceConflictError(target_id)
                revision = self._resolve_revision(session, target, selection)
                instance = ManagedInstance(
                    target_id=target_id,
                    instance_type=instance_type,
                    runtime_revision_id=revision.id,
                    runtime_state=InstanceRuntimeState.STOPPED,
                    start_on_host_boot=start_on_host_boot,
                    notes=notes,
                )
                instances.create(instance)
                return instance
        except IntegrityError as error:
            raise ManagedInstanceConflictError(target_id) from error

    def reassign_runtime(
        self,
        *,
        target_id: UUID,
        selection: RuntimeSelection,
    ) -> ManagedInstance:
        """Atomically change only an existing instance's exact revision."""

        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(target_id) is None:
                raise ManagedInstanceNotFoundError(target_id)
        if self._reset_operations is not None:
            self._reset_operations.require_no_active(target_id)
        with self._mutations.hold(
            target_id, ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT
        ):
            try:
                with self._session_factory.begin() as session:
                    target = self._require_target(session, target_id)
                    instances = ManagedInstanceRepository(session)
                    current = instances.get(target_id)
                    if current is None:
                        raise ManagedInstanceNotFoundError(target_id)
                    revision = self._resolve_revision(session, target, selection)
                    if current.fixture_definition_id is not None:
                        fixture = FixtureDefinitionRepository(session).get(
                            current.fixture_definition_id
                        )
                        if fixture is None:
                            raise FixtureDefinitionNotFoundError(
                                current.fixture_definition_id
                            )
                        definition = RuntimeDefinitionRepository(session).get(
                            revision.runtime_definition_id
                        )
                        assert definition is not None
                        if fixture.compatible_kodi_release != definition.kodi_release:
                            raise FixtureIncompatibleError(
                                fixture_definition_id=fixture.id,
                                fixture_kodi_version=fixture.compatible_kodi_version,
                                runtime_kodi_version=definition.kodi_version,
                            )
                    updated = replace(current, runtime_revision_id=revision.id)
                    instances.update(updated)
                    return updated
            except IntegrityError as error:
                raise ManagedInstanceConflictError(target_id) from error

    @staticmethod
    def _require_target(session: Session, target_id: UUID) -> Target:
        target = TargetRepository(session).get(target_id)
        if target is None:
            raise TargetNotFoundError(target_id)
        return target

    def _resolve_revision(
        self,
        session: Session,
        target: Target,
        selection: RuntimeSelection,
    ) -> RuntimeRevision:
        definitions = RuntimeDefinitionRepository(session)
        definition = definitions.get(selection.runtime_definition_id)
        if definition is None:
            raise RuntimeDefinitionNotFoundError(selection.runtime_definition_id)

        revisions = RuntimeRevisionRepository(session)
        if isinstance(selection, ExactRevisionSelection):
            revision = revisions.get(selection.runtime_revision_id)
            if revision is None:
                raise RuntimeRevisionNotFoundError(selection.runtime_revision_id)
            if revision.runtime_definition_id != definition.id:
                raise RuntimeRevisionDefinitionMismatchError(
                    runtime_revision_id=revision.id,
                    expected_runtime_definition_id=definition.id,
                    actual_runtime_definition_id=revision.runtime_definition_id,
                )
        else:
            revision = next(
                (
                    candidate
                    for candidate in revisions.list_for_definition_descending(
                        definition.id
                    )
                    if self._eligibility.evaluate(
                        session,
                        target=target,
                        definition=definition,
                        revision=candidate,
                        policy=self._selection_policy,
                    ).eligible
                ),
                None,
            )
            if revision is None:
                raise NoEligibleRevisionError(definition.id)

        self._validate_selection(session, target, definition, revision)
        return revision

    def _validate_selection(
        self,
        session: Session,
        target: Target,
        definition: RuntimeDefinition,
        revision: RuntimeRevision,
    ) -> None:
        eligibility = self._eligibility.evaluate(
            session,
            target=target,
            definition=definition,
            revision=revision,
            policy=self._selection_policy,
        )
        if eligibility.eligible:
            return
        reason = eligibility.reason
        assert reason is not None
        if reason is RuntimeIneligibilityReason.INCOMPATIBLE_RELEASE:
            raise KodiVersionIncompatibleError(
                target_id=target.id,
                runtime_definition_id=definition.id,
                target_kodi_version=target.kodi_version,
                runtime_kodi_version=definition.kodi_version,
            )
        if reason is RuntimeIneligibilityReason.REVISION_NOT_APPROVED:
            raise RevisionNotApprovedError(revision.id)
        if reason is RuntimeIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED:
            raise TrustedCustomBuildDisabledError(revision.id)
        if reason is RuntimeIneligibilityReason.TRUSTED_BUILD_CONFIRMATION_REQUIRED:
            raise TrustedCustomBuildConfirmationRequiredError(revision.id)
        raise RuntimeRevisionIneligibleError(revision.id, reason.value)

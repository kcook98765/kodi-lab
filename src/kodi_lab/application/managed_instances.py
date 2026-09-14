"""Transactional managed-instance creation and runtime selection."""

from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    KodiVersionIncompatibleError,
    ManagedInstanceConflictError,
    ManagedInstanceNotFoundError,
    NoApprovedRevisionError,
    RevisionNotApprovedError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionDefinitionMismatchError,
    RuntimeRevisionNotFoundError,
    TargetNotFoundError,
    TrustedCustomBuildConfirmationRequiredError,
    TrustedCustomBuildDisabledError,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
)
from kodi_lab.storage.repositories import (
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
class NewestApprovedRevisionSelection:
    """Select the greatest approved revision number within a definition."""

    runtime_definition_id: UUID


type RuntimeSelection = ExactRevisionSelection | NewestApprovedRevisionSelection


class ManagedInstanceService:
    """Application boundary for managed-instance selection and persistence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        trusted_custom_builds_enabled: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._trusted_custom_builds_enabled = trusted_custom_builds_enabled

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

        try:
            with self._session_factory.begin() as session:
                target = self._require_target(session, target_id)
                instances = ManagedInstanceRepository(session)
                current = instances.get(target_id)
                if current is None:
                    raise ManagedInstanceNotFoundError(target_id)
                revision = self._resolve_revision(session, target, selection)
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
            revision = revisions.newest_approved(definition.id)
            if revision is None:
                raise NoApprovedRevisionError(definition.id)

        self._validate_selection(target, definition, revision)
        return revision

    def _validate_selection(
        self,
        target: Target,
        definition: RuntimeDefinition,
        revision: RuntimeRevision,
    ) -> None:
        if revision.approval_state is not RevisionApprovalState.APPROVED:
            raise RevisionNotApprovedError(revision.id)
        if revision.requires_trusted_build_confirmation:
            if not self._trusted_custom_builds_enabled:
                raise TrustedCustomBuildDisabledError(revision.id)
            if revision.trusted_build_confirmed_at is None:
                raise TrustedCustomBuildConfirmationRequiredError(revision.id)
        if target.kodi_version.strip() != definition.kodi_version.strip():
            raise KodiVersionIncompatibleError(
                target_id=target.id,
                runtime_definition_id=definition.id,
                target_kodi_version=target.kodi_version,
                runtime_kodi_version=definition.kodi_version,
            )

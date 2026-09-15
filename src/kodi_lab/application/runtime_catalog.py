"""Transactional administration of the finalized runtime catalog."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    CatalogDataValidationError,
    InvalidRevisionLifecycleTransitionError,
    RuntimeContainerArtifactConflictError,
    RuntimeDefinitionConflictError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionConflictError,
    RuntimeRevisionNotFoundError,
)
from kodi_lab.domain import (
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeRevision,
)
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
)


class RuntimeCatalogAdministrationService:
    """Application boundary for catalog registration and lifecycle metadata."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def register_definition(
        self, *, definition_id: UUID, slug: str, kodi_version: str
    ) -> RuntimeDefinition:
        """Register one new logical runtime definition atomically."""

        try:
            definition = RuntimeDefinition(
                id=definition_id,
                slug=slug,
                kodi_version=kodi_version,
            )
        except ValueError as error:
            raise CatalogDataValidationError(str(error)) from error

        try:
            with self._session_factory.begin() as session:
                RuntimeDefinitionRepository(session).create(definition)
                return definition
        except IntegrityError as error:
            raise RuntimeDefinitionConflictError(
                runtime_definition_id=definition.id,
                slug=definition.slug,
            ) from error

    def register_container_artifact(
        self, *, runtime_revision_id: UUID, image_id: str
    ) -> RuntimeContainerArtifact:
        """Register one immutable Docker image mapping for an existing revision."""

        try:
            artifact = RuntimeContainerArtifact(
                runtime_revision_id=runtime_revision_id,
                image_id=image_id,
            )
        except ValueError as error:
            raise CatalogDataValidationError(str(error)) from error
        try:
            with self._session_factory.begin() as session:
                if RuntimeRevisionRepository(session).get(runtime_revision_id) is None:
                    raise RuntimeRevisionNotFoundError(runtime_revision_id)
                RuntimeContainerArtifactRepository(session).create(artifact)
                return artifact
        except IntegrityError as error:
            raise RuntimeContainerArtifactConflictError(runtime_revision_id) from error

    def register_revision(
        self,
        *,
        revision_id: UUID,
        runtime_definition_id: UUID,
        revision_number: int,
        source_kind: str,
        source_reference: str,
        content_digest: str,
        manifest_reference: str,
        manifest_digest: str,
        built_at: datetime,
        build_log_reference: str | None = None,
        requires_trusted_build_confirmation: bool = False,
    ) -> RuntimeRevision:
        """Register a complete finalized revision in an unapproved state."""

        try:
            revision = RuntimeRevision(
                id=revision_id,
                runtime_definition_id=runtime_definition_id,
                revision_number=revision_number,
                source_kind=source_kind,
                source_reference=source_reference,
                content_digest=content_digest,
                manifest_reference=manifest_reference,
                manifest_digest=manifest_digest,
                built_at=built_at,
                build_log_reference=build_log_reference,
                requires_trusted_build_confirmation=(
                    requires_trusted_build_confirmation
                ),
            )
        except ValueError as error:
            raise CatalogDataValidationError(str(error)) from error

        try:
            with self._session_factory.begin() as session:
                if (
                    RuntimeDefinitionRepository(session).get(runtime_definition_id)
                    is None
                ):
                    raise RuntimeDefinitionNotFoundError(runtime_definition_id)
                RuntimeRevisionRepository(session).create(revision)
                return revision
        except IntegrityError as error:
            raise RuntimeRevisionConflictError(
                runtime_revision_id=revision.id,
                runtime_definition_id=revision.runtime_definition_id,
                revision_number=revision.revision_number,
            ) from error

    def approve_revision(
        self,
        revision_id: UUID,
        *,
        approved_at: datetime,
        note: str | None = None,
    ) -> RuntimeRevision:
        """Approve a revision, leaving an existing approval event unchanged."""

        try:
            with self._session_factory.begin() as session:
                repository = RuntimeRevisionRepository(session)
                current = self._require_revision(repository, revision_id)
                candidate = current.approve(approved_at=approved_at, note=note)
                if current.approval_state is RevisionApprovalState.APPROVED:
                    return current
                normalized_approved_at = candidate.approved_at
                assert normalized_approved_at is not None
                return repository.approve(
                    revision_id,
                    approved_at=normalized_approved_at,
                    note=candidate.approval_note,
                )
        except ValueError as error:
            raise self._invalid_transition(revision_id, error) from error
        except IntegrityError as error:
            raise self._invalid_transition(revision_id, error) from error

    def revoke_revision_approval(self, revision_id: UUID) -> RuntimeRevision:
        """Revoke approval; already-unapproved revisions are unchanged."""

        try:
            with self._session_factory.begin() as session:
                repository = RuntimeRevisionRepository(session)
                current = self._require_revision(repository, revision_id)
                if current.approval_state is RevisionApprovalState.NOT_APPROVED:
                    return current
                return repository.revoke_approval(revision_id)
        except IntegrityError as error:
            raise self._invalid_transition(revision_id, error) from error

    def confirm_trusted_custom_build(
        self, revision_id: UUID, *, confirmed_at: datetime
    ) -> RuntimeRevision:
        """Record required per-revision trust acceptance independently."""

        try:
            with self._session_factory.begin() as session:
                repository = RuntimeRevisionRepository(session)
                current = self._require_revision(repository, revision_id)
                candidate = current.confirm_trusted_build(confirmed_at=confirmed_at)
                if current.trusted_build_confirmed_at is not None:
                    return current
                normalized_confirmed_at = candidate.trusted_build_confirmed_at
                assert normalized_confirmed_at is not None
                return repository.confirm_trusted_build(
                    revision_id,
                    confirmed_at=normalized_confirmed_at,
                )
        except ValueError as error:
            raise self._invalid_transition(revision_id, error) from error
        except IntegrityError as error:
            raise self._invalid_transition(revision_id, error) from error

    def revoke_trusted_custom_build_confirmation(
        self, revision_id: UUID
    ) -> RuntimeRevision:
        """Clear per-revision trust acceptance when that lifecycle applies."""

        try:
            with self._session_factory.begin() as session:
                repository = RuntimeRevisionRepository(session)
                current = self._require_revision(repository, revision_id)
                if not current.requires_trusted_build_confirmation:
                    raise InvalidRevisionLifecycleTransitionError(
                        runtime_revision_id=revision_id,
                        reason=(
                            "revision does not require Trusted Custom Build "
                            "confirmation"
                        ),
                    )
                if current.trusted_build_confirmed_at is None:
                    return current
                return repository.revoke_trusted_build_confirmation(revision_id)
        except IntegrityError as error:
            raise self._invalid_transition(revision_id, error) from error

    @staticmethod
    def _require_revision(
        repository: RuntimeRevisionRepository, revision_id: UUID
    ) -> RuntimeRevision:
        revision = repository.get(revision_id)
        if revision is None:
            raise RuntimeRevisionNotFoundError(revision_id)
        return revision

    @staticmethod
    def _invalid_transition(
        revision_id: UUID, error: ValueError | IntegrityError
    ) -> InvalidRevisionLifecycleTransitionError:
        reason = (
            str(error)
            if isinstance(error, ValueError)
            else "persisted lifecycle invariant rejected the transition"
        )
        return InvalidRevisionLifecycleTransitionError(
            runtime_revision_id=revision_id,
            reason=reason,
        )

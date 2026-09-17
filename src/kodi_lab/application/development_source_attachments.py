"""Durable mode-neutral per-instance development-source attachment service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    DevelopmentSourceAddonConflictError,
    DevelopmentSourceAlreadyAttachedError,
    DevelopmentSourceAttachmentError,
    DevelopmentSourceAttachmentValidationError,
    DevelopmentSourceIdentityChangedError,
    DevelopmentSourceNotAttachedError,
    DevelopmentSourceNotFoundError,
    ManagedInstanceNotFoundError,
    PersistentDevelopmentSourceAttachmentUnsupportedError,
)
from kodi_lab.application.mutations import ManagedInstanceMutationCoordinator
from kodi_lab.development import (
    DevelopmentSourceValidationError,
    DevelopmentSourceValidator,
)
from kodi_lab.domain import (
    MAX_ADDON_ID,
    MAX_ADDON_NAME,
    MAX_ADDON_VERSION,
    MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME,
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceKind,
    InstanceType,
    ManagedInstanceDevelopmentSource,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
)


class AttachedDevelopmentSourceRead(BaseModel):
    """Path-free read projection of one durable source association."""

    model_config = ConfigDict(extra="forbid")

    development_source_id: UUID
    logical_name: str = Field(max_length=MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME)
    addon_id: str = Field(max_length=MAX_ADDON_ID)
    observed_name: str = Field(max_length=MAX_ADDON_NAME)
    observed_version: str = Field(max_length=MAX_ADDON_VERSION)
    source_kind: DevelopmentSourceKind
    attached_at: datetime
    deployment_confidence: DevelopmentSourceDeploymentConfidence | None = None
    deployment_verified_at: datetime | None = None
    deployment_wording: str = Field(default="Never deployed", max_length=64)
    copy_sync_eligible: bool = False


def attached_development_source_read(
    attachment: ManagedInstanceDevelopmentSource, source: DevelopmentSource
) -> AttachedDevelopmentSourceRead:
    return AttachedDevelopmentSourceRead(
        development_source_id=source.id,
        logical_name=source.logical_name,
        addon_id=source.addon_id,
        observed_name=source.observed_name,
        observed_version=source.observed_version,
        source_kind=source.source_kind,
        attached_at=attachment.attached_at,
    )


class DevelopmentSourceAttachmentService:
    """Attach, detach, and list catalog identities without deployment semantics."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        validator: DevelopmentSourceValidator,
        *,
        mutation_coordinator: ManagedInstanceMutationCoordinator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._validator = validator
        self._mutations = mutation_coordinator or ManagedInstanceMutationCoordinator(
            session_factory
        )
        self._clock = clock
        self._uuid_factory = uuid_factory

    def attach_source(
        self, instance_id: UUID, source_id: UUID
    ) -> AttachedDevelopmentSourceRead:
        """Revalidate and atomically associate one catalog source."""

        source = self._preflight(instance_id, source_id)
        operation_id = self._uuid_factory()
        requested_at = self._clock()
        lease = self._mutations.acquire(
            instance_id, ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT
        )
        try:
            try:
                observation = self._validator.validate(source.relative_locator)
                if observation.addon_id != source.addon_id:
                    raise DevelopmentSourceIdentityChangedError(source.id)
                with self._session_factory.begin() as session:
                    current_source = self._require_context(
                        session, instance_id, source_id
                    )
                    attachments = ManagedInstanceDevelopmentSourceRepository(session)
                    if attachments.get(instance_id, source_id) is not None:
                        raise DevelopmentSourceAlreadyAttachedError(
                            instance_id, source_id
                        )
                    conflict = attachments.get_by_addon_id(
                        instance_id, current_source.addon_id
                    )
                    if conflict is not None:
                        raise DevelopmentSourceAddonConflictError(
                            instance_id, current_source.addon_id
                        )
                    attached_at = self._clock()
                    attachment = ManagedInstanceDevelopmentSource(
                        managed_instance_id=instance_id,
                        development_source_id=source_id,
                        attached_at=attached_at,
                    )
                    attachments.create(attachment)
                    DevelopmentSourceAttachmentOperationRepository(session).create(
                        self._operation(
                            operation_id,
                            instance_id,
                            current_source,
                            DevelopmentSourceAttachmentOperationKind.ATTACH,
                            requested_at,
                            attached_at,
                            DevelopmentSourceAttachmentOperationState.SUCCEEDED,
                        )
                    )
                    return attached_development_source_read(attachment, current_source)
            except DevelopmentSourceValidationError as error:
                wrapped = DevelopmentSourceAttachmentValidationError(error.code.value)
                self._record_failure(
                    operation_id,
                    instance_id,
                    source,
                    DevelopmentSourceAttachmentOperationKind.ATTACH,
                    requested_at,
                    wrapped,
                )
                raise wrapped from error
            except DevelopmentSourceAttachmentError as error:
                self._record_failure(
                    operation_id,
                    instance_id,
                    source,
                    DevelopmentSourceAttachmentOperationKind.ATTACH,
                    requested_at,
                    error,
                )
                raise
        finally:
            self._mutations.release(lease)

    def detach_source(
        self, instance_id: UUID, source_id: UUID
    ) -> AttachedDevelopmentSourceRead:
        """Remove only one association and retain typed history."""

        source = self._preflight(instance_id, source_id)
        operation_id = self._uuid_factory()
        requested_at = self._clock()
        lease = self._mutations.acquire(
            instance_id, ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT
        )
        try:
            try:
                with self._session_factory.begin() as session:
                    current_source = self._require_context(
                        session, instance_id, source_id
                    )
                    attachments = ManagedInstanceDevelopmentSourceRepository(session)
                    attachment = attachments.get(instance_id, source_id)
                    if attachment is None:
                        raise DevelopmentSourceNotAttachedError(instance_id, source_id)
                    if not attachments.delete(instance_id, source_id):
                        raise DevelopmentSourceNotAttachedError(instance_id, source_id)
                    completed_at = self._clock()
                    DevelopmentSourceAttachmentOperationRepository(session).create(
                        self._operation(
                            operation_id,
                            instance_id,
                            current_source,
                            DevelopmentSourceAttachmentOperationKind.DETACH,
                            requested_at,
                            completed_at,
                            DevelopmentSourceAttachmentOperationState.SUCCEEDED,
                        )
                    )
                    return attached_development_source_read(attachment, current_source)
            except DevelopmentSourceAttachmentError as error:
                self._record_failure(
                    operation_id,
                    instance_id,
                    source,
                    DevelopmentSourceAttachmentOperationKind.DETACH,
                    requested_at,
                    error,
                )
                raise
        finally:
            self._mutations.release(lease)

    def list_attached_sources(
        self, instance_id: UUID
    ) -> list[AttachedDevelopmentSourceRead]:
        """Return deterministic path-free association metadata."""

        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            attachments = ManagedInstanceDevelopmentSourceRepository(
                session
            ).list_for_instance(instance_id)
            sources = DevelopmentSourceRepository(session)
            result: list[AttachedDevelopmentSourceRead] = []
            for attachment in attachments:
                source = sources.get(attachment.development_source_id)
                if source is None:
                    raise DevelopmentSourceNotFoundError(
                        attachment.development_source_id
                    )
                result.append(attached_development_source_read(attachment, source))
            return result

    def _preflight(self, instance_id: UUID, source_id: UUID) -> DevelopmentSource:
        with self._session_factory() as session:
            return self._require_context(session, instance_id, source_id)

    @staticmethod
    def _require_context(
        session: Session, instance_id: UUID, source_id: UUID
    ) -> DevelopmentSource:
        instance = ManagedInstanceRepository(session).get(instance_id)
        if instance is None:
            raise ManagedInstanceNotFoundError(instance_id)
        if instance.instance_type is not InstanceType.DISPOSABLE:
            raise PersistentDevelopmentSourceAttachmentUnsupportedError(instance_id)
        source = DevelopmentSourceRepository(session).get(source_id)
        if source is None:
            raise DevelopmentSourceNotFoundError(source_id)
        return source

    def _record_failure(
        self,
        operation_id: UUID,
        instance_id: UUID,
        source: DevelopmentSource,
        kind: DevelopmentSourceAttachmentOperationKind,
        requested_at: datetime,
        error: DevelopmentSourceAttachmentError,
    ) -> None:
        reason = getattr(error, "code", error.__class__.__name__)
        if not isinstance(reason, str):
            reason = str(reason)
        with self._session_factory.begin() as session:
            DevelopmentSourceAttachmentOperationRepository(session).create(
                self._operation(
                    operation_id,
                    instance_id,
                    source,
                    kind,
                    requested_at,
                    self._clock(),
                    DevelopmentSourceAttachmentOperationState.FAILED,
                    failure_reason=reason[:256],
                )
            )

    @staticmethod
    def _operation(
        operation_id: UUID,
        instance_id: UUID,
        source: DevelopmentSource,
        kind: DevelopmentSourceAttachmentOperationKind,
        requested_at: datetime,
        completed_at: datetime,
        state: DevelopmentSourceAttachmentOperationState,
        *,
        failure_reason: str | None = None,
    ) -> DevelopmentSourceAttachmentOperation:
        return DevelopmentSourceAttachmentOperation(
            id=operation_id,
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            development_source_id=source.id,
            source_logical_name=source.logical_name,
            addon_id=source.addon_id,
            kind=kind,
            requested_at=requested_at,
            completed_at=completed_at,
            state=state,
            failure_reason=failure_reason,
        )

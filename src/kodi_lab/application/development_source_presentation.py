"""Path-free read projections for development-source Web presentation."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_attachments import (
    AttachedDevelopmentSourceRead,
    attached_development_source_read,
)
from kodi_lab.application.development_sources import (
    DevelopmentSourceRead,
    development_source_read,
)
from kodi_lab.application.errors import (
    DevelopmentSourceNotFoundError,
    ManagedInstanceNotFoundError,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


class DevelopmentSourceSummary(BaseModel):
    """One registered source and its current live attachment count."""

    model_config = ConfigDict(extra="forbid")

    source: DevelopmentSourceRead
    attached_instance_count: int = Field(ge=0)


class DevelopmentSourceAttachedInstanceRead(BaseModel):
    """Safe current instance identity shown from a source detail view."""

    model_config = ConfigDict(extra="forbid")

    managed_instance_id: UUID
    instance_name: str = Field(max_length=256)
    kodi_version: str = Field(max_length=32)
    runtime_revision_id: UUID
    runtime_revision_number: int = Field(ge=1)
    attached_at: datetime


class DevelopmentSourceDetail(BaseModel):
    """Stored source observation plus current live associations."""

    model_config = ConfigDict(extra="forbid")

    source: DevelopmentSourceRead
    attached_instances: list[DevelopmentSourceAttachedInstanceRead]


class DevelopmentSourcePresentationService:
    """Build read-only DTOs without consulting the host source filesystem."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_sources(self) -> list[DevelopmentSourceSummary]:
        with self._session_factory() as session:
            sources = DevelopmentSourceRepository(session).list()
            counts = ManagedInstanceDevelopmentSourceRepository(
                session
            ).counts_by_source()
            return [
                DevelopmentSourceSummary(
                    source=development_source_read(source),
                    attached_instance_count=counts.get(source.id, 0),
                )
                for source in sources
            ]

    def get_source(self, source_id: UUID) -> DevelopmentSourceDetail:
        with self._session_factory() as session:
            sources = DevelopmentSourceRepository(session)
            source = sources.get(source_id)
            if source is None:
                raise DevelopmentSourceNotFoundError(source_id)
            attachments = ManagedInstanceDevelopmentSourceRepository(session)
            result: list[DevelopmentSourceAttachedInstanceRead] = []
            for instance_id in attachments.list_instance_ids_for_source(source_id):
                instance = ManagedInstanceRepository(session).get(instance_id)
                target = TargetRepository(session).get(instance_id)
                if instance is None or target is None:
                    continue
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    continue
                association = attachments.get(instance_id, source_id)
                if association is None:
                    continue
                result.append(
                    DevelopmentSourceAttachedInstanceRead(
                        managed_instance_id=instance_id,
                        instance_name=target.name,
                        kodi_version=target.kodi_version,
                        runtime_revision_id=revision.id,
                        runtime_revision_number=revision.revision_number,
                        attached_at=association.attached_at,
                    )
                )
            result.sort(
                key=lambda item: (item.instance_name, str(item.managed_instance_id))
            )
            return DevelopmentSourceDetail(
                source=development_source_read(source), attached_instances=result
            )

    def list_attached_sources(
        self, instance_id: UUID
    ) -> list[AttachedDevelopmentSourceRead]:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is None:
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

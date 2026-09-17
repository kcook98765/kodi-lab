"""Narrow live-instance detail and deleted-instance history projections."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.activity import (
    ManagedInstanceActivityItem,
    ManagedInstanceActivityService,
)
from kodi_lab.application.development_source_attachments import (
    AttachedDevelopmentSourceRead,
)
from kodi_lab.application.development_source_presentation import (
    DevelopmentSourcePresentationService,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ManagedInstanceReconciliationStatus,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.snapshot_status import (
    SnapshotHistoryItem,
    SnapshotStatusService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import InstanceType, ManagedInstanceDeleteOperation
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
)


class InstanceFixtureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_definition_id: UUID
    slug: str = Field(max_length=64)
    fixture_version: int
    size_category: str = Field(max_length=16)
    content_category: str = Field(max_length=16)


class ManagedInstanceDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: ManagedRuntimeStatus
    instance_type: InstanceType
    notes: str | None = Field(default=None, max_length=4096)
    fixture: InstanceFixtureDetail | None
    snapshots: list[SnapshotHistoryItem]
    development_sources: list[AttachedDevelopmentSourceRead]
    recent_activity: list[ManagedInstanceActivityItem] = Field(max_length=10)
    reconciliation: ManagedInstanceReconciliationStatus


class DeletedManagedInstanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_managed_instance_id: UUID
    historical_name: str = Field(max_length=256)
    deletion_operation_uuid: UUID
    deletion_time: datetime | None
    operation_state: str = Field(max_length=32)
    kodi_release: str = Field(max_length=32)
    runtime_revision_id: UUID
    runtime_revision_number: int
    fixture_definition_id: UUID | None
    fixture_slug: str | None = Field(default=None, max_length=64)
    preserve_snapshots: bool
    reconciliation_required: bool


class DeletedManagedInstanceHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: DeletedManagedInstanceSummary
    activity: list[ManagedInstanceActivityItem]
    preserved_snapshots: list[SnapshotHistoryItem]


class DeletedManagedInstanceHistoryNotFoundError(LookupError):
    pass


class ManagedInstanceDetailService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        status: ManagedRuntimeStatusService,
        snapshots: SnapshotStatusService,
        activity: ManagedInstanceActivityService,
        reconciliation: ManagedInstanceReconciliationService,
        development_sources: DevelopmentSourcePresentationService,
    ) -> None:
        self._session_factory = session_factory
        self._status = status
        self._snapshots = snapshots
        self._activity = activity
        self._reconciliation = reconciliation
        self._development_sources = development_sources

    def get(self, instance_id: UUID) -> ManagedInstanceDetail:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            fixture = (
                None
                if instance.fixture_definition_id is None
                else FixtureDefinitionRepository(session).get(
                    instance.fixture_definition_id
                )
            )
        runtime = self._status.get(instance_id)
        reconciliation = self._reconciliation.status(instance_id)
        sources = self._development_sources.list_attached_sources(instance_id)
        with self._session_factory() as session:
            active_mutation = (
                ManagedInstanceMutationLeaseRepository(session).current(instance_id)
                is not None
            )
        eligible = (
            instance.instance_type is InstanceType.DISPOSABLE
            and runtime.provision_state.value == "provisioned"
            and runtime.container_state is ContainerRuntimeState.STOPPED
            and not reconciliation.blocking
            and not active_mutation
        )
        return ManagedInstanceDetail(
            runtime=runtime,
            instance_type=instance.instance_type,
            notes=instance.notes,
            fixture=(
                None
                if fixture is None
                else InstanceFixtureDetail(
                    fixture_definition_id=fixture.id,
                    slug=fixture.slug,
                    fixture_version=fixture.fixture_version,
                    size_category=fixture.size_category.value,
                    content_category=fixture.content_category.value,
                )
            ),
            snapshots=self._snapshots.list_for_instance(instance_id),
            development_sources=[
                source.model_copy(update={"copy_sync_eligible": eligible})
                for source in sources
            ],
            recent_activity=self._activity.list_for_live_instance(
                instance_id, limit=10
            ),
            reconciliation=reconciliation,
        )


class DeletedManagedInstanceHistoryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        activity: ManagedInstanceActivityService,
        snapshots: SnapshotStatusService,
    ) -> None:
        self._session_factory = session_factory
        self._activity = activity
        self._snapshots = snapshots

    def list(self) -> list[DeletedManagedInstanceSummary]:
        with self._session_factory() as session:
            latest: dict[UUID, ManagedInstanceDeleteOperation] = {}
            for operation in ManagedInstanceDeleteOperationRepository(
                session
            ).list_all():
                if operation.original_managed_instance_id in latest:
                    continue
                if (
                    ManagedInstanceRepository(session).get(
                        operation.original_managed_instance_id
                    )
                    is not None
                ):
                    continue
                latest[operation.original_managed_instance_id] = operation
            return [
                summary
                for operation in latest.values()
                if (summary := self._summary(session, operation)) is not None
            ]

    def get(self, instance_id: UUID) -> DeletedManagedInstanceHistory:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is not None:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
            operations = ManagedInstanceDeleteOperationRepository(
                session
            ).list_for_original_instance(instance_id)
            if not operations:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
            summary = self._summary(session, operations[0])
            if summary is None:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
        preserved = [
            item
            for item in self._snapshots.list_preserved()
            if item.original_source_managed_instance_id == instance_id
        ]
        return DeletedManagedInstanceHistory(
            summary=summary,
            activity=self._activity.list_for_original_instance(instance_id),
            preserved_snapshots=preserved,
        )

    @staticmethod
    def _summary(
        session: Session, operation: ManagedInstanceDeleteOperation
    ) -> DeletedManagedInstanceSummary | None:
        revision = RuntimeRevisionRepository(session).get(operation.runtime_revision_id)
        if revision is None:
            return None
        definition = RuntimeDefinitionRepository(session).get(
            revision.runtime_definition_id
        )
        if definition is None:
            return None
        fixture = (
            None
            if operation.fixture_definition_id is None
            else FixtureDefinitionRepository(session).get(
                operation.fixture_definition_id
            )
        )
        return DeletedManagedInstanceSummary(
            original_managed_instance_id=operation.original_managed_instance_id,
            historical_name=operation.source_instance_name,
            deletion_operation_uuid=operation.id,
            deletion_time=operation.completed_at,
            operation_state=operation.state.value,
            kodi_release=definition.kodi_version,
            runtime_revision_id=revision.id,
            runtime_revision_number=revision.revision_number,
            fixture_definition_id=operation.fixture_definition_id,
            fixture_slug=None if fixture is None else fixture.slug,
            preserve_snapshots=operation.preserve_snapshots,
            reconciliation_required=operation.reconciliation_required,
        )

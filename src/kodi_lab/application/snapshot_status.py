"""Safe metadata-only snapshot history projections for Web."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.domain import SnapshotState, SnapshotValidationResult
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    SnapshotRepository,
)


class SnapshotHistoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    label: str | None = Field(default=None, max_length=120)
    captured_at: datetime
    state: SnapshotState
    archive_size: int | None
    member_count: int | None
    kodi_release: str = Field(max_length=32)
    runtime_revision_id: UUID
    runtime_revision_number: int
    fixture_definition_id: UUID | None
    fixture_slug: str | None = Field(default=None, max_length=64)
    validation_result: SnapshotValidationResult


class SnapshotStatusService:
    """Project persisted snapshot identity without exposing artifact contents."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_for_instance(self, instance_id: UUID) -> list[SnapshotHistoryItem]:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is None:
                raise ManagedInstanceNotFoundError(instance_id)
            revisions = RuntimeRevisionRepository(session)
            definitions = RuntimeDefinitionRepository(session)
            fixtures = FixtureDefinitionRepository(session)
            result: list[SnapshotHistoryItem] = []
            for snapshot in SnapshotRepository(session).list_for_instance(instance_id):
                revision = revisions.get(snapshot.runtime_revision_id)
                if revision is None:
                    continue
                definition = definitions.get(revision.runtime_definition_id)
                if definition is None:
                    continue
                fixture = (
                    None
                    if snapshot.captured_fixture_definition_id is None
                    else fixtures.get(snapshot.captured_fixture_definition_id)
                )
                result.append(
                    SnapshotHistoryItem(
                        snapshot_id=snapshot.id,
                        label=snapshot.label,
                        captured_at=snapshot.captured_at,
                        state=snapshot.state,
                        archive_size=snapshot.archive_size,
                        member_count=snapshot.member_count,
                        kodi_release=definition.kodi_version,
                        runtime_revision_id=revision.id,
                        runtime_revision_number=revision.revision_number,
                        fixture_definition_id=snapshot.captured_fixture_definition_id,
                        fixture_slug=None if fixture is None else fixture.slug,
                        validation_result=snapshot.validation_result,
                    )
                )
            return result

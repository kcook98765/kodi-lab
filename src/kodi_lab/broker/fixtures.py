"""Broker-trusted resolution of assigned predefined fixture content."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import (
    AssignedFixtureIncompatibleError,
    AssignedFixtureMissingError,
    AssignedFixtureStateError,
    PackagedFixtureUnavailableError,
    PersistentProvisioningUnsupportedError,
)
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceType
from kodi_lab.fixtures.package import (
    FIXTURE_CONTAINER_DESTINATION,
    PackagedFixtureError,
    PackagedFixtureFile,
    load_packaged_fixture,
)
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
)


@dataclass(frozen=True, slots=True)
class AssignedFixtureSpec:
    managed_instance_id: str
    runtime_revision_id: str
    fixture_id: str
    fixture_slug: str
    fixture_version: int
    content_digest: str
    destination: str
    archive: bytes
    members: tuple[PackagedFixtureFile, ...]


class AssignedFixtureResolver(Protocol):
    def resolve(self, managed_instance_id: str) -> AssignedFixtureSpec: ...


class SqliteAssignedFixtureResolver:
    """Resolve fixture authority solely from persistence and package resources."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def production(cls) -> "SqliteAssignedFixtureResolver":
        engine = create_database_engine(Settings().database_url)
        return cls(sessionmaker(engine, expire_on_commit=False))

    def resolve(self, managed_instance_id: str) -> AssignedFixtureSpec:
        try:
            identity = UUID(managed_instance_id)
            if str(identity) != managed_instance_id:
                raise ValueError
        except ValueError as error:
            raise AssignedFixtureStateError(
                "Managed instance identity is not canonical"
            ) from error
        try:
            with self._session_factory() as session:
                instance = ManagedInstanceRepository(session).get(identity)
                if instance is None:
                    raise AssignedFixtureStateError("Managed instance does not exist")
                if instance.instance_type is not InstanceType.DISPOSABLE:
                    raise PersistentProvisioningUnsupportedError(
                        "Persistent fixtures are not supported"
                    )
                if instance.fixture_definition_id is None:
                    raise AssignedFixtureMissingError(
                        "Managed instance has no assigned fixture"
                    )
                revision = RuntimeRevisionRepository(session).get(
                    instance.runtime_revision_id
                )
                if revision is None:
                    raise AssignedFixtureStateError("Pinned revision does not exist")
                definition = RuntimeDefinitionRepository(session).get(
                    revision.runtime_definition_id
                )
                fixture = FixtureDefinitionRepository(session).get(
                    instance.fixture_definition_id
                )
                if definition is None or fixture is None:
                    raise AssignedFixtureStateError(
                        "Assigned fixture catalog state is unavailable"
                    )
                if definition.kodi_release != fixture.compatible_kodi_release:
                    raise AssignedFixtureIncompatibleError(
                        "Assigned fixture is incompatible with the pinned runtime"
                    )
                try:
                    packaged = load_packaged_fixture(fixture.slug)
                except PackagedFixtureError as error:
                    raise PackagedFixtureUnavailableError(
                        "Assigned fixture package is unavailable"
                    ) from error
                if packaged.definition != fixture:
                    raise PackagedFixtureUnavailableError(
                        "Assigned fixture package differs from persisted metadata"
                    )
                if packaged.destination != FIXTURE_CONTAINER_DESTINATION:
                    raise PackagedFixtureUnavailableError(
                        "Assigned fixture destination is not trusted"
                    )
                return AssignedFixtureSpec(
                    managed_instance_id=managed_instance_id,
                    runtime_revision_id=str(revision.id),
                    fixture_id=str(fixture.id),
                    fixture_slug=fixture.slug,
                    fixture_version=fixture.fixture_version,
                    content_digest=fixture.content_digest,
                    destination=packaged.destination,
                    archive=packaged.build_archive(),
                    members=packaged.members,
                )
        except AssignedFixtureStateError:
            raise
        except SQLAlchemyError as error:
            raise AssignedFixtureStateError(
                "Canonical assigned-fixture state could not be read"
            ) from error

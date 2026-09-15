"""Trusted fixture registration and managed-instance assignment services."""

from dataclasses import replace
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    FixtureDefinitionConflictError,
    FixtureDefinitionNotFoundError,
    FixtureIncompatibleError,
    ManagedInstanceNotFoundError,
    PersistentFixtureUnsupportedError,
)
from kodi_lab.application.reset_operations import ResetOperationCoordinator
from kodi_lab.domain import FixtureDefinition, InstanceType, ManagedInstance
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
)


class FixtureAdministrationService:
    """Create immutable definitions from packages and assign them explicitly."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        reset_operations: ResetOperationCoordinator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._reset_operations = reset_operations

    def register_packaged_fixture(self, slug: str) -> FixtureDefinition:
        fixture = load_packaged_fixture(slug).definition
        try:
            with self._session_factory.begin() as session:
                repository = FixtureDefinitionRepository(session)
                existing = repository.get(fixture.id)
                if existing == fixture:
                    return existing
                if (
                    existing is not None
                    or repository.get_by_slug(fixture.slug) is not None
                ):
                    raise FixtureDefinitionConflictError(
                        fixture_definition_id=fixture.id, slug=fixture.slug
                    )
                repository.create(fixture)
                return fixture
        except IntegrityError as error:
            raise FixtureDefinitionConflictError(
                fixture_definition_id=fixture.id, slug=fixture.slug
            ) from error

    def assign(
        self, *, managed_instance_id: UUID, fixture_definition_id: UUID
    ) -> ManagedInstance:
        self._require_no_reset(managed_instance_id)
        with self._session_factory.begin() as session:
            instances = ManagedInstanceRepository(session)
            instance = instances.get(managed_instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(managed_instance_id)
            fixture = FixtureDefinitionRepository(session).get(fixture_definition_id)
            if fixture is None:
                raise FixtureDefinitionNotFoundError(fixture_definition_id)
            self._require_compatible(session, instance, fixture)
            updated = replace(instance, fixture_definition_id=fixture.id)
            instances.update(updated)
            return updated

    def unassign(self, *, managed_instance_id: UUID) -> ManagedInstance:
        self._require_no_reset(managed_instance_id)
        with self._session_factory.begin() as session:
            instances = ManagedInstanceRepository(session)
            instance = instances.get(managed_instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(managed_instance_id)
            updated = replace(instance, fixture_definition_id=None)
            instances.update(updated)
            return updated

    def _require_no_reset(self, instance_id: UUID) -> None:
        if self._reset_operations is not None:
            self._reset_operations.require_no_active(instance_id)

    @staticmethod
    def _require_compatible(
        session: Session, instance: ManagedInstance, fixture: FixtureDefinition
    ) -> None:
        if instance.instance_type is not InstanceType.DISPOSABLE:
            raise PersistentFixtureUnsupportedError(instance.target_id)
        revision = RuntimeRevisionRepository(session).get(instance.runtime_revision_id)
        if revision is None:
            raise ManagedInstanceNotFoundError(instance.target_id)
        definition = RuntimeDefinitionRepository(session).get(
            revision.runtime_definition_id
        )
        if (
            definition is None
            or definition.kodi_release != fixture.compatible_kodi_release
        ):
            raise FixtureIncompatibleError(
                fixture_definition_id=fixture.id,
                fixture_kodi_version=fixture.compatible_kodi_version,
                runtime_kodi_version=(
                    "unavailable" if definition is None else definition.kodi_version
                ),
            )

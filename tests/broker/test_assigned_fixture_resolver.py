from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import (
    AssignedFixtureIncompatibleError,
    AssignedFixtureMissingError,
    PackagedFixtureUnavailableError,
    PersistentProvisioningUnsupportedError,
)
from kodi_lab.broker.fixtures import SqliteAssignedFixtureResolver
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'resolver.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def seed(
    factory: sessionmaker[Session],
    *,
    instance_type: InstanceType = InstanceType.DISPOSABLE,
    assigned: bool = True,
) -> tuple[UUID, UUID]:
    instance_id = uuid4()
    definition = RuntimeDefinition(uuid4(), "resolver-kodi-21-3", "21.3")
    revision = RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=1,
        source_kind="test",
        source_reference="test",
        content_digest="a" * 64,
        manifest_reference="test",
        manifest_digest="b" * 64,
        built_at=NOW,
        approval_state=RevisionApprovalState.APPROVED,
        approved_at=NOW,
    )
    fixture = load_packaged_fixture("small-mixed-v1").definition
    with factory.begin() as session:
        TargetRepository(session).create(
            Target(
                id=instance_id,
                name="Fixture resolver target",
                target_type=TargetType.MANAGED,
                kodi_version="21.3",
                availability=TargetAvailability.UNAVAILABLE,
                health=HealthStatus.WARNING,
                connection_state=ConnectionState.DISCONNECTED,
            )
        )
        RuntimeDefinitionRepository(session).create(definition)
        RuntimeRevisionRepository(session).create(revision)
        FixtureDefinitionRepository(session).create(fixture)
        ManagedInstanceRepository(session).create(
            ManagedInstance(
                target_id=instance_id,
                instance_type=instance_type,
                runtime_revision_id=revision.id,
                runtime_state=InstanceRuntimeState.STOPPED,
                fixture_definition_id=fixture.id if assigned else None,
            )
        )
    return instance_id, revision.id


def test_resolver_returns_only_trusted_persisted_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id = seed(session_factory)
    packaged = load_packaged_fixture("small-mixed-v1")
    resolved = SqliteAssignedFixtureResolver(session_factory).resolve(str(instance_id))
    assert resolved.managed_instance_id == str(instance_id)
    assert resolved.runtime_revision_id == str(revision_id)
    assert resolved.fixture_id == str(packaged.definition.id)
    assert resolved.fixture_slug == "small-mixed-v1"
    assert resolved.content_digest == packaged.definition.content_digest
    assert resolved.destination == "/home/kodi/.kodi"
    assert resolved.archive == packaged.build_archive()


def test_resolver_rejects_missing_assignment_and_persistent_instance(
    session_factory: sessionmaker[Session],
) -> None:
    unassigned, _revision_id = seed(session_factory, assigned=False)
    with pytest.raises(AssignedFixtureMissingError):
        SqliteAssignedFixtureResolver(session_factory).resolve(str(unassigned))


@pytest.mark.parametrize(
    ("column", "value", "error_type"),
    [
        ("compatible_kodi_version", "20.5", AssignedFixtureIncompatibleError),
        ("content_digest", "sha256:" + ("f" * 64), PackagedFixtureUnavailableError),
        ("slug", "missing-package-v1", PackagedFixtureUnavailableError),
    ],
)
def test_resolver_rejects_incompatible_or_untrusted_package_metadata(
    session_factory: sessionmaker[Session],
    column: str,
    value: str,
    error_type: type[Exception],
) -> None:
    instance_id, _revision_id = seed(session_factory)
    assert column in {"compatible_kodi_version", "content_digest", "slug"}
    with session_factory.begin() as session:
        session.execute(
            text(f"UPDATE fixture_definitions SET {column} = :value"),
            {"value": value},
        )
    with pytest.raises(error_type):
        SqliteAssignedFixtureResolver(session_factory).resolve(str(instance_id))


def test_resolver_rejects_persistent_fixture_even_if_state_was_seeded(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, _revision_id = seed(
        session_factory, instance_type=InstanceType.PERSISTENT
    )
    with pytest.raises(PersistentProvisioningUnsupportedError):
        SqliteAssignedFixtureResolver(session_factory).resolve(str(instance_id))

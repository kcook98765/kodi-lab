from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    ConnectionState,
    FixtureContentCategory,
    FixtureDefinition,
    FixtureSizeCategory,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)


def test_movies_fixture_definition_persists_and_reloads(db_session: Session) -> None:
    fixture = FixtureDefinition(
        id=uuid4(),
        slug="test-small-movies-v1",
        fixture_version=1,
        size_category=FixtureSizeCategory.SMALL,
        content_category=FixtureContentCategory.MOVIES,
        content_digest="sha256:" + "a" * 64,
        compatible_kodi_version="21.3",
        description="Repository round-trip for Movies taxonomy.",
    )
    repository = FixtureDefinitionRepository(db_session)
    repository.create(fixture)
    db_session.commit()
    db_session.expire_all()

    assert repository.get(fixture.id) == fixture


def test_tv_fixture_definition_persists_and_reloads(db_session: Session) -> None:
    fixture = FixtureDefinition(
        id=uuid4(),
        slug="test-small-tv-v1",
        fixture_version=1,
        size_category=FixtureSizeCategory.SMALL,
        content_category=FixtureContentCategory.TV,
        content_digest="sha256:" + "b" * 64,
        compatible_kodi_version="21.3",
        description="Repository round-trip for TV taxonomy.",
    )
    repository = FixtureDefinitionRepository(db_session)
    repository.create(fixture)
    db_session.commit()
    db_session.expire_all()

    assert repository.get(fixture.id) == fixture


def make_target(name: str = "Kodi 21") -> Target:
    return Target(
        id=uuid4(),
        name=name,
        target_type=TargetType.MANAGED,
        kodi_version="21.3",
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )


def make_runtime(db_session: Session) -> RuntimeRevision:
    definition = RuntimeDefinition(
        id=uuid4(), slug=f"kodi-{uuid4().hex}", kodi_version="21.3"
    )
    revision = RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=1,
        source_kind="official-package",
        source_reference="ppa:team-xbmc/ppa@21.3",
        content_digest="a" * 64,
        manifest_reference="catalog/kodi-21.3.yaml",
        manifest_digest="b" * 64,
        built_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    RuntimeDefinitionRepository(db_session).create(definition)
    RuntimeRevisionRepository(db_session).create(revision)
    return revision


def make_instance(target: Target, revision: RuntimeRevision) -> ManagedInstance:
    return ManagedInstance(
        target_id=target.id,
        instance_type=InstanceType.PERSISTENT,
        runtime_revision_id=revision.id,
        runtime_state=InstanceRuntimeState.STOPPED,
        notes="development fixture",
    )


def test_sqlite_foreign_keys_are_enabled(db_session: Session) -> None:
    enabled = db_session.scalar(text("PRAGMA foreign_keys"))

    assert enabled == 1


def test_target_and_managed_instance_round_trip(db_session: Session) -> None:
    target = make_target()
    revision = make_runtime(db_session)
    instance = make_instance(target, revision)
    targets = TargetRepository(db_session)
    instances = ManagedInstanceRepository(db_session)

    targets.create(target)
    instances.create(instance)
    db_session.commit()
    db_session.expire_all()

    stored_target = targets.get(target.id)
    stored_instance = instances.get(target.id)
    assert stored_target == target
    assert stored_instance == instance
    assert stored_target is not None
    assert stored_instance is not None
    assert stored_instance.target_id == stored_target.id


def test_lists_have_predictable_order(db_session: Session) -> None:
    target_b = make_target("Beta")
    target_a = make_target("Alpha")
    revision = make_runtime(db_session)
    targets = TargetRepository(db_session)
    instances = ManagedInstanceRepository(db_session)
    for target in (target_b, target_a):
        targets.create(target)
        instances.create(make_instance(target, revision))

    assert targets.list() == [target_a, target_b]
    assert [item.target_id for item in instances.list()] == sorted(
        (target_a.id, target_b.id), key=str
    )


def test_updates_preserve_identity(db_session: Session) -> None:
    target = make_target()
    first_revision = make_runtime(db_session)
    definition = RuntimeDefinitionRepository(db_session).get(
        first_revision.runtime_definition_id
    )
    assert definition is not None
    second_revision = replace(
        first_revision,
        id=uuid4(),
        revision_number=2,
        content_digest="c" * 64,
    )
    RuntimeRevisionRepository(db_session).create(second_revision)
    instance = make_instance(target, first_revision)
    targets = TargetRepository(db_session)
    instances = ManagedInstanceRepository(db_session)
    targets.create(target)
    instances.create(instance)

    updated_target = replace(
        target,
        name="Renamed target",
        availability=TargetAvailability.AVAILABLE,
        health=HealthStatus.HEALTHY,
        connection_state=ConnectionState.CONNECTED,
    )
    updated_instance = replace(
        instance,
        runtime_revision_id=second_revision.id,
        runtime_state=InstanceRuntimeState.RUNNING,
        notes=None,
    )
    targets.update(updated_target)
    instances.update(updated_instance)
    db_session.commit()

    assert targets.get(target.id) == updated_target
    assert instances.get(target.id) == updated_instance


def test_duplicate_target_identity_is_rejected_by_database(
    db_session: Session,
) -> None:
    target = make_target()
    repository = TargetRepository(db_session)
    repository.create(target)

    with pytest.raises(IntegrityError):
        repository.create(target)


def test_duplicate_managed_instance_identity_is_rejected_by_database(
    db_session: Session,
) -> None:
    target = make_target()
    revision = make_runtime(db_session)
    targets = TargetRepository(db_session)
    instances = ManagedInstanceRepository(db_session)
    targets.create(target)
    instance = make_instance(target, revision)
    instances.create(instance)

    with pytest.raises(IntegrityError):
        instances.create(instance)


def test_invalid_managed_instance_revision_foreign_key_is_rejected(
    db_session: Session,
) -> None:
    target = make_target()
    TargetRepository(db_session).create(target)
    instance = ManagedInstance(
        target_id=target.id,
        instance_type=InstanceType.DISPOSABLE,
        runtime_revision_id=uuid4(),
        runtime_state=InstanceRuntimeState.STOPPED,
    )

    with pytest.raises(IntegrityError):
        ManagedInstanceRepository(db_session).create(instance)


def test_required_target_field_is_enforced_by_database(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version,
                    availability, health, connection_state
                ) VALUES (
                    :id, NULL, 'managed', '21.3',
                    'unavailable', 'warning', 'disconnected'
                )
                """
            ),
            {"id": str(uuid4())},
        )


def test_enum_value_is_enforced_by_database(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version,
                    availability, health, connection_state
                ) VALUES (
                    :id, 'target', 'managed', '21.3',
                    'unavailable', 'unknown', 'disconnected'
                )
                """
            ),
            {"id": str(uuid4())},
        )


def test_blank_target_name_is_enforced_by_database(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version,
                    availability, health, connection_state
                ) VALUES (
                    :id, '  ', 'managed', '21.3',
                    'unavailable', 'warning', 'disconnected'
                )
                """
            ),
            {"id": str(uuid4())},
        )


def test_deleting_target_cascades_to_managed_instance(db_session: Session) -> None:
    target = make_target()
    revision = make_runtime(db_session)
    targets = TargetRepository(db_session)
    instances = ManagedInstanceRepository(db_session)
    targets.create(target)
    instances.create(make_instance(target, revision))
    db_session.commit()

    assert targets.delete(target.id) is True
    db_session.commit()
    db_session.expire_all()

    assert targets.get(target.id) is None
    assert instances.get(target.id) is None

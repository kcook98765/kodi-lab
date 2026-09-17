from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import (
    FixtureIncompatibleError,
    PersistentFixtureUnsupportedError,
)
from kodi_lab.application.fixtures import FixtureAdministrationService
from kodi_lab.application.managed_instances import (
    ExactRevisionSelection,
    ManagedInstanceService,
)
from kodi_lab.application.reset_operations import (
    ResetOperationConflictError,
    ResetOperationCoordinator,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    FixtureDefinition,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    ProcessObservationKind,
    ReadinessProbeKind,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeKind,
    RuntimeRevision,
    RuntimeValidationEvidence,
    RuntimeValidationResult,
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
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'fixtures.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def seed_instance(
    factory: sessionmaker[Session],
    *,
    instance_type: InstanceType = InstanceType.DISPOSABLE,
    kodi_version: str = "21.3",
) -> tuple[Target, RuntimeDefinition, RuntimeRevision]:
    target = Target(
        id=uuid4(),
        name="Fixture target",
        target_type=TargetType.MANAGED,
        kodi_version=kodi_version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )
    definition = RuntimeDefinition(
        id=uuid4(),
        slug=f"kodi-{kodi_version.replace('.', '-')}",
        kodi_version=kodi_version,
    )
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
    with factory.begin() as session:
        TargetRepository(session).create(target)
        RuntimeDefinitionRepository(session).create(definition)
        RuntimeRevisionRepository(session).create(revision)
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(revision.id, "sha256:" + ("c" * 64))
        )
        RuntimeExecutionProfileRepository(session).create(
            RuntimeExecutionProfile(
                runtime_revision_id=revision.id,
                runtime_kind=RuntimeKind.KODI,
                runtime_uid=1000,
                runtime_gid=1000,
                expected_application_name="Kodi",
                expected_kodi_version=kodi_version,
                expected_version_tag="stable",
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
                readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
                readiness_timeout_seconds=90,
                poll_interval_seconds=1,
                http_timeout_seconds=3,
            )
        )
        RuntimeValidationEvidenceRepository(session).create(
            RuntimeValidationEvidence(
                id=uuid4(),
                runtime_revision_id=revision.id,
                contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
                result=RuntimeValidationResult.PASSED,
                started_at=NOW,
                completed_at=NOW,
                evidence_reference="test://fixture",
                recorded_at=NOW,
            )
        )
        ManagedInstanceRepository(session).create(
            ManagedInstance(
                target_id=target.id,
                instance_type=instance_type,
                runtime_revision_id=revision.id,
                runtime_state=InstanceRuntimeState.STOPPED,
            )
        )
    return target, definition, revision


def test_register_assign_unassign_preserves_exact_revision(
    session_factory: sessionmaker[Session],
) -> None:
    target, _definition, revision = seed_instance(session_factory)
    service = FixtureAdministrationService(session_factory)
    fixture = service.register_packaged_fixture("small-mixed-v1")
    assert service.register_packaged_fixture("small-mixed-v1") == fixture

    assigned = service.assign(
        managed_instance_id=target.id, fixture_definition_id=fixture.id
    )
    assert assigned.fixture_definition_id == fixture.id
    assert assigned.runtime_revision_id == revision.id

    unassigned = service.unassign(managed_instance_id=target.id)
    assert unassigned.fixture_definition_id is None
    assert unassigned.runtime_revision_id == revision.id


def test_v1_and_v2_register_distinctly_and_v2_assignment_preserves_pin(
    session_factory: sessionmaker[Session],
) -> None:
    target, _definition, revision = seed_instance(session_factory)
    service = FixtureAdministrationService(session_factory)
    v1 = service.register_packaged_fixture("small-mixed-v1")
    v2 = service.register_packaged_fixture("small-mixed-v2")

    assert v1.id != v2.id
    assert v1.fixture_version == 1
    assert v2.fixture_version == 2
    assigned = service.assign(
        managed_instance_id=target.id, fixture_definition_id=v2.id
    )
    assert assigned.fixture_definition_id == v2.id
    assert assigned.runtime_revision_id == revision.id


def test_v2_assignment_rejects_non_omega_runtime(
    session_factory: sessionmaker[Session],
) -> None:
    target, _definition, _revision = seed_instance(session_factory, kodi_version="20.5")
    service = FixtureAdministrationService(session_factory)
    v2 = service.register_packaged_fixture("small-mixed-v2")
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=target.id, fixture_definition_id=v2.id)


def test_small_movies_registration_and_exact_release_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    omega, _definition, revision = seed_instance(session_factory)
    service = FixtureAdministrationService(session_factory)
    fixture = service.register_packaged_fixture("small-movies-v1")
    assigned = service.assign(
        managed_instance_id=omega.id, fixture_definition_id=fixture.id
    )

    assert fixture.content_category.value == "movies"
    assert assigned.runtime_revision_id == revision.id
    assert assigned.fixture_definition_id == fixture.id

    nexus, _definition, _revision = seed_instance(session_factory, kodi_version="20.5")
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=nexus.id, fixture_definition_id=fixture.id)


def test_small_tv_registration_and_exact_release_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    omega, _definition, revision = seed_instance(session_factory)
    service = FixtureAdministrationService(session_factory)
    fixture = service.register_packaged_fixture("small-tv-v1")
    assigned = service.assign(
        managed_instance_id=omega.id, fixture_definition_id=fixture.id
    )

    assert fixture.content_category.value == "tv"
    assert assigned.runtime_revision_id == revision.id
    assert assigned.fixture_definition_id == fixture.id

    nexus, _definition, _revision = seed_instance(session_factory, kodi_version="20.5")
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=nexus.id, fixture_definition_id=fixture.id)


def test_v2_v3_v4_exact_cross_version_assignment_matrix(
    session_factory: sessionmaker[Session],
) -> None:
    nexus, _definition, nexus_revision = seed_instance(
        session_factory, kodi_version="20.5"
    )
    service = FixtureAdministrationService(session_factory)
    v2 = service.register_packaged_fixture("small-mixed-v2")
    v3 = service.register_packaged_fixture("small-mixed-v3")
    v4 = service.register_packaged_fixture("small-mixed-v4")

    assigned = service.assign(managed_instance_id=nexus.id, fixture_definition_id=v3.id)
    assert assigned.fixture_definition_id == v3.id
    assert assigned.runtime_revision_id == nexus_revision.id
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=nexus.id, fixture_definition_id=v2.id)
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=nexus.id, fixture_definition_id=v4.id)

    omega, _definition, omega_revision = seed_instance(session_factory)
    assigned = service.assign(managed_instance_id=omega.id, fixture_definition_id=v2.id)
    assert assigned.fixture_definition_id == v2.id
    assert assigned.runtime_revision_id == omega_revision.id
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=omega.id, fixture_definition_id=v3.id)
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=omega.id, fixture_definition_id=v4.id)

    matrix, _definition, matrix_revision = seed_instance(
        session_factory, kodi_version="19.5"
    )
    assigned = service.assign(
        managed_instance_id=matrix.id, fixture_definition_id=v4.id
    )
    assert assigned.fixture_definition_id == v4.id
    assert assigned.runtime_revision_id == matrix_revision.id
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=matrix.id, fixture_definition_id=v2.id)
    with pytest.raises(FixtureIncompatibleError):
        service.assign(managed_instance_id=matrix.id, fixture_definition_id=v3.id)


@pytest.mark.parametrize(
    "slugs",
    [
        ("small-movies-v3", "small-movies-v2", "small-movies-v1"),
        ("small-tv-v3", "small-tv-v2", "small-tv-v1"),
    ],
)
def test_family_variants_enforce_exact_stable_release_compatibility(
    session_factory: sessionmaker[Session], slugs: tuple[str, str, str]
) -> None:
    service = FixtureAdministrationService(session_factory)
    fixtures = tuple(service.register_packaged_fixture(slug) for slug in slugs)
    instances = (
        seed_instance(session_factory, kodi_version="19.5"),
        seed_instance(session_factory, kodi_version="20.5"),
        seed_instance(session_factory, kodi_version="21.3"),
    )

    for index, (target, _definition, revision) in enumerate(instances):
        for fixture_index, fixture in enumerate(fixtures):
            if fixture_index == index:
                assigned = service.assign(
                    managed_instance_id=target.id,
                    fixture_definition_id=fixture.id,
                )
                assert assigned.runtime_revision_id == revision.id
                assert assigned.fixture_definition_id == fixture.id
            else:
                with pytest.raises(FixtureIncompatibleError):
                    service.assign(
                        managed_instance_id=target.id,
                        fixture_definition_id=fixture.id,
                    )


def test_medium_fixture_assignment_is_exactly_kodi_21_3_compatible(
    session_factory: sessionmaker[Session],
) -> None:
    service = FixtureAdministrationService(session_factory)
    fixture = service.register_packaged_fixture("medium-mixed-v1")
    instances = (
        seed_instance(session_factory, kodi_version="19.5"),
        seed_instance(session_factory, kodi_version="20.5"),
        seed_instance(session_factory, kodi_version="21.3"),
    )

    for instance, definition, revision in instances:
        if definition.kodi_release.canonical == "21.3":
            assigned = service.assign(
                managed_instance_id=instance.id,
                fixture_definition_id=fixture.id,
            )
            assert assigned.runtime_revision_id == revision.id
            assert assigned.fixture_definition_id == fixture.id
        else:
            with pytest.raises(FixtureIncompatibleError):
                service.assign(
                    managed_instance_id=instance.id,
                    fixture_definition_id=fixture.id,
                )


def test_assignment_rejects_persistent_and_incompatible_instances(
    session_factory: sessionmaker[Session],
) -> None:
    service = FixtureAdministrationService(session_factory)
    fixture = service.register_packaged_fixture("small-mixed-v1")
    persistent, _definition, _revision = seed_instance(
        session_factory, instance_type=InstanceType.PERSISTENT
    )
    with pytest.raises(PersistentFixtureUnsupportedError):
        service.assign(
            managed_instance_id=persistent.id, fixture_definition_id=fixture.id
        )

    incompatible, _definition, _revision = seed_instance(
        session_factory, kodi_version="20.5"
    )
    with pytest.raises(FixtureIncompatibleError):
        service.assign(
            managed_instance_id=incompatible.id, fixture_definition_id=fixture.id
        )


def test_revision_reassignment_rejects_existing_incompatible_fixture(
    session_factory: sessionmaker[Session],
) -> None:
    target, definition, revision = seed_instance(session_factory)
    packaged = load_packaged_fixture("small-mixed-v1").definition
    incompatible = FixtureDefinition(
        id=uuid4(),
        slug="test-incompatible-v1",
        fixture_version=1,
        size_category=packaged.size_category,
        content_category=packaged.content_category,
        content_digest="d" * 64,
        compatible_kodi_version="20.5",
        description="Deliberately incompatible repository test fixture.",
    )
    with session_factory.begin() as session:
        FixtureDefinitionRepository(session).create(incompatible)
        instances = ManagedInstanceRepository(session)
        current = instances.get(target.id)
        assert current is not None
        instances.update(
            ManagedInstance(
                target_id=current.target_id,
                instance_type=current.instance_type,
                runtime_revision_id=current.runtime_revision_id,
                runtime_state=current.runtime_state,
                fixture_definition_id=incompatible.id,
            )
        )
    assert revision.runtime_definition_id == definition.id
    with pytest.raises(FixtureIncompatibleError):
        ManagedInstanceService(session_factory).reassign_runtime(
            target_id=target.id,
            selection=ExactRevisionSelection(
                runtime_definition_id=definition.id,
                runtime_revision_id=revision.id,
            ),
        )


def test_active_reset_blocks_fixture_and_revision_administration(
    session_factory: sessionmaker[Session],
) -> None:
    target, definition, revision = seed_instance(session_factory)
    coordinator = ResetOperationCoordinator(session_factory)
    fixture_service = FixtureAdministrationService(
        session_factory, reset_operations=coordinator
    )
    fixture = fixture_service.register_packaged_fixture("small-mixed-v2")
    coordinator.queue(target.id)

    with pytest.raises(ResetOperationConflictError):
        fixture_service.assign(
            managed_instance_id=target.id, fixture_definition_id=fixture.id
        )
    with pytest.raises(ResetOperationConflictError):
        fixture_service.unassign(managed_instance_id=target.id)
    with pytest.raises(ResetOperationConflictError):
        ManagedInstanceService(
            session_factory, reset_operations=coordinator
        ).reassign_runtime(
            target_id=target.id,
            selection=ExactRevisionSelection(definition.id, revision.id),
        )
    active = coordinator.active_for_instance(target.id)
    assert active is not None
    lease = coordinator.acquire(active.id)
    assert lease is not None
    coordinator.finish_success(lease)
    assert (
        fixture_service.assign(
            managed_instance_id=target.id, fixture_definition_id=fixture.id
        ).fixture_definition_id
        == fixture.id
    )

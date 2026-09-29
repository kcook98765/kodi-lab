from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    MatrixAvailability,
    MatrixIneligibilityReason,
    RuntimeMatrixResponse,
    RuntimeMatrixService,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeKind,
    RuntimeRevision,
    RuntimeSupportTier,
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

NOW = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'matrix.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def add_definition(
    session: Session,
    version: str,
    tier: RuntimeSupportTier,
    *,
    requires_trust: bool = False,
    validation: RuntimeValidationResult | None = RuntimeValidationResult.PASSED,
) -> tuple[RuntimeDefinition, RuntimeRevision, Target]:
    definition = RuntimeDefinition(
        id=uuid4(),
        slug=f"kodi-{version.replace('.', '-')}-{uuid4().hex[:6]}",
        kodi_version=version,
        support_tier=tier,
    )
    target = Target(
        id=uuid4(),
        name=f"Kodi {version} target",
        target_type=TargetType.MANAGED,
        kodi_version=version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )
    RuntimeDefinitionRepository(session).create(definition)
    TargetRepository(session).create(target)
    revision = add_revision(
        session,
        definition,
        1,
        requires_trust=requires_trust,
        validation=validation,
    )
    return definition, revision, target


def add_revision(
    session: Session,
    definition: RuntimeDefinition,
    number: int,
    *,
    approved: bool = True,
    requires_trust: bool = False,
    validation: RuntimeValidationResult | None = RuntimeValidationResult.PASSED,
) -> RuntimeRevision:
    revision = RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=number,
        source_kind="test-source",
        source_reference=f"test://{definition.slug}/{number}",
        content_digest=f"{number:064x}",
        manifest_reference=f"runtime/{definition.slug}/manifest.json",
        manifest_digest="f" * 64,
        built_at=NOW + timedelta(seconds=number),
        approval_state=(
            RevisionApprovalState.APPROVED
            if approved
            else RevisionApprovalState.NOT_APPROVED
        ),
        approved_at=NOW if approved else None,
        requires_trusted_build_confirmation=requires_trust,
        trusted_build_confirmed_at=NOW if requires_trust else None,
    )
    RuntimeRevisionRepository(session).create(revision)
    RuntimeContainerArtifactRepository(session).create(
        RuntimeContainerArtifact(
            runtime_revision_id=revision.id,
            image_id="sha256:" + revision.id.hex * 2,
        )
    )
    release = definition.kodi_release
    RuntimeExecutionProfileRepository(session).create(
        RuntimeExecutionProfile(
            runtime_revision_id=revision.id,
            runtime_kind=RuntimeKind.KODI,
            runtime_uid=1000,
            runtime_gid=1000,
            python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
            expected_application_name="Kodi",
            expected_kodi_version=f"{release.major}.{release.minor}",
            expected_version_tag=release.channel.value,
            expected_prerelease_ordinal=release.prerelease_ordinal,
            jsonrpc_port=8080,
            jsonrpc_path="/jsonrpc",
            process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
            readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
            readiness_timeout_seconds=90,
            poll_interval_seconds=1,
            http_timeout_seconds=3,
        )
    )
    if validation is not None:
        RuntimeValidationEvidenceRepository(session).create(
            RuntimeValidationEvidence(
                id=uuid4(),
                runtime_revision_id=revision.id,
                contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
                result=validation,
                started_at=NOW,
                completed_at=NOW,
                evidence_reference=f"test://validation/{revision.id}",
                recorded_at=NOW + timedelta(seconds=number),
            )
        )
    return revision


def seed_curated_matrix(
    factory: sessionmaker[Session],
) -> dict[str, tuple[RuntimeDefinition, RuntimeRevision, Target]]:
    with factory.begin() as session:
        return {
            "19.5": add_definition(session, "19.5", RuntimeSupportTier.LEGACY),
            "20.5": add_definition(session, "20.5", RuntimeSupportTier.CURRENT),
            "21.3": add_definition(session, "21.3", RuntimeSupportTier.CURRENT),
            "22.0-beta.2": add_definition(
                session,
                "22.0-beta.2",
                RuntimeSupportTier.PREVIEW,
                requires_trust=True,
            ),
        }


def selectable(response: RuntimeMatrixResponse) -> dict[str, bool]:
    return {
        item.canonical_release: item.selectable
        for item in response.runtimes
        if item.supported_generation is None
    }


def test_curated_matrix_policy_combinations(factory: sessionmaker[Session]) -> None:
    seed_curated_matrix(factory)
    trust_off = RuntimeMatrixService(factory)
    trust_on = RuntimeMatrixService(factory, trusted_custom_builds_enabled=True)

    assert selectable(trust_off.list_matrix()) == {
        "19.5": False,
        "20.5": True,
        "21.3": True,
        "22.0-beta.2": False,
    }
    assert selectable(trust_off.list_matrix(include_legacy=True)) == {
        "19.5": True,
        "20.5": True,
        "21.3": True,
        "22.0-beta.2": False,
    }
    prerelease = trust_off.list_matrix(include_prerelease=True)
    assert selectable(prerelease) == {
        "19.5": False,
        "20.5": True,
        "21.3": True,
        "22.0-beta.2": False,
    }
    beta = prerelease.runtimes[-1]
    assert beta.ineligibility_reason is (
        MatrixIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED
    )
    assert selectable(trust_on.list_matrix(include_prerelease=True)) == {
        "19.5": False,
        "20.5": True,
        "21.3": True,
        "22.0-beta.2": True,
    }
    assert all(
        selectable(
            trust_on.list_matrix(include_legacy=True, include_prerelease=True)
        ).values()
    )


def test_legacy_fixture_availability_does_not_bypass_include_legacy(
    factory: sessionmaker[Session],
) -> None:
    items = seed_curated_matrix(factory)
    fixtures = tuple(
        load_packaged_fixture(slug).definition
        for slug in ("small-mixed-v4", "small-movies-v3", "small-tv-v3")
    )
    with factory.begin() as session:
        repository = FixtureDefinitionRepository(session)
        for fixture in fixtures:
            repository.create(fixture)

    service = RuntimeMatrixService(factory)
    ordinary = service.resolve_target(items["19.5"][2].id)
    opted_in = service.resolve_target(items["19.5"][2].id, include_legacy=True)

    assert not ordinary.selectable
    assert ordinary.ineligibility_reason is (
        MatrixIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED
    )
    assert opted_in.selectable
    with factory() as session:
        repository = FixtureDefinitionRepository(session)
        assert all(repository.get(fixture.id) == fixture for fixture in fixtures)


def test_matrix_dto_is_semantic_and_preserves_beta_ordinal(
    factory: sessionmaker[Session],
) -> None:
    seed_curated_matrix(factory)
    response = RuntimeMatrixService(factory).list_matrix()
    beta = response.runtimes[-1]
    assert beta.canonical_release == "22.0-beta.2"
    assert beta.display_version == "Kodi 22.0 Beta 2"
    assert beta.prerelease_ordinal == 2
    assert beta.availability is MatrixAvailability.OPT_IN_REQUIRED
    assert beta.availability_detail == ("Preview runtime; enable Include Pre-release.")
    document = response.model_dump(mode="json")
    serialized = str(document)
    for forbidden in ("image_id", "artifact", "evidence_reference", "source_reference"):
        assert forbidden not in serialized


def test_target_resolution_is_exact_and_has_no_cross_version_fallback(
    factory: sessionmaker[Session],
) -> None:
    items = seed_curated_matrix(factory)
    service = RuntimeMatrixService(factory, trusted_custom_builds_enabled=True)
    target_19 = items["19.5"][2]
    target_beta2 = items["22.0-beta.2"][2]

    assert service.resolve_target(target_19.id).selected_revision_number is None
    selected_19 = service.resolve_target(target_19.id, include_legacy=True)
    assert selected_19.selectable
    assert selected_19.required_canonical_release == "19.5"
    assert selected_19.runtime_definition_id == items["19.5"][0].id

    assert not service.resolve_target(target_beta2.id).selectable
    selected_beta2 = service.resolve_target(target_beta2.id, include_prerelease=True)
    assert selected_beta2.selectable
    assert selected_beta2.runtime_definition_id == items["22.0-beta.2"][0].id

    with factory.begin() as session:
        beta1 = Target(
            id=uuid4(),
            name="Beta 1 target",
            target_type=TargetType.MANAGED,
            kodi_version="22.0-beta.1",
            availability=TargetAvailability.UNAVAILABLE,
            health=HealthStatus.WARNING,
            connection_state=ConnectionState.DISCONNECTED,
        )
        TargetRepository(session).create(beta1)
    unresolved = service.resolve_target(beta1.id, include_prerelease=True)
    assert not unresolved.selectable
    assert unresolved.runtime_definition_id is None
    assert unresolved.ineligibility_reason is (
        MatrixIneligibilityReason.RUNTIME_DEFINITION_UNAVAILABLE
    )


def test_newest_eligible_revision_drives_presentation(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        definition, first, _target = add_definition(
            session, "18.9", RuntimeSupportTier.CURRENT
        )
        add_revision(
            session,
            definition,
            2,
            validation=RuntimeValidationResult.FAILED,
        )
    entry = RuntimeMatrixService(factory).list_matrix().runtimes[0]
    assert entry.newest_revision_number == 2
    assert entry.selected_revision_number == first.revision_number
    assert entry.effective_validation_state == "validated"
    assert entry.selectable


@pytest.mark.parametrize(
    ("version", "validation", "reason"),
    [
        ("18.7", None, MatrixIneligibilityReason.UNVALIDATED),
        (
            "18.8",
            RuntimeValidationResult.FAILED,
            MatrixIneligibilityReason.VALIDATION_FAILED,
        ),
    ],
)
def test_unvalidated_and_failed_current_definitions_are_unavailable(
    factory: sessionmaker[Session],
    version: str,
    validation: RuntimeValidationResult | None,
    reason: MatrixIneligibilityReason,
) -> None:
    with factory.begin() as session:
        add_definition(
            session,
            version,
            RuntimeSupportTier.CURRENT,
            validation=validation,
        )
    entry = RuntimeMatrixService(factory).list_matrix().runtimes[0]
    assert not entry.selectable
    assert entry.ineligibility_reason is reason
    assert entry.availability is MatrixAvailability.UNAVAILABLE


def test_retired_definition_remains_historical_with_existing_pin(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        _definition, revision, target = add_definition(
            session, "18.6", RuntimeSupportTier.RETIRED
        )
        ManagedInstanceRepository(session).create(
            ManagedInstance(
                target_id=target.id,
                instance_type=InstanceType.DISPOSABLE,
                runtime_revision_id=revision.id,
                runtime_state=InstanceRuntimeState.STOPPED,
            )
        )
    entry = (
        RuntimeMatrixService(factory, trusted_custom_builds_enabled=True)
        .list_matrix(include_legacy=True, include_prerelease=True)
        .runtimes[0]
    )
    assert not entry.selectable
    assert entry.availability is MatrixAvailability.HISTORICAL_ONLY
    assert entry.pinned_instance_count == 1
    assert entry.availability_detail == "Runtime is retired and historical only."

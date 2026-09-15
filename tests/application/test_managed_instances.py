from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    ExactRevisionSelection,
    KodiVersionIncompatibleError,
    ManagedInstanceConflictError,
    ManagedInstanceNotFoundError,
    ManagedInstanceService,
    NewestApprovedRevisionSelection,
    NoApprovedRevisionError,
    RevisionNotApprovedError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionDefinitionMismatchError,
    RuntimeRevisionNotFoundError,
    TargetNotFoundError,
    TrustedCustomBuildConfirmationRequiredError,
    TrustedCustomBuildDisabledError,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
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
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'application.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)

    yield factory

    engine.dispose()


def make_target(*, kodi_version: str = "21.3") -> Target:
    return Target(
        id=uuid4(),
        name=f"Target {uuid4().hex[:8]}",
        target_type=TargetType.MANAGED,
        kodi_version=kodi_version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )


def make_definition(*, kodi_version: str = "21.3") -> RuntimeDefinition:
    return RuntimeDefinition(
        id=uuid4(),
        slug=f"kodi-{uuid4().hex}",
        kodi_version=kodi_version,
    )


def make_revision(
    definition: RuntimeDefinition,
    number: int,
    *,
    approved: bool = True,
    requires_trust: bool = False,
    confirmed: bool = False,
) -> RuntimeRevision:
    return RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=number,
        source_kind="official-package",
        source_reference=f"ppa:team-xbmc/ppa@{definition.kodi_version}-r{number}",
        content_digest=f"{number:064x}",
        manifest_reference=f"catalog/{definition.slug}.yaml",
        manifest_digest="f" * 64,
        built_at=NOW,
        approval_state=(
            RevisionApprovalState.APPROVED
            if approved
            else RevisionApprovalState.NOT_APPROVED
        ),
        approved_at=NOW if approved else None,
        requires_trusted_build_confirmation=requires_trust,
        trusted_build_confirmed_at=NOW if confirmed else None,
    )


def seed(
    factory: sessionmaker[Session],
    *,
    target: Target | None = None,
    definition: RuntimeDefinition | None = None,
    revisions: tuple[RuntimeRevision, ...] = (),
) -> None:
    with factory.begin() as session:
        if target is not None:
            TargetRepository(session).create(target)
        if definition is not None:
            RuntimeDefinitionRepository(session).create(definition)
        repository = RuntimeRevisionRepository(session)
        for revision in revisions:
            repository.create(revision)
            RuntimeContainerArtifactRepository(session).create(
                RuntimeContainerArtifact(
                    runtime_revision_id=revision.id,
                    image_id="sha256:" + f"{revision.revision_number:064x}",
                )
            )
            assert definition is not None
            release = definition.kodi_release
            RuntimeExecutionProfileRepository(session).create(
                RuntimeExecutionProfile(
                    runtime_revision_id=revision.id,
                    runtime_kind=RuntimeKind.KODI,
                    expected_application_name="Kodi",
                    expected_kodi_version=f"{release.major}.{release.minor}",
                    expected_version_tag=release.channel.value,
                    expected_prerelease_ordinal=release.prerelease_ordinal,
                    jsonrpc_port=8080,
                    jsonrpc_path="/jsonrpc",
                    process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
                    readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
                    readiness_timeout_seconds=90,
                    poll_interval_seconds=1.0,
                    http_timeout_seconds=3.0,
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
                    evidence_reference=f"test://revision/{revision.id}",
                    recorded_at=NOW,
                )
            )


def exact(
    definition: RuntimeDefinition, revision: RuntimeRevision
) -> ExactRevisionSelection:
    return ExactRevisionSelection(definition.id, revision.id)


def load_instance(
    factory: sessionmaker[Session], target_id: UUID
) -> ManagedInstance | None:
    with factory() as session:
        return ManagedInstanceRepository(session).get(target_id)


def test_create_with_exact_approved_revision(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    created = ManagedInstanceService(session_factory).create(
        target_id=target.id,
        selection=exact(definition, revision),
        instance_type=InstanceType.PERSISTENT,
        notes="preserve me",
    )

    assert created.runtime_revision_id == revision.id
    assert created.runtime_state is InstanceRuntimeState.STOPPED
    assert load_instance(session_factory, target.id) == created


def test_create_with_newest_approved_uses_greatest_revision_number(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision_1 = make_revision(definition, 1)
    revision_3 = make_revision(definition, 3)
    revision_4 = make_revision(definition, 4, approved=False)
    seed(
        session_factory,
        target=target,
        definition=definition,
        revisions=(revision_4, revision_1, revision_3),
    )

    created = ManagedInstanceService(session_factory).create(
        target_id=target.id,
        selection=NewestApprovedRevisionSelection(definition.id),
        instance_type=InstanceType.DISPOSABLE,
    )

    assert created.runtime_revision_id == revision_3.id


def test_reassign_preserves_identity_and_unrelated_fields(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision_1 = make_revision(definition, 1)
    revision_2 = make_revision(definition, 2)
    seed(
        session_factory,
        target=target,
        definition=definition,
        revisions=(revision_1, revision_2),
    )
    service = ManagedInstanceService(session_factory)
    original = service.create(
        target_id=target.id,
        selection=exact(definition, revision_1),
        instance_type=InstanceType.PERSISTENT,
        start_on_host_boot=True,
        notes="unchanged",
    )

    updated = service.reassign_runtime(
        target_id=target.id,
        selection=exact(definition, revision_2),
    )

    assert updated == ManagedInstance(
        target_id=original.target_id,
        instance_type=original.instance_type,
        runtime_revision_id=revision_2.id,
        runtime_state=original.runtime_state,
        start_on_host_boot=original.start_on_host_boot,
        notes=original.notes,
    )
    assert load_instance(session_factory, target.id) == updated


def test_exact_unapproved_revision_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, approved=False)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(RevisionNotApprovedError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=exact(definition, revision),
            instance_type=InstanceType.PERSISTENT,
        )

    assert load_instance(session_factory, target.id) is None


def test_no_approved_revision_is_typed_error(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, approved=False)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(NoApprovedRevisionError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=NewestApprovedRevisionSelection(definition.id),
            instance_type=InstanceType.PERSISTENT,
        )


def test_approval_makes_revision_selectable(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, approved=False)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))
    with session_factory.begin() as session:
        RuntimeRevisionRepository(session).approve(revision.id, approved_at=NOW)

    created = ManagedInstanceService(session_factory).create(
        target_id=target.id,
        selection=exact(definition, revision),
        instance_type=InstanceType.PERSISTENT,
    )

    assert created.runtime_revision_id == revision.id


def test_trusted_build_requires_global_opt_in(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, requires_trust=True, confirmed=True)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(TrustedCustomBuildDisabledError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=exact(definition, revision),
            instance_type=InstanceType.PERSISTENT,
        )


def test_trusted_build_requires_prior_confirmation(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, requires_trust=True)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(TrustedCustomBuildConfirmationRequiredError):
        ManagedInstanceService(
            session_factory, trusted_custom_builds_enabled=True
        ).create(
            target_id=target.id,
            selection=exact(definition, revision),
            instance_type=InstanceType.PERSISTENT,
        )


def test_approval_and_trust_confirmation_together_permit_selection(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1, requires_trust=True, confirmed=True)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    created = ManagedInstanceService(
        session_factory, trusted_custom_builds_enabled=True
    ).create(
        target_id=target.id,
        selection=exact(definition, revision),
        instance_type=InstanceType.PERSISTENT,
    )

    assert created.runtime_revision_id == revision.id


def test_incompatible_kodi_version_rolls_back_creation(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target(kodi_version="21.3")
    definition = make_definition(kodi_version="20.5")
    revision = make_revision(definition, 1)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(KodiVersionIncompatibleError) as raised:
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=exact(definition, revision),
            instance_type=InstanceType.PERSISTENT,
        )

    assert raised.value.target_kodi_version == "21.3"
    assert raised.value.runtime_kodi_version == "20.5"
    assert load_instance(session_factory, target.id) is None


def test_kodi_version_comparison_ignores_surrounding_whitespace(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target(kodi_version=" 21.3")
    definition = make_definition(kodi_version="21.3 ")
    revision = make_revision(definition, 1)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    created = ManagedInstanceService(session_factory).create(
        target_id=target.id,
        selection=exact(definition, revision),
        instance_type=InstanceType.PERSISTENT,
    )

    assert created.runtime_revision_id == revision.id


def test_failed_incompatible_reassignment_preserves_prior_revision(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    compatible = make_definition()
    incompatible = make_definition(kodi_version="20.5")
    first = make_revision(compatible, 1)
    second = make_revision(incompatible, 1)
    seed(session_factory, target=target, definition=compatible, revisions=(first,))
    seed(session_factory, definition=incompatible, revisions=(second,))
    service = ManagedInstanceService(session_factory)
    original = service.create(
        target_id=target.id,
        selection=exact(compatible, first),
        instance_type=InstanceType.PERSISTENT,
    )

    with pytest.raises(KodiVersionIncompatibleError):
        service.reassign_runtime(
            target_id=target.id,
            selection=exact(incompatible, second),
        )

    assert load_instance(session_factory, target.id) == original


def test_missing_target(session_factory: sessionmaker[Session]) -> None:
    target_id = uuid4()

    with pytest.raises(TargetNotFoundError):
        ManagedInstanceService(session_factory).create(
            target_id=target_id,
            selection=NewestApprovedRevisionSelection(uuid4()),
            instance_type=InstanceType.PERSISTENT,
        )


def test_missing_runtime_definition(session_factory: sessionmaker[Session]) -> None:
    target = make_target()
    seed(session_factory, target=target)

    with pytest.raises(RuntimeDefinitionNotFoundError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=NewestApprovedRevisionSelection(uuid4()),
            instance_type=InstanceType.PERSISTENT,
        )


def test_missing_exact_revision(session_factory: sessionmaker[Session]) -> None:
    target = make_target()
    definition = make_definition()
    seed(session_factory, target=target, definition=definition)

    with pytest.raises(RuntimeRevisionNotFoundError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=ExactRevisionSelection(definition.id, uuid4()),
            instance_type=InstanceType.PERSISTENT,
        )


def test_exact_revision_must_belong_to_intended_definition(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    intended = make_definition()
    actual = make_definition()
    revision = make_revision(actual, 1)
    seed(session_factory, target=target, definition=intended)
    seed(session_factory, definition=actual, revisions=(revision,))

    with pytest.raises(RuntimeRevisionDefinitionMismatchError):
        ManagedInstanceService(session_factory).create(
            target_id=target.id,
            selection=ExactRevisionSelection(intended.id, revision.id),
            instance_type=InstanceType.PERSISTENT,
        )


def test_missing_managed_instance_for_reassignment(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))

    with pytest.raises(ManagedInstanceNotFoundError):
        ManagedInstanceService(session_factory).reassign_runtime(
            target_id=target.id,
            selection=exact(definition, revision),
        )


def test_duplicate_managed_instance_is_typed_conflict_and_preserves_original(
    session_factory: sessionmaker[Session],
) -> None:
    target = make_target()
    definition = make_definition()
    revision = make_revision(definition, 1)
    seed(session_factory, target=target, definition=definition, revisions=(revision,))
    service = ManagedInstanceService(session_factory)
    original = service.create(
        target_id=target.id,
        selection=exact(definition, revision),
        instance_type=InstanceType.PERSISTENT,
        notes="original",
    )

    with pytest.raises(ManagedInstanceConflictError):
        service.create(
            target_id=target.id,
            selection=exact(definition, revision),
            instance_type=InstanceType.DISPOSABLE,
            notes="replacement",
        )

    assert load_instance(session_factory, target.id) == original

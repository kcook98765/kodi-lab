from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    CatalogDataValidationError,
    ExactRevisionSelection,
    InvalidRevisionLifecycleTransitionError,
    ManagedInstanceService,
    RevisionNotApprovedError,
    RuntimeCatalogAdministrationService,
    RuntimeContainerArtifactConflictError,
    RuntimeDefinitionConflictError,
    RuntimeDefinitionNotFoundError,
    RuntimeRevisionConflictError,
    RuntimeRevisionNotFoundError,
    TrustedCustomBuildConfirmationRequiredError,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    InstanceType,
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeKind,
    RuntimeRevision,
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
    RuntimeRevisionRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def catalog_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'catalog-service.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)

    yield factory

    engine.dispose()


def register_definition(
    service: RuntimeCatalogAdministrationService,
    *,
    slug: str = "kodi-21.3",
    kodi_version: str = "21.3",
) -> RuntimeDefinition:
    return service.register_definition(
        definition_id=uuid4(),
        slug=slug,
        kodi_version=kodi_version,
    )


def register_revision(
    service: RuntimeCatalogAdministrationService,
    definition: RuntimeDefinition,
    *,
    revision_id: UUID | None = None,
    revision_number: int = 1,
    requires_trust: bool = False,
    source_kind: str = "official-package",
    content_digest: str | None = None,
) -> RuntimeRevision:
    return service.register_revision(
        revision_id=uuid4() if revision_id is None else revision_id,
        runtime_definition_id=definition.id,
        revision_number=revision_number,
        source_kind=source_kind,
        source_reference=f"ppa:team-xbmc/ppa@21.3-r{revision_number}",
        content_digest=(
            f"{revision_number:064x}" if content_digest is None else content_digest
        ),
        manifest_reference=f"catalog/{definition.slug}.yaml",
        manifest_digest="f" * 64,
        built_at=NOW,
        build_log_reference=f"logs/{definition.slug}-{revision_number}.log",
        requires_trusted_build_confirmation=requires_trust,
    )


def make_target(kodi_version: str = "21.3") -> Target:
    return Target(
        id=uuid4(),
        name=f"Target {uuid4().hex[:8]}",
        target_type=TargetType.MANAGED,
        kodi_version=kodi_version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )


def persist_target(factory: sessionmaker[Session], target: Target) -> None:
    with factory.begin() as session:
        TargetRepository(session).create(target)


def load_revision(
    factory: sessionmaker[Session], revision: RuntimeRevision
) -> RuntimeRevision | None:
    with factory() as session:
        return RuntimeRevisionRepository(session).get(revision.id)


def register_eligibility_dependencies(
    service: RuntimeCatalogAdministrationService, revision: RuntimeRevision
) -> None:
    service.register_container_artifact(
        runtime_revision_id=revision.id, image_id="sha256:" + ("e" * 64)
    )
    service.register_execution_profile(
        runtime_revision_id=revision.id,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )
    service.record_validation_evidence(
        evidence_id=uuid4(),
        runtime_revision_id=revision.id,
        contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
        result=RuntimeValidationResult.PASSED,
        started_at=NOW,
        completed_at=NOW,
        evidence_reference=f"test://validation/{revision.id}",
        recorded_at=NOW,
    )


def test_register_runtime_definition(catalog_factory: sessionmaker[Session]) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)

    definition = register_definition(service)

    with catalog_factory() as session:
        assert RuntimeDefinitionRepository(session).get(definition.id) == definition


def test_register_immutable_runtime_container_artifact(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    revision = register_revision(service, register_definition(service))

    artifact = service.register_container_artifact(
        runtime_revision_id=revision.id, image_id="sha256:" + ("a" * 64)
    )
    assert artifact.image_id == "sha256:" + ("a" * 64)
    with catalog_factory() as session:
        assert RuntimeContainerArtifactRepository(session).get(revision.id) == artifact

    with pytest.raises(RuntimeContainerArtifactConflictError):
        service.register_container_artifact(
            runtime_revision_id=revision.id, image_id="sha256:" + ("b" * 64)
        )


def test_container_artifact_registration_validates_revision_and_image(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    with pytest.raises(RuntimeRevisionNotFoundError):
        service.register_container_artifact(
            runtime_revision_id=uuid4(), image_id="sha256:" + ("a" * 64)
        )
    with pytest.raises(CatalogDataValidationError):
        service.register_container_artifact(
            runtime_revision_id=uuid4(), image_id="mutable:tag"
        )
    with pytest.raises(CatalogDataValidationError):
        service.register_container_artifact(
            runtime_revision_id=uuid4(), image_id="A" * 64
        )


def test_invalid_definition_data_is_typed_and_not_persisted(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)

    with pytest.raises(CatalogDataValidationError, match="slug"):
        service.register_definition(
            definition_id=uuid4(), slug="Invalid Slug", kodi_version="21.3"
        )

    with catalog_factory() as session:
        assert RuntimeDefinitionRepository(session).list() == []


def test_duplicate_definition_slug_is_typed_conflict_and_rolls_back(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    original = register_definition(service)

    with pytest.raises(RuntimeDefinitionConflictError) as raised:
        register_definition(service)

    assert raised.value.slug == original.slug
    with catalog_factory() as session:
        assert RuntimeDefinitionRepository(session).list() == [original]


def test_duplicate_definition_identity_is_typed_conflict(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    original = register_definition(service)

    with pytest.raises(RuntimeDefinitionConflictError):
        service.register_definition(
            definition_id=original.id,
            slug="kodi-21.3-alternate",
            kodi_version="21.3",
        )


def test_register_finalized_revision_starts_unapproved_and_immutable(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)

    revision = register_revision(service, definition)

    assert revision.approval_state is RevisionApprovalState.NOT_APPROVED
    assert revision.approved_at is None
    assert revision.trusted_build_confirmed_at is None
    assert load_revision(catalog_factory, revision) == revision


def test_revision_requires_existing_definition(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    missing = RuntimeDefinition(id=uuid4(), slug="missing", kodi_version="21.3")

    with pytest.raises(RuntimeDefinitionNotFoundError):
        register_revision(service, missing)


def test_duplicate_revision_number_is_typed_conflict_and_rolls_back(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    original = register_revision(service, definition)

    with pytest.raises(RuntimeRevisionConflictError) as raised:
        register_revision(service, definition)

    assert raised.value.revision_number == 1
    with catalog_factory() as session:
        assert RuntimeRevisionRepository(session).list_for_definition(
            definition.id
        ) == [original]


def test_duplicate_revision_identity_is_typed_conflict(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    original = register_revision(service, definition)

    with pytest.raises(RuntimeRevisionConflictError):
        register_revision(
            service,
            definition,
            revision_id=original.id,
            revision_number=2,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"revision_number": 0}, "positive"),
        ({"source_kind": " "}, "source_kind"),
        ({"content_digest": "not-a-digest"}, "SHA-256"),
    ],
)
def test_invalid_revision_data_is_typed_and_not_persisted(
    catalog_factory: sessionmaker[Session],
    overrides: dict[str, object],
    message: str,
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)

    with pytest.raises(CatalogDataValidationError, match=message):
        register_revision(service, definition, **overrides)  # type: ignore[arg-type]

    with catalog_factory() as session:
        assert (
            RuntimeRevisionRepository(session).list_for_definition(definition.id) == []
        )


def test_approval_and_revocation_are_atomic_and_idempotent(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    revision = register_revision(service, definition)

    approved = service.approve_revision(revision.id, approved_at=NOW, note="reviewed")
    repeated = service.approve_revision(
        revision.id,
        approved_at=NOW + timedelta(hours=1),
        note="replacement ignored",
    )

    assert approved.approval_state is RevisionApprovalState.APPROVED
    assert approved.approved_at == NOW
    assert approved.approval_note == "reviewed"
    assert repeated == approved

    revoked = service.revoke_revision_approval(revision.id)
    assert revoked.approval_state is RevisionApprovalState.NOT_APPROVED
    assert revoked.approved_at is None
    assert service.revoke_revision_approval(revision.id) == revoked
    assert load_revision(catalog_factory, revision) == revoked


def test_invalid_approval_timestamp_is_typed_lifecycle_error(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    revision = register_revision(service, definition)

    with pytest.raises(InvalidRevisionLifecycleTransitionError, match="timezone"):
        service.approve_revision(revision.id, approved_at=datetime(2026, 9, 14, 12, 0))

    assert load_revision(catalog_factory, revision) == revision


def test_missing_revision_lifecycle_operation_is_typed(
    catalog_factory: sessionmaker[Session],
) -> None:
    with pytest.raises(RuntimeRevisionNotFoundError):
        RuntimeCatalogAdministrationService(catalog_factory).revoke_revision_approval(
            uuid4()
        )


def test_trust_confirmation_is_independent_idempotent_and_revocable(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    revision = register_revision(service, definition, requires_trust=True)

    confirmed = service.confirm_trusted_custom_build(revision.id, confirmed_at=NOW)
    repeated = service.confirm_trusted_custom_build(
        revision.id, confirmed_at=NOW + timedelta(hours=1)
    )

    assert confirmed.approval_state is RevisionApprovalState.NOT_APPROVED
    assert confirmed.trusted_build_confirmed_at == NOW
    assert repeated == confirmed

    revoked = service.revoke_trusted_custom_build_confirmation(revision.id)
    assert revoked.approval_state is RevisionApprovalState.NOT_APPROVED
    assert revoked.trusted_build_confirmed_at is None
    assert service.revoke_trusted_custom_build_confirmation(revision.id) == revoked


def test_trust_lifecycle_is_rejected_when_revision_does_not_require_it(
    catalog_factory: sessionmaker[Session],
) -> None:
    service = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(service)
    revision = register_revision(service, definition)

    with pytest.raises(InvalidRevisionLifecycleTransitionError):
        service.confirm_trusted_custom_build(revision.id, confirmed_at=NOW)
    with pytest.raises(InvalidRevisionLifecycleTransitionError):
        service.revoke_trusted_custom_build_confirmation(revision.id)


def test_registration_approval_and_managed_selection_compose(
    catalog_factory: sessionmaker[Session],
) -> None:
    catalog = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(catalog)
    revision = register_revision(catalog, definition)
    target = make_target()
    persist_target(catalog_factory, target)
    instances = ManagedInstanceService(catalog_factory)
    selection = ExactRevisionSelection(definition.id, revision.id)

    with pytest.raises(RevisionNotApprovedError):
        instances.create(
            target_id=target.id,
            selection=selection,
            instance_type=InstanceType.PERSISTENT,
        )

    catalog.approve_revision(revision.id, approved_at=NOW)
    register_eligibility_dependencies(catalog, revision)
    created = instances.create(
        target_id=target.id,
        selection=selection,
        instance_type=InstanceType.PERSISTENT,
    )
    catalog.revoke_revision_approval(revision.id)

    assert created.runtime_revision_id == revision.id
    with catalog_factory() as session:
        assert ManagedInstanceRepository(session).get(target.id) == created

    second_target = make_target()
    persist_target(catalog_factory, second_target)
    with pytest.raises(RevisionNotApprovedError):
        instances.create(
            target_id=second_target.id,
            selection=selection,
            instance_type=InstanceType.PERSISTENT,
        )


def test_trust_confirmation_controls_future_selection_not_existing_reference(
    catalog_factory: sessionmaker[Session],
) -> None:
    catalog = RuntimeCatalogAdministrationService(catalog_factory)
    definition = register_definition(catalog)
    revision = register_revision(catalog, definition, requires_trust=True)
    catalog.approve_revision(revision.id, approved_at=NOW)
    first_target = make_target()
    persist_target(catalog_factory, first_target)
    instances = ManagedInstanceService(
        catalog_factory, trusted_custom_builds_enabled=True
    )
    selection = ExactRevisionSelection(definition.id, revision.id)

    with pytest.raises(TrustedCustomBuildConfirmationRequiredError):
        instances.create(
            target_id=first_target.id,
            selection=selection,
            instance_type=InstanceType.PERSISTENT,
        )

    catalog.confirm_trusted_custom_build(revision.id, confirmed_at=NOW)
    register_eligibility_dependencies(catalog, revision)
    created = instances.create(
        target_id=first_target.id,
        selection=selection,
        instance_type=InstanceType.PERSISTENT,
    )
    catalog.revoke_trusted_custom_build_confirmation(revision.id)

    assert created.runtime_revision_id == revision.id
    with catalog_factory() as session:
        assert ManagedInstanceRepository(session).get(first_target.id) == created

    second_target = make_target()
    persist_target(catalog_factory, second_target)
    with pytest.raises(TrustedCustomBuildConfirmationRequiredError):
        instances.create(
            target_id=second_target.id,
            selection=selection,
            instance_type=InstanceType.PERSISTENT,
        )

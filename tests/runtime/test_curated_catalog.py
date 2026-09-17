from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import RuntimeMatrixService
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    KodiReleaseIdentity,
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
from kodi_lab.runtime import (
    CURATED_SUPPRESSION_REVISIONS,
    CuratedSuppressionRevision,
    derive_curated_suppression_revision,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=UTC)


def _factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'curated.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def _support(lineage: CuratedSuppressionRevision) -> RuntimeSupportTier:
    if lineage.canonical_release == "19.5":
        return RuntimeSupportTier.LEGACY
    if "beta" in lineage.canonical_release:
        return RuntimeSupportTier.PREVIEW
    return RuntimeSupportTier.CURRENT


def _predecessor(
    lineage: CuratedSuppressionRevision,
) -> tuple[RuntimeRevision, RuntimeContainerArtifact, RuntimeExecutionProfile]:
    release = KodiReleaseIdentity.parse(lineage.canonical_release)
    requires_trust = _support(lineage) is RuntimeSupportTier.PREVIEW
    revision = RuntimeRevision(
        id=lineage.predecessor_revision_id,
        runtime_definition_id=lineage.runtime_definition_id,
        revision_number=lineage.predecessor_revision_number,
        source_kind="curated",
        source_reference=f"repository:{lineage.canonical_release}",
        content_digest=lineage.image_id,
        manifest_reference=f"runtime/kodi-{lineage.canonical_release}/manifest.json",
        manifest_digest="sha256:" + lineage.predecessor_revision_id.hex * 2,
        built_at=NOW,
        approval_state=RevisionApprovalState.APPROVED,
        approved_at=NOW,
        requires_trusted_build_confirmation=requires_trust,
        trusted_build_confirmed_at=NOW if requires_trust else None,
    )
    return (
        revision,
        RuntimeContainerArtifact(revision.id, lineage.image_id),
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
            poll_interval_seconds=1.0,
            http_timeout_seconds=3.0,
        ),
    )


def test_curated_suppression_revision_identities_are_exact_and_monotonic() -> None:
    assert [
        (
            item.canonical_release,
            item.suppression_revision_number,
            str(item.suppression_revision_id),
        )
        for item in CURATED_SUPPRESSION_REVISIONS
    ] == [
        ("19.5", 2, "51950000-0000-4000-8000-000000000005"),
        ("20.5", 2, "52000000-0000-4000-8000-000000000005"),
        ("21.3", 3, "3a000000-0000-4000-8000-000000000006"),
        ("22.0-beta.2", 2, "52200000-0000-4000-8000-000000000005"),
    ]


def test_derivation_changes_only_revision_identity_and_bytecode_semantics() -> None:
    for lineage in CURATED_SUPPRESSION_REVISIONS:
        old_revision, old_artifact, old_profile = _predecessor(lineage)
        new_revision, new_artifact, new_profile = derive_curated_suppression_revision(
            lineage,
            predecessor=old_revision,
            artifact=old_artifact,
            profile=old_profile,
        )
        assert old_revision.id == lineage.predecessor_revision_id
        assert old_profile.python_bytecode_policy is (
            PythonBytecodePolicy.INTERPRETER_DEFAULT
        )
        assert new_revision.id == lineage.suppression_revision_id
        assert new_revision.revision_number == old_revision.revision_number + 1
        assert new_revision.approval_state is RevisionApprovalState.NOT_APPROVED
        assert new_artifact.image_id == old_artifact.image_id
        assert new_profile.python_bytecode_policy is PythonBytecodePolicy.SUPPRESS
        assert (
            replace(
                new_profile,
                runtime_revision_id=old_profile.runtime_revision_id,
                python_bytecode_policy=old_profile.python_bytecode_policy,
            )
            == old_profile
        )
        assert (
            replace(
                new_revision,
                id=old_revision.id,
                revision_number=old_revision.revision_number,
                approval_state=old_revision.approval_state,
                approved_at=old_revision.approved_at,
                trusted_build_confirmed_at=old_revision.trusted_build_confirmed_at,
            )
            == old_revision
        )


def test_old_and_new_catalog_entries_coexist_and_newest_eligible_is_suppression(
    tmp_path: Path,
) -> None:
    factory = _factory(tmp_path)
    targets: dict[str, UUID] = {}
    with factory.begin() as session:
        for index, lineage in enumerate(CURATED_SUPPRESSION_REVISIONS, start=1):
            definition = RuntimeDefinition(
                id=lineage.runtime_definition_id,
                slug=f"curated-{lineage.canonical_release.replace('.', '-')}",
                kodi_version=lineage.canonical_release,
                support_tier=_support(lineage),
            )
            target = Target(
                id=UUID(f"87000000-0000-4000-8000-{index:012d}"),
                name=f"Kodi {lineage.canonical_release}",
                target_type=TargetType.MANAGED,
                kodi_version=lineage.canonical_release,
                availability=TargetAvailability.UNAVAILABLE,
                health=HealthStatus.WARNING,
                connection_state=ConnectionState.DISCONNECTED,
            )
            old_revision, old_artifact, old_profile = _predecessor(lineage)
            new_revision, new_artifact, new_profile = (
                derive_curated_suppression_revision(
                    lineage,
                    predecessor=old_revision,
                    artifact=old_artifact,
                    profile=old_profile,
                )
            )
            new_revision = new_revision.approve(approved_at=NOW + timedelta(seconds=1))
            if new_revision.requires_trusted_build_confirmation:
                new_revision = new_revision.confirm_trusted_build(
                    confirmed_at=NOW + timedelta(seconds=1)
                )
            RuntimeDefinitionRepository(session).create(definition)
            TargetRepository(session).create(target)
            for revision, artifact, profile, offset in (
                (old_revision, old_artifact, old_profile, 0),
                (new_revision, new_artifact, new_profile, 1),
            ):
                RuntimeRevisionRepository(session).create(revision)
                RuntimeContainerArtifactRepository(session).create(artifact)
                RuntimeExecutionProfileRepository(session).create(profile)
                RuntimeValidationEvidenceRepository(session).create(
                    RuntimeValidationEvidence(
                        id=UUID(f"88000000-0000-4000-8000-{index * 10 + offset:012d}"),
                        runtime_revision_id=revision.id,
                        contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
                        result=RuntimeValidationResult.PASSED,
                        started_at=NOW + timedelta(seconds=offset),
                        completed_at=NOW + timedelta(seconds=offset),
                        evidence_reference=f"evidence:{revision.id}",
                        recorded_at=NOW + timedelta(seconds=offset),
                    )
                )
            targets[lineage.canonical_release] = target.id

    normal = RuntimeMatrixService(factory)
    trusted = RuntimeMatrixService(factory, trusted_custom_builds_enabled=True)
    for lineage in CURATED_SUPPRESSION_REVISIONS:
        result = (
            trusted.resolve_target(
                targets[lineage.canonical_release],
                include_legacy=True,
                include_prerelease=True,
            )
            if lineage.canonical_release == "22.0-beta.2"
            else normal.resolve_target(
                targets[lineage.canonical_release], include_legacy=True
            )
        )
        assert result.selected_revision_number == lineage.suppression_revision_number
        with factory() as session:
            revisions = RuntimeRevisionRepository(session).list_for_definition(
                lineage.runtime_definition_id
            )
            assert {item.id for item in revisions} == {
                lineage.predecessor_revision_id,
                lineage.suppression_revision_id,
            }
            stored_old_profile = RuntimeExecutionProfileRepository(session).get(
                lineage.predecessor_revision_id
            )
            stored_new_profile = RuntimeExecutionProfileRepository(session).get(
                lineage.suppression_revision_id
            )
            assert stored_old_profile is not None
            assert stored_new_profile is not None
            assert stored_old_profile.python_bytecode_policy is (
                PythonBytecodePolicy.INTERPRETER_DEFAULT
            )
            assert (
                stored_new_profile.python_bytecode_policy
                is PythonBytecodePolicy.SUPPRESS
            )

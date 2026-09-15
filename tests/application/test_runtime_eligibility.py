from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    ExactRevisionSelection,
    ManagedInstanceService,
    NewestEligibleRevisionSelection,
    NoEligibleRevisionError,
    RuntimeIneligibilityReason,
    RuntimeRevisionEligibilityPolicy,
    RuntimeRevisionIneligibleError,
    RuntimeSelectionPolicy,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    InstanceType,
    ProcessObservationKind,
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

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'eligibility.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def definition(
    *,
    version: str = "21.3",
    tier: RuntimeSupportTier = RuntimeSupportTier.CURRENT,
) -> RuntimeDefinition:
    return RuntimeDefinition(
        id=uuid4(),
        slug=f"kodi-{version.replace('.', '-')}-{uuid4().hex[:8]}",
        kodi_version=version,
        support_tier=tier,
    )


def target(version: str = "21.3") -> Target:
    return Target(
        id=uuid4(),
        name="matrix target",
        target_type=TargetType.MANAGED,
        kodi_version=version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )


def revision(
    parent: RuntimeDefinition,
    number: int = 1,
    *,
    approved: bool = True,
    requires_trust: bool = False,
    confirmed: bool = False,
) -> RuntimeRevision:
    return RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=parent.id,
        revision_number=number,
        source_kind="official-package",
        source_reference=f"test://{parent.slug}/{number}",
        content_digest=f"{number:064x}",
        manifest_reference=f"runtime/{parent.slug}/manifest.json",
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


def add_profile(
    session: Session, item: RuntimeRevision, parent: RuntimeDefinition
) -> None:
    release = parent.kodi_release
    RuntimeExecutionProfileRepository(session).create(
        RuntimeExecutionProfile(
            runtime_revision_id=item.id,
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


def add_evidence(
    session: Session,
    item: RuntimeRevision,
    result: RuntimeValidationResult,
    *,
    offset: int = 0,
    contract: str = KODI_RUNTIME_VALIDATION_CONTRACT,
) -> None:
    at = NOW + timedelta(seconds=offset)
    RuntimeValidationEvidenceRepository(session).create(
        RuntimeValidationEvidence(
            id=uuid4(),
            runtime_revision_id=item.id,
            contract_version=contract,
            result=result,
            started_at=at,
            completed_at=at,
            evidence_reference=f"test://evidence/{item.id}/{offset}",
            recorded_at=at,
        )
    )


def seed(
    factory: sessionmaker[Session],
    parent: RuntimeDefinition,
    target_item: Target,
    items: tuple[RuntimeRevision, ...],
    *,
    artifact: bool = True,
    profile: bool = True,
    validation: RuntimeValidationResult | None = RuntimeValidationResult.PASSED,
) -> None:
    with factory.begin() as session:
        RuntimeDefinitionRepository(session).create(parent)
        TargetRepository(session).create(target_item)
        for item in items:
            RuntimeRevisionRepository(session).create(item)
            if artifact:
                RuntimeContainerArtifactRepository(session).create(
                    RuntimeContainerArtifact(
                        runtime_revision_id=item.id,
                        image_id="sha256:" + f"{item.revision_number:064x}",
                    )
                )
            if profile:
                add_profile(session, item, parent)
            if validation is not None:
                add_evidence(session, item, validation)


def evaluate(
    factory: sessionmaker[Session],
    target_item: Target,
    parent: RuntimeDefinition,
    item: RuntimeRevision,
    policy: RuntimeSelectionPolicy | None = None,
) -> RuntimeIneligibilityReason | None:
    with factory() as session:
        result = RuntimeRevisionEligibilityPolicy().evaluate(
            session,
            target=target_item,
            definition=parent,
            revision=item,
            policy=policy or RuntimeSelectionPolicy(),
        )
    assert result.eligible is (result.reason is None)
    return result.reason


@pytest.mark.parametrize(
    ("tier", "policy", "expected"),
    [
        (RuntimeSupportTier.CURRENT, RuntimeSelectionPolicy(), None),
        (
            RuntimeSupportTier.LEGACY,
            RuntimeSelectionPolicy(),
            RuntimeIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED,
        ),
        (
            RuntimeSupportTier.LEGACY,
            RuntimeSelectionPolicy(include_legacy=True),
            None,
        ),
        (
            RuntimeSupportTier.PREVIEW,
            RuntimeSelectionPolicy(),
            RuntimeIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED,
        ),
        (
            RuntimeSupportTier.PREVIEW,
            RuntimeSelectionPolicy(include_prerelease=True),
            None,
        ),
        (
            RuntimeSupportTier.RETIRED,
            RuntimeSelectionPolicy(include_legacy=True, include_prerelease=True),
            RuntimeIneligibilityReason.SUPPORT_TIER_NOT_PERMITTED,
        ),
    ],
)
def test_support_tier_selection_policy(
    factory: sessionmaker[Session],
    tier: RuntimeSupportTier,
    policy: RuntimeSelectionPolicy,
    expected: RuntimeIneligibilityReason | None,
) -> None:
    version = "22.0-beta.2" if tier is RuntimeSupportTier.PREVIEW else "21.3"
    parent = definition(version=version, tier=tier)
    target_item = target(version)
    item = revision(parent)
    seed(factory, parent, target_item, (item,))
    assert evaluate(factory, target_item, parent, item, policy) is expected


@pytest.mark.parametrize(
    ("approved", "artifact", "profile", "validation", "expected"),
    [
        (
            False,
            True,
            True,
            RuntimeValidationResult.PASSED,
            RuntimeIneligibilityReason.REVISION_NOT_APPROVED,
        ),
        (
            True,
            False,
            True,
            RuntimeValidationResult.PASSED,
            RuntimeIneligibilityReason.ARTIFACT_UNAVAILABLE,
        ),
        (
            True,
            True,
            False,
            RuntimeValidationResult.PASSED,
            RuntimeIneligibilityReason.EXECUTION_PROFILE_UNAVAILABLE,
        ),
        (True, True, True, None, RuntimeIneligibilityReason.UNVALIDATED),
        (
            True,
            True,
            True,
            RuntimeValidationResult.FAILED,
            RuntimeIneligibilityReason.VALIDATION_FAILED,
        ),
    ],
)
def test_revision_eligibility_gates(
    factory: sessionmaker[Session],
    approved: bool,
    artifact: bool,
    profile: bool,
    validation: RuntimeValidationResult | None,
    expected: RuntimeIneligibilityReason,
) -> None:
    parent = definition()
    target_item = target()
    item = revision(parent, approved=approved)
    seed(
        factory,
        parent,
        target_item,
        (item,),
        artifact=artifact,
        profile=profile,
        validation=validation,
    )
    assert evaluate(factory, target_item, parent, item) is expected


def test_trust_and_exact_compatibility_gates(factory: sessionmaker[Session]) -> None:
    parent = definition()
    target_item = target("20.5")
    item = revision(parent, requires_trust=True)
    seed(factory, parent, target_item, (item,))
    assert (
        evaluate(factory, target_item, parent, item)
        is RuntimeIneligibilityReason.INCOMPATIBLE_RELEASE
    )

    compatible = target("21.3")
    assert (
        evaluate(factory, compatible, parent, item)
        is RuntimeIneligibilityReason.TRUSTED_CUSTOM_BUILDS_DISABLED
    )
    enabled = RuntimeSelectionPolicy(trusted_custom_builds_enabled=True)
    assert (
        evaluate(factory, compatible, parent, item, enabled)
        is RuntimeIneligibilityReason.TRUSTED_BUILD_CONFIRMATION_REQUIRED
    )


def test_latest_applicable_evidence_controls_effective_state(
    factory: sessionmaker[Session],
) -> None:
    parent = definition()
    target_item = target()
    item = revision(parent)
    seed(factory, parent, target_item, (item,), validation=None)
    with factory.begin() as session:
        add_evidence(session, item, RuntimeValidationResult.PASSED, contract="older-v1")
        add_evidence(session, item, RuntimeValidationResult.PASSED, offset=1)
        add_evidence(session, item, RuntimeValidationResult.FAILED, offset=2)
    assert (
        evaluate(factory, target_item, parent, item)
        is RuntimeIneligibilityReason.VALIDATION_FAILED
    )
    with factory.begin() as session:
        add_evidence(session, item, RuntimeValidationResult.PASSED, offset=3)
    assert evaluate(factory, target_item, parent, item) is None


def test_newest_eligible_skips_newer_failed_revision(
    factory: sessionmaker[Session],
) -> None:
    parent = definition()
    target_item = target()
    older = revision(parent, 1)
    newer = revision(parent, 2)
    seed(factory, parent, target_item, (older, newer), validation=None)
    with factory.begin() as session:
        add_evidence(session, older, RuntimeValidationResult.PASSED)
        add_evidence(session, newer, RuntimeValidationResult.FAILED)
    created = ManagedInstanceService(factory).create(
        target_id=target_item.id,
        selection=NewestEligibleRevisionSelection(parent.id),
        instance_type=InstanceType.DISPOSABLE,
    )
    assert created.runtime_revision_id == older.id


def test_exact_ineligible_and_no_eligible_revision_are_typed(
    factory: sessionmaker[Session],
) -> None:
    parent = definition()
    target_item = target()
    item = revision(parent)
    seed(factory, parent, target_item, (item,), validation=None)
    service = ManagedInstanceService(factory)
    with pytest.raises(RuntimeRevisionIneligibleError) as exact_error:
        service.create(
            target_id=target_item.id,
            selection=ExactRevisionSelection(parent.id, item.id),
            instance_type=InstanceType.DISPOSABLE,
        )
    assert exact_error.value.reason == "unvalidated"
    with pytest.raises(NoEligibleRevisionError):
        service.create(
            target_id=target_item.id,
            selection=NewestEligibleRevisionSelection(parent.id),
            instance_type=InstanceType.DISPOSABLE,
        )


def test_existing_pin_survives_retirement_without_reselection(
    factory: sessionmaker[Session],
) -> None:
    parent = definition()
    target_item = target()
    item = revision(parent)
    seed(factory, parent, target_item, (item,))
    created = ManagedInstanceService(factory).create(
        target_id=target_item.id,
        selection=ExactRevisionSelection(parent.id, item.id),
        instance_type=InstanceType.DISPOSABLE,
    )
    with factory.begin() as session:
        RuntimeDefinitionRepository(session).transition_support_tier(
            parent.id, RuntimeSupportTier.RETIRED
        )
    with factory() as session:
        assert ManagedInstanceRepository(session).get(target_item.id) == created
    with pytest.raises(RuntimeRevisionIneligibleError) as raised:
        ManagedInstanceService(factory).reassign_runtime(
            target_id=target_item.id,
            selection=ExactRevisionSelection(parent.id, item.id),
        )
    assert raised.value.reason == "support_tier_not_permitted"

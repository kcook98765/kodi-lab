from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kodi_lab.application import (
    CatalogDataValidationError,
    InvalidSupportTierTransitionError,
    RuntimeCatalogAdministrationService,
    RuntimeRevisionNotFoundError,
    RuntimeValidationEvidenceConflictError,
)
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ProcessObservationKind,
    ReadinessProbeKind,
    RuntimeKind,
    RuntimeSupportTier,
    RuntimeValidationResult,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeValidationEvidenceRepository

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def service(tmp_path: Path) -> RuntimeCatalogAdministrationService:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'phase5-admin.db'}")
    metadata.create_all(engine)
    return RuntimeCatalogAdministrationService(create_session_factory(engine))


def revision_id(
    admin: RuntimeCatalogAdministrationService,
    *,
    version: str = "21.3",
    tier: RuntimeSupportTier = RuntimeSupportTier.CURRENT,
) -> UUID:
    definition = admin.register_definition(
        definition_id=uuid4(),
        slug=f"kodi-{uuid4().hex}",
        kodi_version=version,
        support_tier=tier,
    )
    revision = admin.register_revision(
        revision_id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=1,
        source_kind="official-package",
        source_reference="test://source",
        content_digest="a" * 64,
        manifest_reference="test://manifest",
        manifest_digest="b" * 64,
        built_at=NOW,
    )
    return revision.id


def test_support_registration_and_explicit_transitions(tmp_path: Path) -> None:
    admin = service(tmp_path)
    stable = admin.register_definition(
        definition_id=uuid4(),
        slug="kodi-21-3",
        kodi_version="21.3",
        support_tier=RuntimeSupportTier.CURRENT,
    )
    legacy = admin.transition_support_tier(
        stable.id, support_tier=RuntimeSupportTier.LEGACY
    )
    assert legacy.support_tier is RuntimeSupportTier.LEGACY
    retired = admin.transition_support_tier(
        stable.id, support_tier=RuntimeSupportTier.RETIRED
    )
    assert retired.support_tier is RuntimeSupportTier.RETIRED
    assert (
        admin.transition_support_tier(
            stable.id, support_tier=RuntimeSupportTier.RETIRED
        )
        == retired
    )
    with pytest.raises(InvalidSupportTierTransitionError):
        admin.transition_support_tier(
            stable.id, support_tier=RuntimeSupportTier.CURRENT
        )

    preview = admin.register_definition(
        definition_id=uuid4(),
        slug="kodi-22-beta2",
        kodi_version="22.0-beta.2",
        support_tier=RuntimeSupportTier.PREVIEW,
    )
    assert preview.support_tier is RuntimeSupportTier.PREVIEW
    with pytest.raises(CatalogDataValidationError):
        admin.register_definition(
            definition_id=uuid4(),
            slug="bad-preview",
            kodi_version="22.0-beta.2",
            support_tier=RuntimeSupportTier.CURRENT,
        )


def test_validation_evidence_is_append_only_and_latest_is_effective(
    tmp_path: Path,
) -> None:
    admin = service(tmp_path)
    runtime_revision_id = revision_id(admin)
    first_id = uuid4()
    first = admin.record_validation_evidence(
        evidence_id=first_id,
        runtime_revision_id=runtime_revision_id,
        contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
        result=RuntimeValidationResult.PASSED,
        started_at=NOW,
        completed_at=NOW,
        evidence_reference="validation://phase3/cycle-evidence",
        summary="two clean cycles",
        recorded_at=NOW,
    )
    later = admin.record_validation_evidence(
        evidence_id=uuid4(),
        runtime_revision_id=runtime_revision_id,
        contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
        result=RuntimeValidationResult.FAILED,
        started_at=NOW + timedelta(minutes=1),
        completed_at=NOW + timedelta(minutes=1),
        evidence_reference="validation://phase5/recheck",
        recorded_at=NOW + timedelta(minutes=1),
    )
    with admin._session_factory() as session:
        repository = RuntimeValidationEvidenceRepository(session)
        assert repository.list_for_revision(runtime_revision_id) == [first, later]
        assert (
            repository.latest_for_contract(
                runtime_revision_id, KODI_RUNTIME_VALIDATION_CONTRACT
            )
            == later
        )
    with pytest.raises(RuntimeValidationEvidenceConflictError):
        admin.record_validation_evidence(
            evidence_id=first_id,
            runtime_revision_id=runtime_revision_id,
            contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
            result=RuntimeValidationResult.PASSED,
            started_at=NOW,
            completed_at=NOW,
            evidence_reference="validation://duplicate",
            recorded_at=NOW,
        )


def test_validation_registration_rejects_unknown_contract_and_missing_revision(
    tmp_path: Path,
) -> None:
    admin = service(tmp_path)
    runtime_revision_id = revision_id(admin)
    with pytest.raises(CatalogDataValidationError):
        admin.record_validation_evidence(
            evidence_id=uuid4(),
            runtime_revision_id=runtime_revision_id,
            contract_version="unknown-v1",
            result=RuntimeValidationResult.PASSED,
            started_at=NOW,
            completed_at=NOW,
            evidence_reference="validation://evidence",
            recorded_at=NOW,
        )
    with pytest.raises(RuntimeRevisionNotFoundError):
        admin.record_validation_evidence(
            evidence_id=uuid4(),
            runtime_revision_id=uuid4(),
            contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
            result=RuntimeValidationResult.PASSED,
            started_at=NOW,
            completed_at=NOW,
            evidence_reference="validation://evidence",
            recorded_at=NOW,
        )


def test_prerelease_execution_profile_records_exact_beta_ordinal(
    tmp_path: Path,
) -> None:
    admin = service(tmp_path)
    runtime_revision_id = revision_id(
        admin, version="22.0-beta.2", tier=RuntimeSupportTier.PREVIEW
    )
    profile = admin.register_execution_profile(
        runtime_revision_id=runtime_revision_id,
        runtime_kind=RuntimeKind.KODI,
        expected_application_name="Kodi",
        expected_kodi_version="22.0",
        expected_version_tag="beta",
        expected_prerelease_ordinal=2,
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )
    assert profile.kodi_release.canonical == "22.0-beta.2"

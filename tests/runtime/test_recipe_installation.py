"""Durable recipe install and existing catalog eligibility integration."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeIneligibilityReason,
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.application.screenshots import ManagedScreenshotService
from kodi_lab.mcp.authorization import InternalManagedTargetAuthorityProvider
from kodi_lab.runtime.assembly import AssemblyResult
from kodi_lab.runtime.install import (
    _advance,
    _commit_ready,
    _create_operation,
    _fail,
    bootstrap_recipe_instance,
    recipe_target_id,
)
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.runtime.validation import ValidationResult
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'recipe.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def _assembly() -> AssemblyResult:
    return AssemblyResult(
        image_id=IMAGE,
        image_size=1_050_650_811,
        elapsed_seconds=115.28,
        build_log_digest="sha256:" + "b" * 64,
        build_log_bytes=124_319,
        apt_index_download="34.2 MB",
        apt_package_download="144 MB",
        package_inventory=tuple(
            (item.name, item.version, item.architecture)
            for item in load_reviewed_recipe("matrix").recipe.critical_packages
        ),
        apt_metadata_digests=(("jammy_InRelease", "sha256:" + "c" * 64),),
    )


def _validation() -> ValidationResult:
    reviewed = load_reviewed_recipe("matrix")
    now = datetime.now(UTC)
    return ValidationResult(
        started_at=now,
        completed_at=now,
        report={
            "schema": reviewed.recipe.validation_contract,
            "result": "passed",
            "recipe_digest": reviewed.digest,
            "release": "19.4",
            "image_id": IMAGE,
            "navigation": True,
            "screenshot": {
                "width": 1280,
                "height": 720,
                "software_renderer": "llvmpipe",
            },
        },
    )


def _ready(factory: sessionmaker[Session]) -> UUID:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = uuid4()
    _create_operation(factory, reviewed, operation_id)
    from kodi_lab.domain import RuntimeRecipeInstallationState as State

    for state in (State.PENDING, State.ACQUIRING, State.ASSEMBLING):
        _advance(factory, operation_id, expected=state)
    _commit_ready(factory, reviewed, operation_id, _assembly(), _validation())
    return operation_id


def test_ready_install_is_eligible_and_creates_one_exact_managed_pin(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory() as session:
        installation = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert installation is not None and installation.runtime_revision_id is not None
        revision_id = UUID(installation.runtime_revision_id)
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        revision = RuntimeRevisionRepository(session).get(revision_id)
        assert definition is not None and revision is not None
        assert (
            RuntimeRevisionEligibilityPolicy()
            .evaluate_for_release(
                session,
                required_release=definition.kodi_release,
                definition=definition,
                revision=revision,
                policy=RuntimeSelectionPolicy(),
            )
            .eligible
        )
        assert json.loads(installation.attestation_json or "")["image_id"] == IMAGE
        assert supports_reviewed_recipe_capability(session, revision_id, "screenshot")
        assert supports_reviewed_recipe_capability(session, revision_id, "navigation")
    bootstrap_recipe_instance(factory, reviewed, revision_id)
    bootstrap_recipe_instance(factory, reviewed, revision_id)
    with factory() as session:
        target_id = recipe_target_id(reviewed)
        assert TargetRepository(session).get(target_id) is not None
        instance = ManagedInstanceRepository(session).get(target_id)
        assert instance is not None and instance.runtime_revision_id == revision_id
        assert len(ManagedInstanceRepository(session).list()) == 1
    provider = InternalManagedTargetAuthorityProvider(
        factory,
        screenshots=cast(
            ManagedScreenshotService,
            SimpleNamespace(supports_runtime_revision=lambda _revision_id: True),
        ),
    )
    authority = provider.resolve(recipe_target_id(reviewed))
    assert {"managed_screenshot", "semantic_remote"} <= authority.capabilities


def test_recipe_provenance_tamper_removes_eligibility(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None
        # Terminal rows are immutable in production. This bypasses the trigger
        # only in the metadata.create_all unit database to exercise the gate.
        row.image_id = "sha256:" + "f" * 64
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None and row.runtime_revision_id is not None
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        revision = RuntimeRevisionRepository(session).get(UUID(row.runtime_revision_id))
        assert definition is not None and revision is not None
        result = RuntimeRevisionEligibilityPolicy().evaluate_for_release(
            session,
            required_release=definition.kodi_release,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(),
        )
        assert result.reason is RuntimeIneligibilityReason.RECIPE_PROVENANCE_UNAVAILABLE
        assert not supports_reviewed_recipe_capability(
            session, revision.id, "screenshot"
        )


def test_failed_install_is_terminal_and_does_not_create_revision(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = uuid4()
    _create_operation(factory, reviewed, operation_id)
    _fail(factory, operation_id, code="installation_failed")
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None and row.state.value == "failed"
        assert row.failure_code == "installation_failed"
        assert row.runtime_revision_id is None
        assert (
            RuntimeRevisionRepository(session).list_for_definition(
                reviewed.recipe.definition_id
            )
            == []
        )

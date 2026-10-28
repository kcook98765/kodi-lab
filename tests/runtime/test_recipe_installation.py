"""Durable recipe install and existing catalog eligibility integration."""

import base64
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
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
from kodi_lab.config import Settings
from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.mcp.authorization import InternalManagedTargetAuthorityProvider
from kodi_lab.runtime.assembly import AssemblyResult
from kodi_lab.runtime.attestation_integrity import (
    AttestationIntegrityError,
    canonical_json,
    installed_package_digest,
)
from kodi_lab.runtime.install import (
    RecipeInstallationError,
    _advance,
    _commit_ready,
    _create_operation,
    _fail,
    _ready_installation,
    bootstrap_recipe_instance,
    install_reviewed_runtime,
    recipe_target_id,
)
from kodi_lab.runtime.lab import LabWorkflowError, repin_reviewed_runtime
from kodi_lab.runtime.recipes import load_novnc_phase1_recipe, load_reviewed_recipe
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.runtime.validation import ValidationResult
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    RuntimeRecipeInstallationRow,
    RuntimeValidationEvidenceRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

IMAGE = "sha256:" + "a" * 64


def test_broker_accepted_worker_failure_before_pending_is_durable(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    operation_id = uuid4()

    @contextmanager
    def busy_lock(_path: Path) -> Iterator[None]:
        raise RecipeInstallationError("another runtime installation is active")
        yield

    monkeypatch.setattr("kodi_lab.runtime.install._installation_lock", busy_lock)
    monkeypatch.setattr(
        "kodi_lab.runtime.install._database_call",
        lambda _settings, action: action(factory),
    )
    with pytest.raises(RecipeInstallationError):
        install_reviewed_runtime(
            "matrix",
            settings=Settings(runtime_root=tmp_path / "lock"),
            operation_id=operation_id,
        )
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None
        assert row.state.value == "failed"
        assert row.failure_code == "installation_failed"


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    generate_master_key(state_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(state_root))
    engine = create_database_engine(f"sqlite:///{tmp_path / 'recipe.db'}")
    metadata.create_all(engine)
    return create_session_factory(engine)


def _assembly(image_id: str = IMAGE, generation: str = "matrix") -> AssemblyResult:
    reviewed = load_reviewed_recipe(generation)
    return AssemblyResult(
        image_id=image_id,
        image_size=1_050_650_811,
        elapsed_seconds=115.28,
        build_log_digest="sha256:" + "b" * 64,
        build_log_bytes=124_319,
        apt_index_download="34.2 MB",
        apt_package_download="144 MB",
        package_inventory=(
            *tuple(
                (item.name, item.version, item.architecture)
                for item in reviewed.recipe.critical_packages
            ),
            ("libaudit-transitive", "1.0-1", "amd64"),
        ),
        apt_metadata_digests=tuple(
            (
                f"archive.ubuntu.com_ubuntu_dists_{pocket}_InRelease",
                "sha256:" + "c" * 64,
            )
            for pocket in reviewed.recipe.provider.pockets
        ),
    )


def _validation(image_id: str = IMAGE, generation: str = "matrix") -> ValidationResult:
    reviewed = load_reviewed_recipe(generation)
    now = datetime.now(UTC)
    return ValidationResult(
        started_at=now,
        completed_at=now,
        report={
            "schema": reviewed.recipe.validation_contract,
            "result": "passed",
            "recipe_digest": reviewed.digest,
            "release": reviewed.recipe.kodi_version,
            "image_id": image_id,
            "network_mode": "isolated_internal_bridge",
            "non_root_uid": 1000,
            "jsonrpc": True,
            "navigation": True,
            "library_query": True,
            "screenshot": {
                "width": 1280,
                "height": 720,
                "software_renderer": "llvmpipe",
                "png_bytes": 100,
                "png_sha256": "sha256:" + "d" * 64,
            },
            "cycles": [
                {"cycle": number, "readiness": True, "clean_stop": True}
                for number in (1, 2)
            ],
        },
    )


def _ready(
    factory: sessionmaker[Session], image_id: str = IMAGE, generation: str = "matrix"
) -> UUID:
    reviewed = load_reviewed_recipe(generation)
    operation_id = uuid4()
    _create_operation(factory, reviewed, operation_id)
    from kodi_lab.domain import RuntimeRecipeInstallationState as State

    for state in (State.PENDING, State.ACQUIRING, State.ASSEMBLING):
        _advance(factory, operation_id, expected=state)
    _commit_ready(
        factory,
        reviewed,
        operation_id,
        _assembly(image_id, generation),
        _validation(image_id, generation),
        data_root=Settings().data_root,
    )
    return operation_id


def test_ready_novnc_candidate_requires_explicit_selection_policy(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.runtime_matrix import RuntimeMatrixService

    reviewed = load_novnc_phase1_recipe()
    monkeypatch.setitem(globals(), "load_reviewed_recipe", lambda _: reviewed)
    operation = _ready(factory, generation="omega")
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        assert row.recipe_revision == 4
        revision = RuntimeRevisionRepository(session).get(UUID(row.runtime_revision_id))
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        assert revision is not None and definition is not None
        policy = RuntimeRevisionEligibilityPolicy()
        assert not policy.evaluate_for_release(
            session,
            required_release=definition.kodi_release,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(),
        ).eligible
        assert policy.evaluate_for_release(
            session,
            required_release=definition.kodi_release,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(novnc_phase1=True),
        ).eligible
        assert supports_reviewed_recipe_capability(session, revision.id, "screenshot")
    matrix = RuntimeMatrixService(factory).list_matrix()
    candidate = next(
        x
        for x in matrix.runtimes
        if x.runtime_definition_id == reviewed.recipe.definition_id
    )
    assert not candidate.selectable and candidate.selected_revision_number is None

    # Existing agent capture resolves the sealed runtime geometry without any
    # viewer, private display lane, or viewer authority object.
    from kodi_lab.broker.screenshots import SqliteScreenshotCaptureResolver
    from kodi_lab.kodi.screenshots import prepare_capture_stage

    bootstrap_recipe_instance(factory, reviewed, revision.id)
    capture = uuid4()
    prepare_capture_stage(Settings().data_root, capture)
    resolved = SqliteScreenshotCaptureResolver(
        factory, data_root=Settings().data_root
    ).resolve(str(recipe_target_id(reviewed)), str(revision.id), str(capture))
    assert resolved.experimental_image_id == IMAGE


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


def _assert_recipe_ineligible(
    factory: sessionmaker[Session], operation_id: UUID
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).ready_for_revision(
            UUID(
                str(
                    session.query(RuntimeRecipeInstallationRow.runtime_revision_id)
                    .filter_by(id=str(operation_id))
                    .scalar()
                )
            )
        )
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
    assert _ready_installation(factory, reviewed) == {}


@pytest.mark.parametrize(
    "case",
    [
        "signed_metadata_digest",
        "transitive_inventory",
        "critical_package_hash",
        "recipe_digest",
        "base_image_identity",
        "image_identity",
        "validation_contract",
        "evidence_identity",
        "operation_identity",
        "missing_binding",
        "malformed_binding",
        "wrong_binding",
    ],
)
def test_valid_shape_persisted_provenance_tamper_fails_closed(
    factory: sessionmaker[Session], case: str
) -> None:
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None and row.attestation_json and row.validation_json
        attestation = json.loads(row.attestation_json)
        validation = json.loads(row.validation_json)
        if case == "signed_metadata_digest":
            attestation["signed_metadata_digests"][0][1] = "sha256:" + "f" * 64
        elif case == "transitive_inventory":
            attestation["installed_packages"][-1][1] = "1.0-2"
            attestation["installed_packages_digest"] = installed_package_digest(
                attestation["installed_packages"]
            )
        elif case == "critical_package_hash":
            attestation["critical_packages"][0]["sha256"] = "sha256:" + "f" * 64
        elif case == "recipe_digest":
            attestation["recipe_digest"] = "sha256:" + "f" * 64
        elif case == "base_image_identity":
            attestation["base_image_reference"] = "ubuntu@sha256:" + "f" * 64
        elif case == "image_identity":
            attestation["image_id"] = "sha256:" + "f" * 64
        elif case == "validation_contract":
            validation["schema"] = "kodi-runtime-validation-other"
        elif case == "evidence_identity":
            evidence = session.query(RuntimeValidationEvidenceRow).one()
            evidence.id = str(uuid4())
        elif case == "operation_identity":
            # The row ID is validly shaped and still references the same revision.
            row.id = str(uuid4())
            evidence = session.query(RuntimeValidationEvidenceRow).one()
            evidence.evidence_reference = (
                f"kodi-lab-db:runtime-recipe-installation/{row.id}#validation"
            )
        elif case == "missing_binding":
            del attestation["integrity_binding"]
        elif case == "malformed_binding":
            attestation["integrity_binding"] = "hmac-sha256:xyz"
        else:
            attestation["integrity_binding"] = "hmac-sha256:" + "f" * 64
        row.attestation_json = canonical_json(attestation)
        row.validation_json = canonical_json(validation)
    if case != "operation_identity":
        _assert_recipe_ineligible(factory, operation_id)
    else:
        # The operation lookup is gone; selecting the ready row still fails.
        assert _ready_installation(factory, load_reviewed_recipe("matrix")) == {}


@pytest.mark.parametrize("case", ["wrong_key", "corrupt_key", "missing_key"])
def test_integrity_key_failure_fails_closed(
    factory: sessionmaker[Session], case: str
) -> None:
    operation_id = _ready(factory)
    key_path = Settings().data_root / "master-key"
    if case == "missing_key":
        key_path.unlink()
    elif case == "corrupt_key":
        key_path.write_text("not-a-key\n", encoding="ascii")
    else:
        key_path.write_bytes(base64.urlsafe_b64encode(os.urandom(32)) + b"\n")
    _assert_recipe_ineligible(factory, operation_id)


def test_missing_integrity_key_rolls_back_ready_transition(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = uuid4()
    _create_operation(factory, reviewed, operation_id)
    from kodi_lab.domain import RuntimeRecipeInstallationState as State

    for state in (State.PENDING, State.ACQUIRING, State.ASSEMBLING):
        _advance(factory, operation_id, expected=state)
    (Settings().data_root / "master-key").unlink()
    with pytest.raises(AttestationIntegrityError):
        _commit_ready(
            factory,
            reviewed,
            operation_id,
            _assembly(),
            _validation(),
            data_root=Settings().data_root,
        )
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None and row.state is State.VALIDATING
        assert (
            RuntimeRevisionRepository(session).list_for_definition(
                reviewed.recipe.definition_id
            )
            == []
        )


def test_malformed_persisted_operation_identity_fails_closed(
    factory: sessionmaker[Session],
) -> None:
    _ready(factory)
    with factory.begin() as session:
        row = session.query(RuntimeRecipeInstallationRow).one()
        row.id = "malformed-operation-id"
        evidence = session.query(RuntimeValidationEvidenceRow).one()
        evidence.evidence_reference = (
            "kodi-lab-db:runtime-recipe-installation/malformed-operation-id#validation"
        )
    assert _ready_installation(factory, load_reviewed_recipe("matrix")) == {}


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


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("base_image_reference", "ubuntu:unreviewed"),
        ("platform", "linux/arm64"),
        ("critical_packages", []),
        ("installed_packages", []),
        ("signed_metadata_digests", []),
    ],
)
def test_corrupted_ready_attestation_is_ineligible_and_not_reused(
    factory: sessionmaker[Session], field: str, replacement: object
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None and row.attestation_json is not None
        attestation = json.loads(row.attestation_json)
        attestation[field] = replacement
        row.attestation_json = json.dumps(attestation)
    assert _ready_installation(factory, reviewed) == {}
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


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema", "old-contract"),
        ("release", "19.5"),
        ("cycles", []),
        ("screenshot", None),
    ],
)
def test_corrupted_validation_report_is_ineligible(
    factory: sessionmaker[Session], field: str, replacement: object
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None and row.validation_json is not None
        validation = json.loads(row.validation_json)
        validation[field] = replacement
        row.validation_json = json.dumps(validation)
    assert _ready_installation(factory, reviewed) == {}


@pytest.mark.parametrize("field", ["evidence_reference", "started_at", "recorded_at"])
def test_stale_or_rebound_validation_evidence_is_ineligible(
    factory: sessionmaker[Session], field: str
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        assert row is not None and row.runtime_revision_id is not None
        evidence = (
            session.query(RuntimeValidationEvidenceRow)
            .filter_by(runtime_revision_id=row.runtime_revision_id)
            .one()
        )
        if field == "evidence_reference":
            evidence.evidence_reference = (
                f"kodi-lab-db:runtime-recipe-installation/{uuid4()}#validation"
            )
        elif field == "started_at":
            evidence.started_at = "2020-01-01T00:00:00.000000Z"
        else:
            evidence.recorded_at = "2099-01-01T00:00:00.000000Z"
    assert _ready_installation(factory, reviewed) == {}


def test_corrupted_installation_timestamp_is_ineligible(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    operation_id = _ready(factory)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None
        row.created_at = "2026-01-01T00:00:00.000000"
    assert _ready_installation(factory, reviewed) == {}


def test_reinstallation_creates_new_artifact_without_rebinding_existing_pin(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    first = _ready(factory)
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(first)
        assert row is not None and row.runtime_revision_id is not None
        first_revision_id = UUID(row.runtime_revision_id)
    assert (
        bootstrap_recipe_instance(factory, reviewed, first_revision_id)["created"]
        == "true"
    )
    replacement_image = "sha256:" + "e" * 64
    second = _ready(factory, replacement_image)
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(second)
        assert row is not None and row.runtime_revision_id is not None
        second_revision_id = UUID(row.runtime_revision_id)
        assert second_revision_id != first_revision_id
        revisions = RuntimeRevisionRepository(session).list_for_definition(
            reviewed.recipe.definition_id
        )
        assert [(item.revision_number, item.content_digest) for item in revisions] == [
            (1, IMAGE),
            (2, replacement_image),
        ]
    assert (
        bootstrap_recipe_instance(factory, reviewed, second_revision_id)["created"]
        == "false"
    )
    with factory() as session:
        instance = ManagedInstanceRepository(session).get(recipe_target_id(reviewed))
        assert instance is not None
        assert instance.runtime_revision_id == first_revision_id


def test_explicit_repin_selects_only_latest_ready_reviewed_revision(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    first = _ready(factory)
    with factory() as session:
        first_row = RuntimeRecipeInstallationRepository(session).get(first)
        assert first_row is not None and first_row.runtime_revision_id is not None
        first_revision_id = UUID(first_row.runtime_revision_id)
    bootstrap_recipe_instance(factory, reviewed, first_revision_id)
    second = _ready(factory, "sha256:" + "e" * 64)
    with factory() as session:
        second_row = RuntimeRecipeInstallationRepository(session).get(second)
        assert second_row is not None and second_row.runtime_revision_id is not None
        second_revision_id = UUID(second_row.runtime_revision_id)

    with pytest.raises(LabWorkflowError, match="exact ready recipe revision"):
        repin_reviewed_runtime(factory, "matrix", first_revision_id)
    with factory() as session:
        instance = ManagedInstanceRepository(session).get(recipe_target_id(reviewed))
        assert instance is not None
        assert instance.runtime_revision_id == first_revision_id

    assert repin_reviewed_runtime(factory, "matrix", second_revision_id) == (
        second_revision_id
    )
    with factory() as session:
        instance = ManagedInstanceRepository(session).get(recipe_target_id(reviewed))
        assert instance is not None
        assert instance.runtime_revision_id == second_revision_id
    with pytest.raises(LabWorkflowError, match="already pinned"):
        repin_reviewed_runtime(factory, "matrix", second_revision_id)


def test_interrupted_validating_attempt_is_failed_before_new_pending_attempt(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_reviewed_recipe("matrix")
    first = uuid4()
    _create_operation(factory, reviewed, first)
    from kodi_lab.domain import RuntimeRecipeInstallationState as State

    for state in (State.PENDING, State.ACQUIRING, State.ASSEMBLING):
        _advance(factory, first, expected=state)
    second = uuid4()
    _create_operation(factory, reviewed, second)
    with factory() as session:
        old = RuntimeRecipeInstallationRepository(session).get(first)
        current = RuntimeRecipeInstallationRepository(session).get(second)
        assert old is not None and old.state is State.FAILED
        assert old.failure_code == "interrupted"
        assert current is not None and current.state is State.PENDING
        assert (
            RuntimeRevisionRepository(session).list_for_definition(
                reviewed.recipe.definition_id
            )
            == []
        )


@pytest.mark.parametrize(
    "case",
    (
        "critical_package",
        "platform",
        "release",
        "image",
        "validation_evidence",
        "hmac",
    ),
)
def test_nexus_reviewed_attestation_tamper_fails_closed(
    factory: sessionmaker[Session], case: str
) -> None:
    reviewed = load_reviewed_recipe("nexus")
    operation_id = _ready(factory, generation="nexus")
    assert _ready_installation(factory, reviewed)["operation_id"] == str(operation_id)
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation_id))
        assert row is not None and row.attestation_json and row.validation_json
        attestation = json.loads(row.attestation_json)
        validation = json.loads(row.validation_json)
        if case == "critical_package":
            attestation["critical_packages"][0]["sha256"] = "sha256:" + "f" * 64
        elif case == "platform":
            attestation["platform"] = "linux/arm64"
        elif case == "release":
            attestation["kodi_version"] = "20.4"
        elif case == "image":
            attestation["image_id"] = "sha256:" + "f" * 64
        elif case == "validation_evidence":
            validation["recipe_digest"] = "sha256:" + "f" * 64
        else:
            attestation["integrity_binding"] = "hmac-sha256:" + "f" * 64
        row.attestation_json = canonical_json(attestation)
        row.validation_json = canonical_json(validation)
    assert _ready_installation(factory, reviewed) == {}


def test_matrix_and_nexus_ready_records_are_separate(
    factory: sessionmaker[Session],
) -> None:
    matrix = load_reviewed_recipe("matrix")
    nexus = load_reviewed_recipe("nexus")
    matrix_operation = _ready(factory)
    nexus_image = "sha256:" + "e" * 64
    nexus_operation = _ready(factory, nexus_image, generation="nexus")
    matrix_ready = _ready_installation(factory, matrix)
    nexus_ready = _ready_installation(factory, nexus)
    assert matrix_ready["operation_id"] == str(matrix_operation)
    assert nexus_ready["operation_id"] == str(nexus_operation)
    assert matrix_ready["runtime_revision_id"] != nexus_ready["runtime_revision_id"]
    assert matrix_ready["image_id"] != nexus_ready["image_id"]
    with factory() as session:
        matrix_revision = RuntimeRevisionRepository(session).get(
            UUID(matrix_ready["runtime_revision_id"])
        )
        nexus_revision = RuntimeRevisionRepository(session).get(
            UUID(nexus_ready["runtime_revision_id"])
        )
        assert matrix_revision is not None and nexus_revision is not None
        assert matrix_revision.runtime_definition_id == matrix.recipe.definition_id
        assert nexus_revision.runtime_definition_id == nexus.recipe.definition_id


def test_experimental_bootstrap_never_reuses_normal_omega_target_identity() -> None:
    normal = load_reviewed_recipe("omega")
    experimental = load_novnc_phase1_recipe()
    assert recipe_target_id(normal) != recipe_target_id(experimental)
    assert normal.recipe.definition_id == experimental.recipe.definition_id


def test_normal_and_experimental_omega_registrations_coexist_without_repinning(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    normal = load_reviewed_recipe("omega")
    operation = _ready(factory, generation="omega")
    with factory() as db:
        installation = RuntimeRecipeInstallationRepository(db).get(operation)
        assert installation is not None and installation.runtime_revision_id is not None
        normal_revision = UUID(installation.runtime_revision_id)
    bootstrap_recipe_instance(factory, normal, normal_revision)
    experimental = load_novnc_phase1_recipe()
    monkeypatch.setitem(globals(), "load_reviewed_recipe", lambda _: experimental)
    operation = _ready(factory, image_id="sha256:" + "b" * 64, generation="omega")
    with factory() as db:
        installation = RuntimeRecipeInstallationRepository(db).get(operation)
        assert installation is not None and installation.runtime_revision_id is not None
        experimental_revision = UUID(installation.runtime_revision_id)
    bootstrap_recipe_instance(factory, experimental, experimental_revision)
    with factory() as db:
        ordinary = ManagedInstanceRepository(db).get(recipe_target_id(normal))
        viewer = ManagedInstanceRepository(db).get(recipe_target_id(experimental))
        assert ordinary is not None and viewer is not None
        assert ordinary.runtime_revision_id == normal_revision
        assert viewer.runtime_revision_id == experimental_revision
        assert len(ManagedInstanceRepository(db).list()) == 2

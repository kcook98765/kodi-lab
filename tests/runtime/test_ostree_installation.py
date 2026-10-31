"""Deterministic admission, provenance, HMAC and exact Preview regressions.

The small fixture is real retained evidence, not a new cryptographic test.
Image assembly and validation doubles here do not claim live installation.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.broker.operations import ReviewedInstallationLaunchParams
from kodi_lab.config import Settings
from kodi_lab.domain import (
    KodiReleaseIdentity,
    RuntimeRecipeInstallationState,
    RuntimeSupportTier,
)
from kodi_lab.runtime.install import (
    RecipeInstallationError,
    _advance,
    _commit_ready,
    _create_operation,
    acquisition_attestation,
    bootstrap_recipe_instance,
)
from kodi_lab.runtime.ostree import OstreeVerificationEvidence
from kodi_lab.runtime.ostree_assembly import OstreeAssemblyEvidence
from kodi_lab.runtime.ostree_installation import (
    OstreeInstallationAssembly,
    ostree_attestation_matches,
)
from kodi_lab.runtime.ostree_validation import native_identity
from kodi_lab.runtime.preview import PreviewAcknowledgement, admission_evidence
from kodi_lab.runtime.recipes import (
    PREVIEW_INSTALLATION_DIGEST,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
    load_reviewed_recipe,
    supported_generations,
)
from kodi_lab.runtime.validation import RecipeValidationError, ValidationResult
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
)
from tests.runtime.test_recipe_installation import factory as factory


def acknowledgement() -> PreviewAcknowledgement:
    return PreviewAcknowledgement(
        recipe_id="piers-22.0-rc1-r2",
        recipe_revision=2,
        recipe_digest=PREVIEW_INSTALLATION_DIGEST,
        release="22.0-rc.1",
        support_tier="preview",
        acknowledgement="development-only-prerelease",
    )


def assembly(operation: UUID) -> OstreeInstallationAssembly:
    data = json.loads(
        (Path(__file__).parent / "fixtures/rc1-installation-evidence.json").read_text()
    )
    raw = data["assembly"]
    raw["operation_id"] = operation.hex
    raw["recipe_revision"] = 2
    # Derive current synthetic assembly from retained original evidence.
    # The historical evidence file remains unchanged.
    from kodi_lab.runtime.ostree_assembly import ASSETS_SHA256, load_assembly_assets
    from kodi_lab.runtime.ubuntu_support import load_support_closure

    raw["supporting_closure_digest"] = load_support_closure().digest
    raw["assembly_assets_digest"] = "sha256:" + ASSETS_SHA256
    raw["asset_hashes"] = sorted(
        (name, hashlib.sha256(payload).hexdigest())
        for name, payload in load_assembly_assets().items()
    )
    names = {f.name for f in fields(OstreeAssemblyEvidence)}
    for key in ("asset_hashes", "input_components"):
        raw[key] = tuple(tuple(row) for row in raw[key])
    return OstreeInstallationAssembly(
        OstreeAssemblyEvidence(**{k: v for k, v in raw.items() if k in names}),
        OstreeVerificationEvidence.model_validate(data["acquisition"]),
        acknowledgement(),
    )


def validation(image_id: str) -> ValidationResult:
    from tests.runtime.test_recipe_installation import _validation

    reviewed = load_installation_recipe("piers")
    base = _validation(image_id)
    report = deepcopy(base.report)
    report.update(
        recipe_digest=reviewed.digest,
        release="22.0-rc.1",
        native_identity={
            "name": "Kodi",
            "version": {
                "major": 22,
                "minor": 0,
                "tag": "releasecandidate",
                "tagversion": "1",
                "revision": "20260927-28ea2eac-dirty",
            },
        },
        python="3.14.7",
        python_api="3.1.0",
        ffmpeg="9.0.2",
        renderer="llvmpipe",
        windowing="x11",
        security_contract="nonroot-capless-seccomp-v1",
        profile="/home/kodi/.kodi",
        profile_restart=True,
        framebuffer_change=True,
        session_dbus=True,
        stale_display_absent=True,
    )
    now = datetime.now(UTC)
    return ValidationResult(now, now, report)


def ready(factory: sessionmaker[Session]) -> UUID:
    reviewed = load_installation_recipe("piers")
    operation = uuid4()
    _create_operation(factory, reviewed, operation)
    built = assembly(operation)
    for state in (
        RuntimeRecipeInstallationState.PENDING,
        RuntimeRecipeInstallationState.ACQUIRING,
        RuntimeRecipeInstallationState.ASSEMBLING,
    ):
        _advance(factory, operation, expected=state)
    _commit_ready(
        factory,
        reviewed,
        operation,
        built,
        validation(built.image_id),
        data_root=Settings().data_root,
    )
    return operation


def test_exact_backend_recipe_does_not_change_web_stable_registry() -> None:
    reviewed = load_installation_recipe("piers")
    assert reviewed.digest == PREVIEW_INSTALLATION_DIGEST
    assert reviewed.recipe.kodi_version == "22.0-rc.1"
    assert supported_generations() == ("matrix", "nexus", "omega")
    with pytest.raises(ValueError):
        load_reviewed_recipe("piers")
    assert (
        load_recorded_reviewed_recipe("piers", "piers-22.0-rc1-r2", 2, reviewed.digest)
        == reviewed
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipe_digest", "sha256:" + "f" * 64),
        ("release", "22.0-rc.2"),
        ("release", "22.0"),
        ("recipe_revision", 1),
        ("support_tier", "current"),
        ("recipe_id", "piers-22.0-rc2-r1"),
        ("acknowledgement", "all-prereleases"),
        ("url", "https://attacker.invalid"),
    ],
)
def test_acknowledgement_is_exact(field: str, value: object) -> None:
    data = acknowledgement().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        PreviewAcknowledgement.model_validate(data)


@pytest.mark.parametrize(
    "generation,ack",
    [("piers", None), ("unknown", None), ("piers-rc2", None), ("omega", True)],
)
def test_broker_closed_selector_and_scope(generation: str, ack: bool | None) -> None:
    with pytest.raises(ValidationError):
        ReviewedInstallationLaunchParams.model_validate(
            {
                "generation": generation,
                "operation_id": str(uuid4()),
                "preview_acknowledgement": acknowledgement().model_dump()
                if ack
                else None,
            }
        )


def test_preview_is_sealed_exact_and_not_automatically_provisioned(
    factory: sessionmaker[Session],
) -> None:
    operation = ready(factory)
    reviewed = load_installation_recipe("piers")
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row and row.runtime_revision_id
        revision = RuntimeRevisionRepository(session).get(UUID(row.runtime_revision_id))
        assert revision
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        assert definition
        assert definition.support_tier is RuntimeSupportTier.PREVIEW
        policy = RuntimeRevisionEligibilityPolicy()
        args: dict[str, Any] = dict(
            required_release=definition.kodi_release,
            definition=definition,
            revision=revision,
        )
        assert not policy.evaluate_for_release(
            session, **args, policy=RuntimeSelectionPolicy()
        ).eligible
        assert not policy.evaluate_for_release(
            session, **args, policy=RuntimeSelectionPolicy(include_prerelease=True)
        ).eligible
        assert policy.evaluate_for_release(
            session,
            **args,
            policy=RuntimeSelectionPolicy(
                include_prerelease=True, preview_recipe_digest=reviewed.digest
            ),
        ).eligible
        assert not policy.evaluate_for_release(
            session,
            **args,
            policy=RuntimeSelectionPolicy(
                include_prerelease=True, preview_recipe_digest="sha256:" + "f" * 64
            ),
        ).eligible
        assert "hmac-sha256:" in (row.attestation_json or "")
        revision_id = revision.id
    assert bootstrap_recipe_instance(factory, reviewed, revision_id) == {
        "created": "false"
    }
    with factory() as session:
        assert ManagedInstanceRepository(session).list() == []


@pytest.mark.parametrize(
    "path,value",
    [
        (("recipe_digest",), "sha256:" + "f" * 64),
        (("release",), "22.0-rc.2"),
        (("release",), "22.0"),
        (("image_id",), "sha256:" + "f" * 64),
        (("support_tier",), "current"),
        (("acquisition", "kodi_source_revision"), "f" * 40),
        (("acquisition", "ostree_integrity"), "failed"),
        (("acquisition", "components", 0, "primary_key"), "F" * 40),
        (("acquisition", "components", 0, "signature_result"), "missing"),
        (("acquisition", "components", 0, "commit"), "f" * 64),
        (("assembly", "supporting_closure_digest"), None),
        (("assembly", "assembly_assets_digest"), "sha256:" + "f" * 64),
        (("assembly", "asset_hashes"), []),
        (("assembly", "image_id"), "sha256:" + "f" * 64),
        (("assembly", "input_components"), []),
        (("assembly", "acquisition_digest"), "sha256:" + "f" * 64),
        (("preview_acknowledgement", "release"), "22.0-rc.2"),
    ],
)
def test_changed_provenance_rejected(path: tuple[Any, ...], value: object) -> None:
    reviewed = load_installation_recipe("piers")
    built = assembly(uuid4())
    data = json.loads(acquisition_attestation(reviewed, built))
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert not ostree_attestation_matches(data, reviewed, built.image_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("python", "3.13.0"),
        ("python_api", "3.0.0"),
        ("ffmpeg", "8.0"),
        ("renderer", "hardware"),
        ("windowing", "wayland"),
        ("profile_restart", False),
        ("security_contract", None),
        ("session_dbus", False),
        ("framebuffer_change", False),
        ("stale_display_absent", False),
    ],
)
def test_missing_or_wrong_validation_never_approves(
    factory: sessionmaker[Session], field: str, value: object
) -> None:
    reviewed = load_installation_recipe("piers")
    operation = uuid4()
    built = assembly(operation)
    _create_operation(factory, reviewed, operation)
    for state in (
        RuntimeRecipeInstallationState.PENDING,
        RuntimeRecipeInstallationState.ACQUIRING,
        RuntimeRecipeInstallationState.ASSEMBLING,
    ):
        _advance(factory, operation, expected=state)
    checked = validation(built.image_id)
    checked.report[field] = value
    with pytest.raises(RecipeInstallationError):
        _commit_ready(
            factory, reviewed, operation, built, checked, data_root=Settings().data_root
        )
    with factory() as session:
        assert (
            RuntimeRevisionRepository(session).list_for_definition(
                reviewed.recipe.definition_id
            )
            == []
        )


@pytest.mark.parametrize(
    "tag,ordinal",
    [
        ("beta", "1"),
        ("releasecandidate", "2"),
        ("stable", None),
        ("releasecandidate", None),
        ("releasecandidate", "0"),
    ],
)
def test_native_rc2_final_or_malformed_identity_rejected(
    tag: str, ordinal: object
) -> None:
    with pytest.raises(RecipeValidationError):
        native_identity(
            {
                "name": "Kodi",
                "version": {
                    "major": 22,
                    "minor": 0,
                    "revision": "28ea2eac-dirty",
                    "tag": tag,
                    "tagversion": ordinal,
                },
            }
        )


def test_persisted_attestation_tamper_fails_hmac(
    factory: sessionmaker[Session],
) -> None:
    operation = ready(factory)
    reviewed = load_installation_recipe("piers")
    with factory.begin() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row and row.attestation_json
        data = json.loads(row.attestation_json)
        data["assembly"]["build_log_digest"] = "sha256:" + "f" * 64
        from kodi_lab.runtime.attestation_integrity import canonical_json

        row.attestation_json = canonical_json(data)
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row and row.runtime_revision_id
        revision = RuntimeRevisionRepository(session).get(UUID(row.runtime_revision_id))
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        assert revision and definition
        assert (
            not RuntimeRevisionEligibilityPolicy()
            .evaluate_for_release(
                session,
                required_release=KodiReleaseIdentity.parse("22.0-rc.1"),
                definition=definition,
                revision=revision,
                policy=RuntimeSelectionPolicy(
                    include_prerelease=True, preview_recipe_digest=reviewed.digest
                ),
            )
            .eligible
        )


def test_admission_preserves_accepted_pending_operation(
    factory: sessionmaker[Session],
) -> None:
    reviewed = load_installation_recipe("piers")
    operation = uuid4()
    _create_operation(factory, reviewed, operation)
    with factory.begin() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row
        row.attestation_json = admission_evidence(acknowledgement(), operation)
    _create_operation(factory, reviewed, operation)
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row and row.state.value == "pending"

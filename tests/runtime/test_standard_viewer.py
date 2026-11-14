"""Standard viewing requires installed seals at both admission boundaries."""

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import PreviewActionApproval
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.broker.novnc_service import HumanDisplayService
from kodi_lab.config import Settings
from kodi_lab.runtime.display_capabilities import (
    admitted_display_capability,
    reviewed_display_capability,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import (
    load_installation_recipe,
    load_novnc_phase1_recipe,
    recipe_target_id,
)
from kodi_lab.storage.models import (
    RuntimeContainerArtifactRow,
    RuntimeRecipeInstallationRow,
    RuntimeValidationEvidenceRow,
)
from kodi_lab.web.novnc_optin import build_viewer
from tests.runtime import test_recipe_installation as fixture
from tests.runtime.test_piers_display import ready_display

factory = fixture.factory


@pytest.mark.parametrize(
    "generation,revision", [("matrix", 4), ("nexus", 4), ("omega", 6), ("piers", 4)]
)
def test_fresh_selection_and_both_admission_layers_require_exact_seals(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
    revision: int,
) -> None:
    recipe = load_installation_recipe(generation)
    capability = reviewed_display_capability(recipe)
    assert recipe.recipe.recipe_revision == revision
    assert capability is not None and capability.qualification == "standard"
    operation = (
        ready_display(factory)[0]
        if generation == "piers"
        else fixture._ready(factory, generation=generation)
    )
    with factory() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        runtime = UUID(row.runtime_revision_id)
        image = row.image_id
        assert admitted_display_capability(db, runtime) == capability
    if generation == "piers":
        from types import SimpleNamespace

        from kodi_lab.application.runtime_installations import (
            RuntimeInstallationService,
            preview_acknowledgement,
        )
        from kodi_lab.runtime.preview import PreviewAcknowledgement
        from tests.application.test_piers_installation_admission import broker

        backend = broker()
        backend.reviewed_runtime_image_present = lambda _: SimpleNamespace(present=True)
        RuntimeInstallationService(factory, backend).create_preview_instance(
            approval=PreviewActionApproval(
                action="create",
                approved=True,
                acknowledgement=PreviewAcknowledgement.model_validate(
                    preview_acknowledgement()
                ),
            ),
        )
    else:
        bootstrap_recipe_instance(factory, recipe, runtime)
    target = recipe_target_id(recipe)
    settings = Settings()
    assert settings.experimental_novnc_instances == ()
    service = HumanDisplayService(settings, factory)
    assert service.qualified(target) == capability
    calls = []

    def binding(*args: object) -> ViewBinding:
        calls.append(args)
        assert image is not None
        return ViewBinding("a" * 64, image, "one-start", ())

    monkeypatch.setattr("kodi_lab.web.novnc_optin.resolve_binding", binding)
    viewer = build_viewer(settings, factory)
    try:
        assert viewer.require_https and viewer.enabled_instances is None
        assert viewer.authority(target).identity[0] == str(runtime)
        assert len(calls) == 1
        with factory.begin() as db:
            row = db.get(RuntimeRecipeInstallationRow, str(operation))
            assert row is not None
            row.validation_json = (row.validation_json or "") + " "
        with pytest.raises(ValueError, match="not qualified"):
            service.qualified(target)
        with pytest.raises(ValueError, match="not qualified"):
            viewer.authority(target)
        assert len(calls) == 1, "Invalid provenance must fail before private lookup"
    finally:
        viewer.close()


@pytest.mark.parametrize("drift", ["image", "validation", "helper", "candidate"])
def test_standard_metadata_cannot_bypass_installed_validation(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    recipe = load_installation_recipe("omega")
    operation = fixture._ready(factory, generation="omega")
    with factory.begin() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        runtime = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, recipe, runtime)
    with factory.begin() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None
        capability = admitted_display_capability(db, runtime)
        assert capability is not None
        if drift == "image":
            artifact = db.get(RuntimeContainerArtifactRow, str(runtime))
            assert artifact is not None
            artifact.image_id = "sha256:" + "f" * 64
        elif drift == "validation":
            evidence = (
                db.query(RuntimeValidationEvidenceRow)
                .filter_by(runtime_revision_id=str(runtime))
                .one()
            )
            db.delete(evidence)
        elif drift == "helper":
            row.attestation_json = (row.attestation_json or "").replace(
                capability.helper_digest, "sha256:" + "f" * 64
            )
            # Even when the acquisition report stores helper proof only inside
            # sealed validation, changing those bytes invalidates the HMAC.
            row.validation_json = (row.validation_json or "").replace(
                capability.helper_digest, "sha256:" + "f" * 64
            )
        else:
            monkeypatch.setattr(
                "kodi_lab.runtime.display_capabilities.installed_display_capability",
                lambda *args: replace(capability, qualification="candidate"),
            )
    with pytest.raises(ValueError, match="not qualified"):
        HumanDisplayService(Settings(), factory).qualified(recipe_target_id(recipe))


def test_historical_omega_r4_still_requires_explicit_optin(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = load_novnc_phase1_recipe()
    monkeypatch.setattr(fixture, "load_reviewed_recipe", lambda _: recipe)
    operation = fixture._ready(factory, generation="omega")
    with factory() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        runtime = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, recipe, runtime)
    target = recipe_target_id(recipe)
    with pytest.raises(ValueError, match="not qualified"):
        HumanDisplayService(Settings(), factory).qualified(target)
    assert (
        HumanDisplayService(Settings(experimental_novnc_instances=(target,)), factory)
        .qualified(target)
        .qualification
        == "experimental"
    )

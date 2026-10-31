"""Recipe metadata cannot substitute for sealed installed/native identity."""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.display_capabilities import (
    EXPERIMENTAL_OMEGA_DISPLAY,
    installed_display_capability,
    reviewed_display_capability,
)
from kodi_lab.runtime.recipes import load_novnc_phase1_recipe, load_reviewed_recipe
from kodi_lab.storage.models import (
    RuntimeContainerArtifactRow,
    RuntimeRecipeInstallationRow,
    RuntimeValidationEvidenceRow,
)
from tests.runtime import test_recipe_installation as fixture

factory = fixture.factory


def test_only_exact_experimental_recipe_has_display_packaging() -> None:
    reviewed = load_novnc_phase1_recipe()
    capability = reviewed_display_capability(reviewed)
    assert capability == EXPERIMENTAL_OMEGA_DISPLAY
    assert (
        capability.width,
        capability.height,
        capability.fps,
        capability.absolute_seconds,
    ) == (640, 360, 2, 600)
    assert capability.qualification == "experimental"
    for generation in ("matrix", "nexus", "omega"):
        assert reviewed_display_capability(load_reviewed_recipe(generation)) is None


@pytest.mark.parametrize(
    "change", ["id", "revision", "digest", "raw", "helper", "package", "geometry"]
)
def test_substituted_recipe_never_inherits_display_capability(change: str) -> None:
    reviewed = load_novnc_phase1_recipe()
    if change == "id":
        reviewed = replace(
            reviewed,
            recipe=reviewed.recipe.model_copy(update={"recipe_id": "substituted"}),
        )
    elif change == "revision":
        reviewed = replace(
            reviewed, recipe=reviewed.recipe.model_copy(update={"recipe_revision": 5})
        )
    elif change == "digest":
        reviewed = replace(reviewed, digest="sha256:" + "f" * 64)
    elif change == "raw":
        reviewed = replace(reviewed, raw=reviewed.raw + b"\n")
    elif change == "helper":
        reviewed = replace(
            reviewed,
            assets={
                k: v for k, v in reviewed.assets.items() if k != "novnc_view_helper.py"
            },
        )
    elif change == "package":
        reviewed = replace(
            reviewed,
            recipe=reviewed.recipe.model_copy(
                update={
                    "critical_packages": tuple(
                        p
                        for p in reviewed.recipe.critical_packages
                        if p.name != "tigervnc-scraping-server"
                    )
                }
            ),
        )
    else:
        reviewed = replace(
            reviewed,
            assets={
                **reviewed.assets,
                "entrypoint.sh": reviewed.assets["entrypoint.sh"].replace(
                    b"640x360", b"1280x720"
                ),
            },
        )
    assert reviewed_display_capability(reviewed) is None


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "missing",
        "revision",
        "digest",
        "image",
        "unvalidated",
        "geometry",
        "navigation",
        "package",
    ],
)
def test_installed_capability_requires_sealed_exact_revision(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    drift: str | None,
) -> None:
    reviewed = load_novnc_phase1_recipe()
    monkeypatch.setattr(fixture, "load_reviewed_recipe", lambda _: reviewed)
    operation = fixture._ready(factory, generation="omega")
    with factory.begin() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
        assert installed_display_capability(db, revision) == EXPERIMENTAL_OMEGA_DISPLAY
        if drift == "missing":
            revision = uuid4()
        elif drift == "revision":
            row.recipe_revision = 3
        elif drift == "digest":
            row.recipe_digest = "sha256:" + "f" * 64
        elif drift == "image":
            artifact = db.get(RuntimeContainerArtifactRow, str(revision))
            assert artifact is not None
            artifact.image_id = "sha256:" + "b" * 64
        elif drift == "unvalidated":
            db.execute(
                delete(RuntimeValidationEvidenceRow).where(
                    RuntimeValidationEvidenceRow.runtime_revision_id == str(revision)
                )
            )
        elif drift in ("geometry", "navigation"):
            report = json.loads(row.validation_json or "")
            if drift == "geometry":
                report["screenshot"]["width"] = 640
            else:
                report["navigation"] = False
            row.validation_json = json.dumps(report)
        elif drift == "package":
            report = json.loads(row.attestation_json or "")
            report["installed_packages"] = [
                p
                for p in report["installed_packages"]
                if p[0] != "tigervnc-scraping-server"
            ]
            row.attestation_json = json.dumps(report)
        db.flush()
        assert (installed_display_capability(db, revision) is not None) == (
            drift is None
        )

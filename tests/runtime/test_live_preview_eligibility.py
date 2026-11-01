"""Preview has a separate closed recipe gate; screenshots do not grant streaming."""

import json
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.live_preview import reviewed_live_preview_prerequisites
from kodi_lab.runtime.recipes import load_recorded_reviewed_recipe, load_reviewed_recipe
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from tests.runtime import test_recipe_installation as recipe_fixture
from tests.runtime.test_recipe_installation import _ready
from tests.runtime.test_recipe_installation import factory as factory


def historical(monkeypatch: pytest.MonkeyPatch, generation: str) -> None:
    current = load_reviewed_recipe(generation)
    digest = (
        "sha256:"
        + {
            "matrix": (
                "2f962f50580c53e9aa98447975617b2bc18552f6d8a7e645f07d90fa19641e70"
            ),
            "nexus": "9845a930212f91c20e1313f03cc289882b8555f6067a5bf22e11e724b9821631",
            "omega": "4cb4a003da6d119018738db06aed6789c0acaee2b5ed89b210b93db5526d7140",
        }[generation]
    )
    old = load_recorded_reviewed_recipe(generation, current.recipe.recipe_id, 2, digest)
    monkeypatch.setattr(recipe_fixture, "load_reviewed_recipe", lambda _: old)


@pytest.mark.parametrize("generation", ["omega", "nexus", "matrix"])
def test_actual_completed_recipe_prerequisites_remain_separate_from_screenshots(
    factory: sessionmaker[Session], generation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical(monkeypatch, generation)
    operation = _ready(factory, generation=generation)
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
        assert supports_reviewed_recipe_capability(session, revision, "screenshot")
        assert reviewed_live_preview_prerequisites(session, revision) is (
            generation in {"omega", "nexus"}
        )
    # This only admits an explicit native probe, never an enabled policy grant.


@pytest.mark.parametrize(
    "drift",
    [
        "recipe_id",
        "recipe_revision",
        "recipe_digest",
        "image",
        "navigation",
        "screenshot",
    ],
)
def test_reviewed_preview_prerequisites_reject_nearby_or_revoked_evidence(
    factory: sessionmaker[Session], drift: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical(monkeypatch, "omega")
    operation = _ready(factory, generation="omega")
    with factory.begin() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
        assert reviewed_live_preview_prerequisites(session, revision)
        # Fixture metadata database has no production immutability triggers.
        if drift == "recipe_id":
            row.recipe_id = "unreviewed-runtime"
        elif drift == "recipe_revision":
            row.recipe_revision += 1
        elif drift == "recipe_digest":
            row.recipe_digest = "sha256:" + "f" * 64
        elif drift == "image":
            row.image_id = "sha256:" + "f" * 64
        else:
            report = json.loads(str(row.validation_json))
            if drift == "navigation":
                report["navigation"] = False
            else:
                report["screenshot"]["width"] = 640
            row.validation_json = json.dumps(report)
    with factory() as session:
        assert not reviewed_live_preview_prerequisites(session, revision)


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_standard_novnc_does_not_grant_unqualified_jpeg_preview(
    factory: sessionmaker[Session], generation: str
) -> None:
    from kodi_lab.runtime.display_capabilities import installed_display_capability

    operation = _ready(factory, generation=generation)
    with factory() as session:
        row = session.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
        capability = installed_display_capability(session, revision)
        assert capability is not None and capability.qualification == "standard"
        assert not reviewed_live_preview_prerequisites(session, revision)

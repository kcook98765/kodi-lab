"""Preview has a separate closed recipe gate; screenshots do not grant streaming."""

import json
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.runtime.live_preview import reviewed_live_preview_prerequisites
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from tests.runtime.test_recipe_installation import _ready
from tests.runtime.test_recipe_installation import factory as factory


@pytest.mark.parametrize("generation", ["omega", "nexus", "matrix"])
def test_actual_completed_recipe_prerequisites_remain_separate_from_screenshots(
    factory: sessionmaker[Session], generation: str
) -> None:
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
    factory: sessionmaker[Session], drift: str
) -> None:
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

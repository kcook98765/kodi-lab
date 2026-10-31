"""Noble packaging is exact and remains separate from standard activation."""

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.novnc_service import HumanDisplayService
from kodi_lab.config import Settings
from kodi_lab.runtime.display_capabilities import (
    installed_display_capability,
    reviewed_display_capability,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import (
    load_display_candidate_recipe,
    load_reviewed_recipe,
    recipe_target_id,
)
from kodi_lab.runtime.validation import ValidationResult
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from kodi_lab.storage.repositories import ManagedInstanceRepository
from tests.runtime import test_recipe_installation as fixture
from tests.runtime.test_display_candidate import proof

factory = fixture.factory


def test_nexus_r3_preserves_kodi_pins_and_signed_distro_identity() -> None:
    old = load_reviewed_recipe("nexus")
    candidate = load_display_candidate_recipe("nexus")
    assert old.recipe.recipe_revision == 2
    assert candidate.recipe.recipe_revision == 3
    assert candidate.recipe.kodi_version == "20.5"
    assert candidate.recipe.provider == old.recipe.provider
    assert candidate.recipe.base_image_reference == old.recipe.base_image_reference
    assert candidate.recipe.critical_packages[:-1] == old.recipe.critical_packages
    tiger = candidate.recipe.critical_packages[-1]
    assert (tiger.name, tiger.version, tiger.sha256) == (
        "tigervnc-scraping-server",
        "1.13.1+dfsg-2build2",
        "sha256:f3844df856a95e4b306a61c03fa75433599d247ed2f9551df15003b191a8d054",
    )
    for name, payload in old.assets.items():
        if name != "entrypoint.sh":
            assert candidate.assets[name] == payload
    assert b"640x360x24" in candidate.assets["entrypoint.sh"]
    assert b"LP_NUM_THREADS=1 dbus-run-session" in candidate.assets["entrypoint.sh"]
    assert (
        candidate.assets["novnc_view_helper.py"]
        == load_display_candidate_recipe("omega").assets["novnc_view_helper.py"]
    )
    assert recipe_target_id(candidate) == recipe_target_id(old)
    capability = reviewed_display_capability(candidate)
    assert capability is not None and capability.qualification == "candidate"
    assert reviewed_display_capability(replace(candidate, recipe=old.recipe)) is None


def test_sealed_nexus_candidate_preserves_old_pin_and_stays_unactivated(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = load_reviewed_recipe("nexus")
    operation = fixture._ready(factory, generation="nexus")
    with factory() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        old_revision = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, old, old_revision)
    candidate = load_display_candidate_recipe("nexus")
    capability = reviewed_display_capability(candidate)
    assert capability is not None
    monkeypatch.setattr(fixture, "load_reviewed_recipe", lambda _: candidate)
    original = fixture._validation

    def validation(
        image_id: str = fixture.IMAGE, generation: str = "nexus"
    ) -> ValidationResult:
        result = original(image_id, generation)
        return replace(
            result,
            report={
                **result.report,
                "display": {**proof(), "package": list(capability.package)},
            },
        )

    monkeypatch.setattr(fixture, "_validation", validation)
    operation = fixture._ready(
        factory, image_id="sha256:" + "b" * 64, generation="nexus"
    )
    with factory() as db:
        row = db.get(RuntimeRecipeInstallationRow, str(operation))
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
        assert installed_display_capability(db, revision) == capability
    bootstrap_recipe_instance(factory, candidate, revision)
    target = recipe_target_id(candidate)
    with factory() as db:
        instance = ManagedInstanceRepository(db).get(target)
        assert instance is not None and instance.runtime_revision_id == old_revision
    monkeypatch.setattr(
        "docker.APIClient",
        lambda **kwargs: pytest.fail("candidate must remain unactivated"),
    )
    with pytest.raises(ValueError, match="not qualified"):
        HumanDisplayService(
            Settings(experimental_novnc_instances=(target,)), factory
        ).resolve(target)

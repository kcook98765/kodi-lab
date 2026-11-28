"""The sealed Omega pilot shares OSTree without changing default selection."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import preview_acknowledgement
from kodi_lab.broker.operations import ReviewedInstallationLaunchParams
from kodi_lab.runtime.display_capabilities import reviewed_display_capability
from kodi_lab.runtime.ostree_validation import (
    native_executable_matches,
    native_identity,
)
from kodi_lab.runtime.preview import (
    PreviewAcknowledgement,
    admission_evidence,
    recorded_acknowledgement,
    reviewed_preview_recipe,
)
from kodi_lab.runtime.recipes import (
    InactiveOstreeRecipe,
    load_inactive_ostree_recipe,
    load_installation_recipe,
    load_omega_flathub_pilot,
    selected_installation_recipe,
)
from kodi_lab.runtime.validation import RecipeValidationError
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.runtime.test_recipe_installation import factory as factory


def test_pilot_uses_distinct_artifacts_and_preserves_every_default() -> None:
    pilot = load_omega_flathub_pilot()
    assert pilot.recipe.kodi_version == "21.3"
    assert (
        pilot.recipe.definition_id
        != load_installation_recipe("omega").recipe.definition_id
    )
    assert selected_installation_recipe("omega", omega_flathub_pilot=True) == pilot
    for generation in ("matrix", "nexus", "omega", "piers"):
        assert selected_installation_recipe(
            generation, omega_flathub_pilot=False
        ) == load_installation_recipe(generation)
        if generation != "omega":
            assert selected_installation_recipe(
                generation, omega_flathub_pilot=True
            ) == load_installation_recipe(generation)
    capability = reviewed_display_capability(pilot)
    assert capability is not None
    assert capability.expected_kodi_version == "21.3"
    assert capability.process_probe == "piers-loader"


def test_exact_signed_component_set_rejects_mixing_piers_and_omega() -> None:
    omega = load_inactive_ostree_recipe("21.3").recipe
    piers = load_inactive_ostree_recipe("22.0-rc.2").recipe
    assert omega.provider.components[0].remote == "flathub"
    assert piers.provider.components[0].remote == "flathub-beta"
    wrong: dict[str, Any] = deepcopy(omega.model_dump(mode="json"))
    wrong["provider"]["components"][1] = piers.provider.components[1].model_dump()
    with pytest.raises(ValidationError, match="source and release"):
        InactiveOstreeRecipe.model_validate(wrong)


def test_pilot_approval_is_bound_to_exact_recipe_and_omega_generation() -> None:
    ack = PreviewAcknowledgement.model_validate(
        preview_acknowledgement(generation="omega")
    )
    assert reviewed_preview_recipe(ack) == load_omega_flathub_pilot()
    with pytest.raises(ValidationError, match="acknowledgement/provider"):
        ReviewedInstallationLaunchParams(
            generation="piers",
            operation_id="06ad33b0-de21-429a-bbab-347d40f29abd",
            preview_acknowledgement=ack,
        )
    with pytest.raises(ValidationError, match="acknowledgement differs"):
        PreviewAcknowledgement.model_validate(
            {**ack.model_dump(), "acknowledgement": "development-only-prerelease"}
        )


def test_native_pilot_rejects_unreviewed_omega_binary() -> None:
    version = {
        "major": 21,
        "minor": 3,
        "tag": "stable",
        "revision": "20251031-a3a448d-dirty",
    }
    assert native_identity({"name": "Kodi", "version": version}, "21.3") == version
    assert native_executable_matches(
        b"Kodi Media Center 21.3 (21.3.0) Git:20251031-a3a448d-dirty\n"
        b"Copyright (C) 2005-2025 Team Kodi - http://kodi.tv\n",
        "21.3",
    )
    assert not native_executable_matches(
        b"Kodi Media Center 21.3 (21.3.0) Git:20260315-nogitfound\n", "21.3"
    )
    with pytest.raises(RecipeValidationError, match="identity differs"):
        native_identity(
            {"name": "Kodi", "version": {**version, "revision": "20260315-nogitfound"}},
            "21.3",
        )


def test_worker_reads_only_exact_operation_owned_pilot_consent(
    factory: sessionmaker[Session],
) -> None:
    ack = PreviewAcknowledgement.model_validate(
        preview_acknowledgement(generation="omega")
    )
    operation = uuid4()
    with factory.begin() as db:
        row = RuntimeRecipeInstallationRepository(db).create(
            operation_id=operation,
            generation="omega",
            recipe_id=ack.recipe_id,
            recipe_revision=ack.recipe_revision,
            recipe_digest=ack.recipe_digest,
            now=datetime.now(UTC),
        )
        row.attestation_json = admission_evidence(ack, operation)
    assert recorded_acknowledgement(factory, operation) == ack
    with factory.begin() as db:
        recorded = RuntimeRecipeInstallationRepository(db).get(operation)
        assert recorded is not None
        recorded.attestation_json = admission_evidence(ack, uuid4())
    with pytest.raises(ValueError, match="operation admission differs"):
        recorded_acknowledgement(factory, operation)

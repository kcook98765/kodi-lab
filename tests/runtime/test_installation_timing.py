"""Timing emits only committed RC1 transitions and preserves APT responses."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.install import _advance, _fail
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.runtime.test_recipe_installation import factory as factory


@pytest.mark.parametrize("generation", ["piers", "omega"])
def test_committed_phase_timing_preserves_response(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str], generation: str
) -> None:
    reviewed = load_installation_recipe(generation)
    operation = uuid4()
    with factory.begin() as db:
        RuntimeRecipeInstallationRepository(db).create(
            operation_id=operation,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation=generation,
            now=datetime.now(UTC),
        )
    assert _advance(
        factory, operation, expected=RuntimeRecipeInstallationState.PENDING
    ) == {"state": "acquiring"}
    output = capsys.readouterr().err
    if generation == "piers":
        event = json.loads(output)
        assert event["state"] == "acquiring" and event["operation_id"] == str(operation)
        assert event["previous_phase_seconds"] >= 0
        with factory() as db:
            row = RuntimeRecipeInstallationRepository(db).get(operation)
            assert row is not None and row.updated_at == event["at"]
    else:
        assert output == ""
    assert _fail(factory, operation, code="interrupted") == {"state": "failed"}
    output = capsys.readouterr().err
    if generation == "piers":
        assert json.loads(output)["state"] == "failed"
    else:
        assert output == ""


def test_invalid_transition_emits_no_success_event(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError):
        _advance(factory, uuid4(), expected=RuntimeRecipeInstallationState.PENDING)
    assert capsys.readouterr().err == ""

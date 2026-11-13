"""Retirement of an independently installed profile never settles Create history."""

from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.runtime_installations import (
    CreateInstallationObservation,
    RuntimeInstallationService,
)
from kodi_lab.application.runtime_retirement_receipts import read_retirement_receipt
from kodi_lab.domain.create_kodi import CreateConflict, CreateIntent
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.models import RuntimeRecipeInstallationRow
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_runtime_retirement_receipts import case as case
from tests.application.test_runtime_retirement_receipts import scope


def test_exact_retirement_preserves_unbound_uncertain_installation_and_replay(
    case: Any,
) -> None:
    repo = CreateKodiRepository(case.sessions)
    recipe = load_installation_recipe("omega")
    request = uuid4()
    intent = CreateIntent(
        "omega",
        fixture.TARGET_ID,
        recipe.recipe.recipe_id,
        recipe.recipe.recipe_revision,
        recipe.digest,
        start_after_setup=False,
    )

    def accept() -> Any:
        return repo.accept(
            request_id=request,
            generation="omega",
            recipe_digest=recipe.digest,
            resolve_intent=lambda: intent,
            start_after_setup=False,
        )

    op = accept().operation
    op = repo.plan(op.id, version=op.version)
    op = repo.claim_dispatch(op.id, version=op.version).operation
    op = repo.note_uncertain(
        op.id, version=op.version, reason="installation_launch_unconfirmed"
    )
    assert op.runtime_revision_id is None and op.container_id is None
    assert op.provision_operation_id is op.start_operation_id is None

    class MissingChild:
        def observe_create(self, *_args: object) -> CreateInstallationObservation:
            return CreateInstallationObservation(
                operation_id=op.installation_operation_id,
                generation="omega",
                recipe_id=op.recipe_id,
                recipe_revision=op.recipe_revision,
                recipe_digest=op.recipe_digest,
                status="absent",
                busy=False,
            )

        def start(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("uncertain installation must never be redispatched")

    adapter = CreateKodiInstallationService(
        repo, cast(RuntimeInstallationService, MissingChild())
    )
    assert adapter.resolve(op.id, version=op.version) == op
    removal = uuid4()
    case.lifecycle.deprovision(
        fixture.TARGET_ID, operation_id=removal, expected_scope=scope()
    )
    receipt = read_retirement_receipt(case.sessions, removal)
    assert receipt.confirmation == "confirmed"
    assert receipt.settles_uncertain_create is False
    assert repo.get(op.id) == op
    assert adapter.resolve(op.id, version=op.version) == op
    assert accept().replayed and accept().operation == op
    with case.sessions() as session:
        assert session.get(RuntimeRecipeInstallationRow, str(op.child_id)) is None
    # The original canonical target stays reserved in the Create ledger forever.
    with pytest.raises(CreateConflict, match="canonical_create_exists"):
        repo.accept(
            request_id=uuid4(),
            generation="omega",
            recipe_digest=recipe.digest,
            resolve_intent=lambda: intent,
            start_after_setup=False,
        )
    assert not case.env.state.records and len(case.broker.calls) == 1

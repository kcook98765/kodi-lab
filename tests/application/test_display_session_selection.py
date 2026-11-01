"""Preview selection requires exact sealed approval and the reviewed recipe."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_session_workflow import DevelopmentSessionWorkflow
from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.config import Settings
from kodi_lab.domain import RevisionApprovalState
from kodi_lab.runtime.recipes import recipe_target_id
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import RuntimeContainerArtifactRow, RuntimeRevisionRow
from tests.application.test_piers_installation_admission import broker
from tests.runtime.test_piers_display import ready_display
from tests.runtime.test_recipe_installation import factory as factory


@pytest.mark.parametrize("defect", [None, "recipe", "reference", "image", "approval"])
def test_current_piers_preview_selection_requires_exact_sealed_approval(
    factory: sessionmaker[Session],
    tmp_path: Path,
    defect: str | None,
) -> None:
    _operation, reviewed, built, revision_id = ready_display(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _: SimpleNamespace(present=True)
    RuntimeInstallationService(factory, backend).create_preview_instance(
        development_mode=True,
        acknowledgement=built.acknowledgement,
        display_candidate=True,
    )
    target = recipe_target_id(reviewed)
    with factory.begin() as db:
        revision = db.get(RuntimeRevisionRow, str(revision_id))
        assert revision is not None
        if defect == "recipe":
            revision.manifest_digest = "sha256:" + "f" * 64
        if defect == "reference":
            revision.manifest_reference = "unreviewed://recipe"
        if defect == "image":
            artifact = db.get(RuntimeContainerArtifactRow, str(revision_id))
            assert artifact is not None
            artifact.image_id = "sha256:" + "f" * 64
        if defect == "approval":
            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
    service = DevelopmentSessionService(
        factory,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=Settings().data_root,
    )
    if defect is None:
        with factory() as db:
            service._runtime(db, target, revision_id)
        assert any(
            choice.target_id == target
            for choice in DevelopmentSessionWorkflow(service, cast(Any, None)).choices()
        )
    else:
        with factory() as db, pytest.raises(DevelopmentSessionConflict):
            service._runtime(db, target, revision_id)
        assert DevelopmentSessionWorkflow(service, cast(Any, None)).choices() == ()


def test_ordinary_piers_r2_does_not_gain_disposable_selection(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from dataclasses import replace
    from uuid import UUID

    from kodi_lab.runtime.ostree_assembly import (
        assembly_assets_digest,
        load_assembly_assets,
    )
    from kodi_lab.runtime.ostree_installation import OstreeInstallationAssembly
    from kodi_lab.runtime.recipes import (
        PREVIEW_INSTALLATION_DIGEST,
        load_recorded_reviewed_recipe,
    )
    from kodi_lab.runtime.ubuntu_support import load_support_closure
    from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
    from tests.runtime import test_ostree_installation as fixture

    reviewed = load_recorded_reviewed_recipe(
        "piers", "piers-22.0-rc1-r2", 2, PREVIEW_INSTALLATION_DIGEST
    )
    acknowledgement = fixture.acknowledgement().model_copy(
        update={
            "recipe_id": reviewed.recipe.recipe_id,
            "recipe_revision": 2,
            "recipe_digest": reviewed.digest,
        }
    )
    current_assembly = fixture.assembly

    def historical_assembly(operation: UUID) -> OstreeInstallationAssembly:
        original = current_assembly(operation)
        return replace(
            original,
            assembly=replace(
                original.assembly,
                recipe_revision=2,
                supporting_closure_digest=load_support_closure(revision=2).digest,
                assembly_assets_digest="sha256:" + assembly_assets_digest(revision=2),
                asset_hashes=tuple(
                    sorted(
                        (name, hashlib.sha256(data).hexdigest())
                        for name, data in load_assembly_assets(revision=2).items()
                    )
                ),
            ),
            acknowledgement=acknowledgement,
        )

    # Reconstruct only the test's historical installation/registration. Restore
    # current defaults before exercising the real selection policy below.
    with monkeypatch.context() as historical:
        historical.setattr(fixture, "load_installation_recipe", lambda _: reviewed)
        historical.setattr(fixture, "assembly", historical_assembly)
        historical.setattr(
            "kodi_lab.application.runtime_installations.load_installation_recipe",
            lambda _: reviewed,
        )
        operation = fixture.ready(factory)
        backend = broker()
        backend.reviewed_runtime_image_present = lambda _: SimpleNamespace(present=True)
        RuntimeInstallationService(factory, backend).create_preview_instance(
            development_mode=True, acknowledgement=acknowledgement
        )
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        revision_id = UUID(row.runtime_revision_id)
    target = recipe_target_id(reviewed)
    service = DevelopmentSessionService(
        factory,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=Settings().data_root,
    )
    with factory() as db, pytest.raises(DevelopmentSessionConflict):
        service._runtime(db, target, revision_id)

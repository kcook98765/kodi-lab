"""Exact approved Preview selection never grants a standard viewer."""

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
    factory: sessionmaker[Session],
) -> None:
    from uuid import UUID

    from kodi_lab.runtime.recipes import load_installation_recipe
    from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
    from tests.runtime.test_ostree_installation import acknowledgement, ready

    operation = ready(factory)
    backend = broker()
    backend.reviewed_runtime_image_present = lambda _: SimpleNamespace(present=True)
    RuntimeInstallationService(factory, backend).create_preview_instance(
        development_mode=True, acknowledgement=acknowledgement()
    )
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        revision_id = UUID(row.runtime_revision_id)
    target = recipe_target_id(load_installation_recipe("piers"))
    service = DevelopmentSessionService(
        factory,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=Settings().data_root,
    )
    with factory() as db, pytest.raises(DevelopmentSessionConflict):
        service._runtime(db, target, revision_id)

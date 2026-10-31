"""The sealed r4 development admission uses real installation provenance."""

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_session_workflow import DevelopmentSessionWorkflow
from kodi_lab.application.development_sessions import (
    APPROVED_OMEGA_VIEW_IMAGE,
    APPROVED_OMEGA_VIEW_REVISION,
    DevelopmentSessionRequest,
    DevelopmentSessionService,
    development_target_ids,
)
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import (
    load_display_candidate_recipe,
    load_novnc_phase1_recipe,
    recipe_target_id,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeContainerArtifactRow,
    RuntimeExecutionProfileRow,
    RuntimeRecipeInstallationRow,
    RuntimeRevisionRow,
    TargetRow,
)
from tests.runtime import test_recipe_installation as recipe_fixture
from tests.runtime.test_recipe_installation import factory as factory


def test_r4_reservation_and_prepare_share_the_exact_eligibility_fence(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.application import test_mcp_workspace_development as workspace_fixture

    service, target = installed(factory, tmp_path, monkeypatch)
    env = workspace_fixture._environment(
        tmp_path / "workspace", database_path=tmp_path / "recipe.db"
    )
    reserved = service.reserve(
        DevelopmentSessionRequest(
            request_id=uuid4(),
            expected_generation=0,
            target_id=target,
            workspace_id=env.workspace_id,
        )
    )
    assert reserved.target_id == target
    assert reserved.runtime_revision_id == APPROVED_OMEGA_VIEW_REVISION

    class Lifecycle:
        def provision(self, target_id: UUID, *, precondition: Any) -> Any:
            assert target_id == target
            with factory() as db:
                precondition(db)
            return "qualified"

    workflow = DevelopmentSessionWorkflow(service, cast(Any, Lifecycle()))
    assert cast(Any, workflow.act("prepare", reserved.generation)) == "qualified"
    with factory.begin() as db:
        artifact = db.get(
            RuntimeContainerArtifactRow, str(APPROVED_OMEGA_VIEW_REVISION)
        )
        assert artifact is not None
        artifact.image_id = "sha256:" + "f" * 64
    with pytest.raises(DevelopmentSessionConflict, match="runtime_ineligible"):
        workflow.act("prepare", reserved.generation)


def installed(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DevelopmentSessionService, UUID]:
    reviewed = load_novnc_phase1_recipe()
    monkeypatch.setattr(recipe_fixture, "load_reviewed_recipe", lambda _: reviewed)
    monkeypatch.setattr(
        "kodi_lab.runtime.install.uuid4", lambda: APPROVED_OMEGA_VIEW_REVISION
    )
    recipe_fixture._ready(
        factory, image_id=APPROVED_OMEGA_VIEW_IMAGE, generation="omega"
    )
    bootstrap_recipe_instance(factory, reviewed, APPROVED_OMEGA_VIEW_REVISION)
    service = DevelopmentSessionService(
        factory,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=tmp_path / "state",
    )
    return service, recipe_target_id(reviewed)


def test_only_approved_r4_is_selectable_with_viewer_disabled(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.config import Settings
    from kodi_lab.runtime.lab import KNOWN_SELECTORS, canonical_id

    service, target = installed(factory, tmp_path, monkeypatch)
    assert Settings().experimental_novnc_instances == ()
    with factory() as db:
        service._runtime(db, target, APPROVED_OMEGA_VIEW_REVISION)
    choices = DevelopmentSessionWorkflow(service, cast(Any, None)).choices()
    assert [choice.target_id for choice in choices] == [target]
    assert development_target_ids() == {
        *(canonical_id(selector) for selector in KNOWN_SELECTORS),
        target,
        recipe_target_id(load_display_candidate_recipe("piers")),
    }
    with (
        factory() as db,
        pytest.raises(DevelopmentSessionConflict, match="noncanonical"),
    ):
        service._runtime(db, uuid4(), APPROVED_OMEGA_VIEW_REVISION)


@pytest.mark.parametrize(
    "defect",
    ["r3", "revision", "image", "pin", "approval", "profile", "attestation", "target"],
)
def test_r4_admission_rejects_wrong_stale_or_unqualified_state(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    service, target = installed(factory, tmp_path, monkeypatch)
    with factory.begin() as db:
        revision = db.get(RuntimeRevisionRow, str(APPROVED_OMEGA_VIEW_REVISION))
        assert revision is not None
        if defect == "r3":
            revision.manifest_digest = "sha256:" + (
                "a81b7786b6df3c4cf0fde57b117ee87b5911eb8df1f17c74e2cee45685337a46"
            )
        elif defect == "revision":
            revision.manifest_reference = "unreviewed://recipe"
        elif defect == "image":
            artifact = db.get(
                RuntimeContainerArtifactRow, str(APPROVED_OMEGA_VIEW_REVISION)
            )
            assert artifact is not None
            artifact.image_id = "sha256:" + "f" * 64
        elif defect == "pin":
            instance = db.get(ManagedInstanceRow, str(target))
            assert instance is not None
            replacement = RuntimeRevisionRow(
                **{
                    column.name: getattr(revision, column.name)
                    for column in RuntimeRevisionRow.__table__.columns
                }
            )
            replacement.id = str(uuid4())
            replacement.revision_number += 1
            db.add(replacement)
            db.flush()
            instance.runtime_revision_id = replacement.id
        elif defect == "approval":
            from kodi_lab.domain import RevisionApprovalState

            revision.approval_state = RevisionApprovalState.NOT_APPROVED
            revision.approved_at = None
        elif defect == "profile":
            profile = db.get(
                RuntimeExecutionProfileRow, str(APPROVED_OMEGA_VIEW_REVISION)
            )
            assert profile is not None
            profile.runtime_uid = 0
        elif defect == "attestation":
            from sqlalchemy import select

            installation = db.scalar(select(RuntimeRecipeInstallationRow))
            assert installation is not None
            installation.attestation_json = "{}"
        else:
            row = db.get(TargetRow, str(target))
            assert row is not None
            row.name = "Other target"
    with factory() as db, pytest.raises(DevelopmentSessionConflict):
        service._runtime(db, target, APPROVED_OMEGA_VIEW_REVISION)
    assert DevelopmentSessionWorkflow(service, cast(Any, None)).choices() == ()

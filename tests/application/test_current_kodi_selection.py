"""Real sealed-r4 qualification composes into the selected Web presentation."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_session_workflow import DevelopmentSessionWorkflow
from kodi_lab.application.development_sessions import DevelopmentSessionRequest
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
    ManagedStatusObservation,
)
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.models import DevelopmentSessionRow
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from kodi_lab.web.current_kodi import CurrentKodiPresentation
from tests.application import test_mcp_workspace_development as workspace_fixture
from tests.application.test_development_runtime_selection import installed
from tests.runtime import test_recipe_installation as recipe_fixture
from tests.runtime.test_recipe_installation import factory as factory


def test_selected_r4_renders_inline_viewer_without_adopting_canonical_omega(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = load_reviewed_recipe("omega")
    operation = recipe_fixture._ready(factory, generation="omega")
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        canonical_revision = UUID(row.runtime_revision_id)
    bootstrap_recipe_instance(factory, canonical, canonical_revision)
    sessions, target = installed(factory, tmp_path, monkeypatch)
    env = workspace_fixture._environment(
        tmp_path / "workspace", database_path=tmp_path / "recipe.db"
    )
    current = sessions.reserve(
        DevelopmentSessionRequest(
            request_id=uuid4(),
            expected_generation=0,
            target_id=target,
            workspace_id=env.workspace_id,
        )
    )
    with factory.begin() as db:
        bound = db.get(DevelopmentSessionRow, current.generation)
        assert bound is not None
        bound.phase, bound.container_id = "bound", "a" * 64
    runtime = ManagedRuntimeStatus.model_validate(
        {
            "managed_instance_id": target,
            "name": "Omega noVNC",
            "kodi_version": "21.3",
            "runtime_revision_id": current.runtime_revision_id,
            "runtime_revision_number": 2,
            "provision_state": "provisioned",
            "container_state": "running",
            "process_state": "running",
            "jsonrpc_state": "not_checked",
            "usability_state": "not_usable",
            "readiness_state": "idle",
            "allowed_actions": ["stop"],
        }
    )
    inventory = ManagedContainerInventoryResult(
        containers=[
            ManagedContainerInventoryEntry(
                managed_instance_id=str(target),
                container_id="a" * 64,
                image_id=runtime_image(),
                runtime_revision_id=str(current.runtime_revision_id),
                runtime_state=ContainerRuntimeState.RUNNING,
                health_state=ContainerHealthState.HEALTHY,
            )
        ]
    )
    status = SimpleNamespace(
        get_observation=lambda _: ManagedStatusObservation(runtime, inventory, None)
    )
    reconciliation = SimpleNamespace(
        status=lambda _: SimpleNamespace(operation_uuid=None, blocking=False)
    )
    view = CurrentKodiPresentation(
        factory,
        DevelopmentSessionWorkflow(sessions, cast(Any, None)),
        sessions,
        cast(ManagedRuntimeStatusService, status),
        cast(ManagedInstanceReconciliationService, reconciliation),
    ).observe()
    assert view.current is not None and view.current.target_id == target
    assert view.summary is not None and view.summary.name == "Omega 21.3"
    assert view.summary.status == "Running"  # Version and running alone are not Ready.
    assert view.summary.actions == ["stop"]
    templates = Path(__file__).parents[2] / "src/kodi_lab/web/templates"
    renderer = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape()
    )
    page = renderer.get_template("development-session.html").render(
        current_kodi=view,
        workspaces=[],
        choices=[],
        csrf_token="fixture",
        shared_navigation_enabled=True,
        admin_view_enabled=False,
        novnc_instance_enabled=lambda value: value == target,
        url_for=lambda _name, **kwargs: "/static" + kwargs["path"],
    )
    assert f'data-novnc-viewer data-instance="{target}"' in page
    assert f'data-instance="{canonical_revision}"' not in page
    assert page.count('type="module"') == 1
    assert page.count("data-novnc-viewer") == 1
    assert "<h1 data-current-name>Omega 21.3</h1>" in page


def runtime_image() -> str:
    from kodi_lab.application.development_sessions import APPROVED_OMEGA_VIEW_IMAGE

    return APPROVED_OMEGA_VIEW_IMAGE

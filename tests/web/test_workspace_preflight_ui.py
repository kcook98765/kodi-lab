"""Stored deployment guidance is explicit, authenticated and independent of liveness."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.domain import InstanceType
from kodi_lab.storage.models import ManagedInstanceRow
from tests.web.conftest import INSTANCE_ID, login
from tests.web.test_agent_workspaces import SOURCE_ID, _seed_source


@pytest.mark.parametrize("selected", [False, True])
def test_workspace_offers_catalog_preflight_without_requesting_assessment_or_runtime(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
) -> None:
    _seed_source(database, data_root)
    context = cast(FastAPI, client.app).state.kodi_lab
    creation = context.agent_workspaces.queue_creation(SOURCE_ID)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    workspace = creation.workspace_id
    url = f"/agent-workspaces/{workspace}"
    assert client.get(url, follow_redirects=False).status_code == 303
    login(client)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("rendering preflight must not probe runtime")

    monkeypatch.setattr(context.status, "get", forbidden)
    monkeypatch.setattr(context.status, "get_observation", forbidden)
    monkeypatch.setattr(context.status, "list_statuses", forbidden)
    monkeypatch.setattr(context.readiness, "start", forbidden)
    # The existing promotion section has a separate target-binding policy.
    monkeypatch.setattr(
        context.agent_workspace_promotions, "eligible_targets", lambda _: []
    )
    monkeypatch.setattr(
        DevelopmentSessionService,
        "status",
        lambda _: SimpleNamespace(
            target_id=INSTANCE_ID,
            workspace_id=workspace if selected else uuid4(),
        ),
    )
    page = client.get(url)
    assert page.status_code == 200
    assert 'id="workspace-deployment-preflight"' in page.text
    assert "workspace-deployment-preflight.js" in page.text
    assert 'data-generation="0"' in page.text
    assert (f'value="{INSTANCE_ID}" selected' in page.text) is selected
    assert "does not deploy, start, stop or repair" in page.text
    assert "check an agent's permissions" in page.text
    assert str(data_root) not in page.text
    # A persistent managed target must not become a disposable deployment choice.
    with database.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.instance_type = InstanceType.PERSISTENT
    page = client.get(url)
    assert "No managed disposable Kodi is registered" in page.text
    assert "data-preflight-target" not in page.text


def test_preflight_browser_never_mutates_or_accepts_stale_target_results() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("workspace_preflight_browser.js")),
            str(root / "src/kodi_lab/web/static/workspace-deployment-preflight.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

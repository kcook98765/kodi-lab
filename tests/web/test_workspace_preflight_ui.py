"""Stored deployment guidance is explicit, authenticated and independent of liveness."""

import json
import os
import re
import subprocess
from html import unescape
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


@pytest.mark.parametrize("view", ["standard", "development"])
@pytest.mark.parametrize("warning", [False, True])
def test_long_workspace_previews_preserve_text_and_editor_bindings(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    view: str,
    warning: bool,
) -> None:
    _seed_source(database, data_root)
    context = cast(FastAPI, client.app).state.kodi_lab
    creation = context.agent_workspaces.queue_creation(SOURCE_ID)
    lease = context.agent_workspaces.acquire(creation.operation_id)
    assert lease is not None
    context.agent_workspaces.run(lease)
    csrf = login(client)
    client.cookies.set("kodi_lab_admin_view", view)
    page = client.get(f"/agent-workspaces/{creation.workspace_id}")
    assert page.status_code == 200
    values = dict(cast(Any, page).context)
    filename = "resources/" + "long_filename_" * 16 + ".py"
    before = (
        "# source with whitespace\n\tif value < 3 and name == '<&>':\n    " + "x" * 2048
    )
    after = "# " + "words with spaces " * 128 + "\n" + "sha256=" + "a" * 64
    values["entries"] = [SimpleNamespace(name=filename, entry_type="file")]
    values["changes"] = SimpleNamespace(
        identity_status="identity_changed" if warning else "stable",
        changes=[
            SimpleNamespace(
                path=filename,
                change_kind="modified",
                baseline_size=len(before),
                current_size=len(after),
                before_preview=before,
                after_preview=after,
                before_truncated=False,
                after_truncated=False,
            )
        ],
    )
    values["capture_status"] = values["capture_status"].model_copy(
        update={
            "active_capture_digest": "a" * 64,
            "workspace_tree_digest": "b" * 64,
            "private_edits": warning,
            "recovery_state": "staging" if warning else None,
            "capture_available": not warning,
        }
    )
    html = cast(Any, page).template.render(**values)
    assert [unescape(v) for v in re.findall(r"<pre>(.*?)</pre>", html, re.S)] == [
        before,
        after,
    ]
    assert filename in html and "b" * 64 in html
    assert f'data-workspace-id="{creation.workspace_id}"' in html
    assert f'data-csrf="{csrf}"' in html
    assert 'id="workspace-editor"' in html and 'data-generation="0"' in html
    for action in ("create", "replace", "patch"):
        assert f'data-action="{action}"' in html
    if warning:
        assert 'role="alert">Capture recovery state: staging.' in html
        assert (
            '<details class="navigation-group" open>'
            "<summary>Detailed changes and previews" in html
        )
    if evidence := os.getenv("KODI_UI_OVERFLOW_EVIDENCE"):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        name = f"workspace-{view}-{warning}"
        (root / f"{name}.html").write_text(html)
        (root / f"{name}.json").write_text(
            json.dumps({"previews": [before, after], "filename": filename})
        )


@pytest.mark.parametrize("view", ["standard", "development"])
@pytest.mark.parametrize("selected", [False, True])
def test_workspace_offers_catalog_preflight_without_requesting_assessment_or_runtime(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected: bool,
    view: str,
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
    client.cookies.set("kodi_lab_admin_view", view)

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
    primary = page.text.split('<details class="navigation-group"', 1)[0]
    assert 'id="workspace-source-capture"' in primary
    assert 'id="workspace-deployment-preflight"' in primary
    assert 'href="/development-session"' in primary
    for label in [
        "Text file editing and root directory",
        "Detailed changes and previews",
        "Workspace edit history",
        "Promotion review",
        "Lifecycle history",
        "Retention and cleanup",
    ]:
        assert f"<summary>{label}</summary>" in page.text
    context = dict(cast(Any, page).context)
    context["capture_status"] = context["capture_status"].model_copy(
        update={
            "private_edits": True,
            "recovery_state": "staging",
            "recover_operation_id": uuid4(),
            "capture_available": False,
            "capture_unavailable_reason": "reconciliation_required",
        }
    )
    context["changes"] = context["changes"].model_copy(
        update={"identity_status": "identity_changed"}
    )
    context["lifecycle_activity"] = [
        SimpleNamespace(
            occurred_at="recorded",
            operation=SimpleNamespace(value="cleanup"),
            failure_code="reconciliation_required",
        )
    ]
    context["promotion_plan"] = SimpleNamespace(
        workspace_generation=0,
        state=SimpleNamespace(value="reviewable"),
        verification_target_id=INSTANCE_ID,
        plan_id=uuid4(),
    )
    context["repository_deployments"] = [
        SimpleNamespace(
            addon_id="plugin.video.workspace",
            addon_version="1.0.0",
            target_id=INSTANCE_ID,
            state="failed",
            operation_id=uuid4(),
            failure_code="deployment_ambiguous",
        )
    ]
    warning = cast(Any, page).template.render(**context)
    before_optional = warning.split('<details class="navigation-group"', 1)[0]
    assert "Reconcile interrupted capture" in before_optional
    assert "Capture is blocked to preserve" in before_optional
    assert (
        '<details class="navigation-group" open>'
        "<summary>Detailed changes and previews</summary>" in warning
    )
    assert (
        '<details class="navigation-group" open><summary>Lifecycle history</summary>'
        in warning
    )
    assert (
        '<details class="navigation-group" open><summary>Promotion review</summary>'
        in warning
    )
    assert (
        '<details class="navigation-group" open>'
        "<summary>Recorded repository deployments</summary>" in warning
    )
    if evidence := os.getenv("KODI_WORKSPACE_UI_EVIDENCE"):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"workspace-{view}-{selected}.html").write_text(page.text)
        (root / f"workspace-warning-{view}-{selected}.html").write_text(warning)
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

"""Current Kodi follows the durable selection, never the page last visited."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.application.runtime_status import ManagedStatusObservation
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.storage.models import DevelopmentSessionPolicyRow, DevelopmentSessionRow
from kodi_lab.web.app import create_app
from tests.web import conftest
from tests.web.conftest import (
    CONTAINER_ID,
    INSTANCE_ID,
    REVISION_ID,
    FakeBroker,
    inventory_entry,
    login,
)
from tests.web.test_development_session import source_and_workspace
from tests.web.test_novnc import viewer


@pytest.fixture
def viewer_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    create = create_app

    def configured(**kwargs: Any) -> Any:
        kwargs["novnc_viewer"] = viewer()
        app = create(**kwargs)
        app.state.current_kodi_viewer_fixture = kwargs["novnc_viewer"]
        return app

    monkeypatch.setattr(conftest, "create_app", configured)
    return cast(TestClient, request.getfixturevalue("client"))


def selected(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: str = "bound",
) -> None:
    _source, creation = source_and_workspace(client)
    now = datetime.now(UTC).isoformat()
    with database.begin() as db:
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        if policy is None:
            db.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        else:
            policy.active_generation = 1
        workspace = cast(FastAPI, client.app).state.kodi_lab.agent_workspaces.get(
            creation.workspace_id
        )
        db.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                workspace_id=str(creation.workspace_id),
                source_id=str(workspace.development_source_id),
                container_id=CONTAINER_ID if phase == "bound" else None,
                phase=phase,
                created_at=now,
                updated_at=now,
            )
        )
    # Qualification is covered by the sealed-runtime tests, not these UI fixtures.
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_args: None)


def test_export_current_kodi_browser_fixtures(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = viewer_client
    # Browser visuals use a disposable qualified-viewer double. Exact r4 runtime
    # admission is tested independently with sealed acquisition evidence.
    monkeypatch.setattr(
        "kodi_lab.runtime.recipes.recipe_target_id", lambda _: INSTANCE_ID
    )
    login(client)
    selected(client, database, monkeypatch)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    paths = [
        "/",
        "/kodi",
        "/development-session",
        "/runtimes",
        "/settings",
        "/development",
        f"/instances/{INSTANCE_ID}",
        f"/instances/{INSTANCE_ID}/novnc-phase1",
    ]
    pages = {p: client.get(p).text for p in paths}
    apis = {
        p: client.get(p).json()
        for p in [
            "/api/development-session",
            "/api/instances",
            f"/api/instances/{INSTANCE_ID}",
        ]
    }
    assert pages["/development-session"]
    for scenario in ("empty", "reserved", "uncertain"):
        with database.begin() as db:
            row = db.get(DevelopmentSessionRow, 1)
            policy = db.get(DevelopmentSessionPolicyRow, 1)
            assert row is not None and policy is not None
            policy.active_generation = None if scenario == "empty" else 1
            row.phase = (
                "reserved"
                if scenario == "reserved"
                else "uncertain"
                if scenario == "uncertain"
                else "bound"
            )
            row.container_id = None if scenario == "reserved" else CONTAINER_ID
        pages[f"/scenario/{scenario}"] = client.get("/development-session").text
        if scenario == "empty":
            pages["/scenario/expanded-unselected"] = client.get(
                f"/instances/{INSTANCE_ID}/novnc-phase1"
            ).text
    with database.begin() as db:
        row = db.get(DevelopmentSessionRow, 1)
        policy = db.get(DevelopmentSessionPolicyRow, 1)
        assert row is not None and policy is not None
        row.phase, row.container_id, policy.active_generation = "bound", CONTAINER_ID, 1
    if destination := os.getenv("KODI_LAB_CURRENT_UI_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "pages.json").write_text(json.dumps(pages))
        (root / "apis.json").write_text(json.dumps(apis))


@pytest.mark.parametrize(
    ("phase", "container", "usability", "expected", "actions"),
    [
        (
            "reserved",
            None,
            "not_usable",
            "Preparation required",
            ["prepare", "close-absent"],
        ),
        ("provisioning", None, "not_usable", "Preparing", []),
        ("uncertain", None, "not_usable", "Needs Attention", []),
        ("bound", "stopped", "not_usable", "Stopped", ["resume"]),
        ("bound", "running", "not_usable", "Running", ["stop"]),
        ("bound", "running", "checking", "Starting", ["stop"]),
        ("bound", "running", "usable", "Ready", ["stop"]),
        ("bound", "running", "failed", "Needs Attention", ["stop"]),
        ("bound", None, "not_usable", "Needs Attention", ["close-absent"]),
    ],
)
def test_selected_status_and_contextual_actions(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    container: str | None,
    usability: str,
    expected: str,
    actions: list[str],
) -> None:
    login(client)
    selected(client, database, monkeypatch, phase=phase)
    context = cast(FastAPI, client.app).state.kodi_lab
    runtime = context.status.get(INSTANCE_ID)
    runtime = runtime.model_validate(
        {
            **runtime.model_dump(),
            "container_state": container or "stopped",
            "provision_state": "provisioned" if container else "unprovisioned",
            "process_state": "running" if container == "running" else "not_running",
            "jsonrpc_state": "responsive" if usability == "usable" else "not_checked",
            "usability_state": usability,
            "readiness_state": "checking" if usability == "checking" else "idle",
            "allowed_actions": ["stop"]
            if container == "running"
            else ["start"]
            if container
            else ["provision"],
        }
    )
    observation = ManagedStatusObservation(
        runtime,
        ManagedContainerInventoryResult(
            containers=[]
            if container is None
            else [inventory_entry(ContainerRuntimeState(container))],
        ),
        None,
    )
    monkeypatch.setattr(context.status, "get_observation", lambda _id: observation)
    view = client.get("/api/development-session").json()
    assert view["current"]["target_id"] == str(INSTANCE_ID)
    assert view["summary"]["status"] == expected
    assert view["summary"]["actions"] == actions
    assert view["summary"]["workspace_name"] == "Session source"
    assert view["summary"]["retained"] is False
    assert view["viewer_state"] == "unsupported"
    assert "agent_authority" not in view["summary"]


def test_empty_selection_never_adopts_a_running_registration(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    context = cast(FastAPI, client.app).state.kodi_lab
    monkeypatch.setattr(
        context.status,
        "get_observation",
        lambda _: pytest.fail("No selected target may be observed"),
    )
    view = client.get("/api/development-session").json()
    assert view["current"] is None and view["summary"] is None
    assert "No Kodi selected" in client.get("/development-session").text
    assert not broker.calls


def test_direct_instance_links_do_not_select_kodi_or_expose_its_saved_screen_as_live(
    client: TestClient,
) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert "Not the currently selected Kodi" in page
    assert 'href="/development-session"' in page
    assert "Saved screenshot" in page
    assert "not a live screen" in page
    assert client.get("/api/development-session").json()["current"] is None


def test_selection_change_during_observation_discards_all_old_status_and_actions(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    selected(client, database, monkeypatch)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    context = cast(FastAPI, client.app).state.kodi_lab
    observe = context.status.get_observation

    def changed(target: Any) -> ManagedStatusObservation:
        value = observe(target)
        with database.begin() as db:
            row = db.get(DevelopmentSessionRow, 1)
            assert row is not None
            row.phase = "uncertain"
        return cast(ManagedStatusObservation, value)

    monkeypatch.setattr(context.status, "get_observation", changed)
    view = client.get("/api/development-session").json()
    assert view["summary"]["status"] == "Needs Attention"
    assert view["summary"]["actions"] == []
    assert view["runtime"] is None
    assert "changed" in view["summary"]["detail"]


def test_current_page_and_advanced_are_accessible_in_both_presentation_modes(
    client: TestClient,
) -> None:
    login(client)
    for mode in ("standard", "development"):
        client.cookies.set("kodi_lab_admin_view", mode)
        assert "Current Kodi" in client.get("/kodi").text
        advanced = client.get("/settings").text
        for destination in (
            "/runtimes",
            "/#instances",
            "/managed-resources",
            "/history/instances",
            "/snapshots/preserved",
        ):
            assert f'href="{destination}"' in advanced
        page = client.get("/development-session").text
        assert 'href="/kodi" aria-current="page"' in page
        assert "This is a Development administration page" not in page


def test_inline_viewer_is_single_explicit_controller_with_narrow_csp(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = viewer_client
    login(client)
    selected(client, database, monkeypatch)
    page = client.get("/development-session")
    assert page.text.count("data-novnc-viewer") == 1
    assert page.text.count('type="module"') == 1
    assert "<iframe" not in page.text
    assert 'id="start" disabled' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "img-src 'self' blob: data:" in page.headers["content-security-policy"]
    assert "data:" not in client.get("/kodi").headers["content-security-policy"]
    assert "data:" not in client.get("/settings").headers["content-security-policy"]


def test_disabled_viewer_has_no_inline_controller_or_relaxed_policy(
    client: TestClient,
) -> None:
    login(client)
    page = client.get("/development-session")
    assert "data-novnc-viewer" not in page.text
    assert "novnc-phase1.js" not in page.text
    assert "data:" not in page.headers["content-security-policy"]


@pytest.mark.parametrize("phase", ["reserved", "bound"])
def test_ineligible_runtime_keeps_absence_only_recovery(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    login(client)
    selected(client, database, monkeypatch, phase=phase)

    def rejected(*_: Any) -> None:
        from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

        raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    monkeypatch.setattr(DevelopmentSessionService, "_runtime", rejected)
    view = client.get("/api/development-session").json()
    assert view["summary"]["status"] == "Needs Attention"
    assert view["summary"]["actions"] == ["close-absent"]


def test_rejected_approval_cannot_hide_exact_stop(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    selected(client, database, monkeypatch)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)

    def rejected(*_: Any) -> None:
        from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

        raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    monkeypatch.setattr(DevelopmentSessionService, "_runtime", rejected)
    view = client.get("/api/development-session").json()
    assert view["summary"]["status"] == "Needs Attention"
    assert view["summary"]["actions"] == ["stop"]


def test_bound_absent_profile_can_be_explicitly_released(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(client)
    selected(client, database, monkeypatch)
    assert client.get("/api/development-session").json()["summary"]["actions"] == [
        "close-absent"
    ]
    from tests.web.conftest import ORIGIN

    response = client.post(
        "/api/development-session/close-absent",
        json={"expected_generation": 1},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 200 and response.json()["phase"] == "closed"
    assert client.get("/api/development-session").json()["current"] is None
    with database() as db:
        assert db.get(DevelopmentSessionRow, 1) is not None


@pytest.mark.parametrize("release_fails", [False, True])
def test_stop_kodi_waits_for_verified_human_release_before_lifecycle_dispatch(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    release_fails: bool,
) -> None:
    from types import SimpleNamespace

    from kodi_lab.application.development_session_workflow import (
        DevelopmentSessionWorkflow,
    )
    from kodi_lab.web.novnc import PrototypeViewer
    from tests.web.conftest import ORIGIN

    client = viewer_client
    csrf = login(client)
    selected(client, database, monkeypatch)
    calls: list[str] = []

    def revoke(service: PrototypeViewer, _target: Any) -> None:
        calls.append("revoke")

        def released(_target: Any) -> None:
            calls.append("release-proof")
            if release_fails:
                raise ValueError("unproven")

        service.ownership = cast(Any, SimpleNamespace(wait_released=released))

    def act(_self: Any, _action: str, _generation: int, *, before_stop: Any) -> Any:
        before_stop()
        assert calls == ["revoke", "release-proof"]
        calls.append("dispatch")
        return cast(FastAPI, client.app).state.kodi_lab.status.get(INSTANCE_ID)

    monkeypatch.setattr(PrototypeViewer, "revoke_instance", revoke)
    monkeypatch.setattr(DevelopmentSessionWorkflow, "act", act)
    response = client.post(
        "/api/development-session/stop",
        json={"expected_generation": 1},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == (409 if release_fails else 200)
    assert calls == (
        ["revoke", "release-proof"]
        if release_fails
        else ["revoke", "release-proof", "dispatch"]
    )
    if release_fails:
        assert response.json()["error"]["code"] == "input_release_unproven"


@pytest.mark.parametrize("route", ["session", "advanced"])
@pytest.mark.parametrize("proven", [True, False])
def test_stop_composes_real_ownership_exclusion_and_existing_lifecycle(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    proven: bool,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from tests.web.conftest import IMAGE_ID, ORIGIN

    client = viewer_client
    csrf = login(client)
    selected(client, database, monkeypatch)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    service = cast(FastAPI, client.app).state.current_kodi_viewer_fixture
    ownership = HumanViewerOwnership(database)
    service.ownership = ownership
    attempt, broker_id = uuid4(), str(uuid4())
    binding = ViewBinding(
        CONTAINER_ID, IMAGE_ID, "fixture-start", (str(REVISION_ID), 1)
    )
    ownership.create(INSTANCE_ID, attempt, "c" * 64, binding, "interactive")
    ownership.claim(INSTANCE_ID, attempt, "c" * 64, binding, "interactive", broker_id)
    reached = Event()
    wait_released = ownership.wait_released

    def wait(target: Any) -> None:
        reached.set()
        wait_released(target, timeout=2 if proven else 0.02)
        from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

        with database() as db:
            assert db.get(ManagedInstanceMutationLeaseRow, str(target)) is not None
        # A successor with authority resolved before Stop must still be denied
        # atomically after the old worker's release, for both viewer modes.
        for mode in ("view-only", "interactive"):
            with pytest.raises(ValueError, match="lifecycle operation conflict"):
                ownership.create(target, uuid4(), "d" * 64, binding, mode)

    monkeypatch.setattr(ownership, "wait_released", wait)
    endpoint = (
        "/api/development-session/stop"
        if route == "session"
        else f"/api/instances/{INSTANCE_ID}/stop"
    )
    body = {"expected_generation": 1} if route == "session" else {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            client.post,
            endpoint,
            json=body,
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert reached.wait(2)
        assert not any(kind == "stop" for kind, _ in broker.calls)
        assert ownership.status(INSTANCE_ID) is not None
        if proven:
            ownership.finish(INSTANCE_ID, attempt, broker_id, True)
        response = request.result(timeout=5)
    assert response.status_code == (200 if proven else 409)
    assert any(kind == "stop" for kind, _ in broker.calls) is proven


def test_sealed_r4_current_page_admission_handoff_deadline_and_stop_compose(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Real qualification, Web auth and ownership; native transport is a fixture."""
    from kodi_lab.application.development_sessions import APPROVED_OMEGA_VIEW_IMAGE
    from kodi_lab.application.novnc import CatalogViewAuthority
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.broker.operations import (
        ManagedContainerInventoryEntry,
        ObserveManagedKodiRuntimeResult,
    )
    from kodi_lab.host_bootstrap import generate_master_key
    from kodi_lab.runtime.recipes import load_reviewed_recipe, recipe_target_id
    from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
    from tests.application.test_development_runtime_selection import installed
    from tests.runtime import test_recipe_installation as recipe_fixture
    from tests.web.conftest import ORIGIN

    client = viewer_client
    csrf = login(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    root = context.settings.data_root
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    if not (root / "master-key").exists():
        generate_master_key(root)
    canonical = load_reviewed_recipe("omega")
    operation = recipe_fixture._ready(database, generation="omega")
    with database() as db:
        record = RuntimeRecipeInstallationRepository(db).get(operation)
        assert record is not None and record.runtime_revision_id is not None
        canonical_revision = UUID(record.runtime_revision_id)
    bootstrap_recipe_instance(database, canonical, canonical_revision)
    _sessions, target = installed(database, tmp_path, monkeypatch)
    _source, creation = source_and_workspace(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    reserved = client.post(
        "/api/development-session/reserve",
        json={
            "request_id": str(uuid4()),
            "expected_generation": 0,
            "target_id": str(target),
            "workspace_id": str(creation.workspace_id),
        },
        headers=headers,
    )
    assert reserved.status_code == 200, reserved.text
    selection = reserved.json()
    with database.begin() as db:
        row = db.get(DevelopmentSessionRow, selection["generation"])
        assert row is not None
        row.phase, row.container_id = "bound", CONTAINER_ID
    broker.container = ManagedContainerInventoryEntry.model_validate(
        {
            "managed_instance_id": str(target),
            "runtime_revision_id": selection["runtime_revision_id"],
            "container_id": CONTAINER_ID,
            "image_id": APPROVED_OMEGA_VIEW_IMAGE,
            "runtime_state": "running",
            "health_state": "healthy",
        }
    )
    monkeypatch.setattr(
        broker,
        "observe_managed_kodi_runtime",
        lambda **_: ObserveManagedKodiRuntimeResult.model_validate(
            {
                "managed_instance_id": str(target),
                "runtime_revision_id": selection["runtime_revision_id"],
                "container_id": CONTAINER_ID,
                "runtime_state": "running",
                "process_state": "running",
                "ipv4_address": "10.250.0.10",
                "jsonrpc_port": 8080,
            }
        ),
    )
    service = cast(FastAPI, client.app).state.current_kodi_viewer_fixture
    service.enabled_instances = frozenset({target})
    authority = CatalogViewAuthority(
        database,
        {
            target: ViewBinding(
                CONTAINER_ID, APPROVED_OMEGA_VIEW_IMAGE, "fixture-start", ()
            )
        },
    )
    service.authority = authority.resolve
    service.ownership = HumanViewerOwnership(database)
    view = client.get("/api/development-session").json()
    assert (
        view["current"]["target_id"] == str(target) and view["viewer_state"] == "ready"
    )
    assert view["summary"]["name"] == "Omega 21.3"
    page = client.get("/development-session").text
    assert f'data-novnc-viewer data-instance="{target}"' in page
    assert (
        client.get(f"/instances/{recipe_target_id(canonical)}/novnc-phase1").status_code
        == 404
    )
    prefix = f"/api/instances/{target}/novnc-"
    expected = {
        k: view["current"][k]
        for k in (
            "generation",
            "runtime_revision_id",
            "container_id",
            "workspace_id",
            "source_id",
        )
    }
    first = uuid4()
    assert (
        client.post(
            prefix + "start",
            json={"attempt": str(first), "rate": 2, "expected_selection": expected},
            headers=headers,
        ).json()["mode"]
        == "view-only"
    )
    deadline = service.attachments[first].deadline
    second = uuid4()
    assert (
        client.post(
            prefix + "take-control",
            json={"attempt": str(first), "next_attempt": str(second)},
            headers=headers,
        ).json()["mode"]
        == "interactive"
    )
    assert service.attachments[second].deadline == deadline
    third = uuid4()
    assert (
        client.post(
            prefix + "release-control",
            json={"attempt": str(second), "next_attempt": str(third)},
            headers=headers,
        ).json()["mode"]
        == "view-only"
    )
    assert service.attachments[third].deadline == deadline
    stop = broker.stop_managed_container

    def exact_stop(**kwargs: Any) -> Any:
        before = broker.container
        assert before is not None and before.managed_instance_id == str(target)
        result = stop(**kwargs)
        broker.container = before.model_copy(
            update={"runtime_state": ContainerRuntimeState.STOPPED}
        )
        return result

    monkeypatch.setattr(broker, "stop_managed_container", exact_stop)
    response = client.post(
        "/api/development-session/stop",
        json={"expected_generation": selection["generation"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert service.ownership.status(target) is None
    assert broker.container.runtime_state == ContainerRuntimeState.STOPPED


@pytest.mark.parametrize("phase", [None, "reserved", "bound", "other"])
def test_expanded_page_without_rendered_bound_selection_cannot_follow_future_profile(
    viewer_client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    phase: str | None,
) -> None:
    client = viewer_client
    login(client)
    if phase is not None:
        selected(
            client, database, monkeypatch, phase="bound" if phase == "other" else phase
        )
        if phase == "other":
            from tests.web.conftest import UNRELATED_INSTANCE_ID

            with database.begin() as db:
                row = db.get(DevelopmentSessionRow, 1)
                assert row is not None
                row.target_id = str(UNRELATED_INSTANCE_ID)
    page = client.get(f"/instances/{INSTANCE_ID}/novnc-phase1").text
    if phase == "bound":
        assert 'data-view-generation="1"' in page
        assert 'data-view-container="' + CONTAINER_ID in page
    else:
        assert "data-novnc-viewer" not in page
        assert "novnc-phase1.js" not in page
        assert "Reload this page" in page
        # Selecting/preparing later cannot add admission code to this old HTML.
        if phase is None:
            selected(client, database, monkeypatch)
        else:
            with database.begin() as db:
                row = db.get(DevelopmentSessionRow, 1)
                assert row is not None
                row.target_id = str(INSTANCE_ID)
                row.phase, row.container_id = "bound", CONTAINER_ID
        assert "data-novnc-viewer" not in page
        assert (
            'data-view-generation="1"'
            in client.get(f"/instances/{INSTANCE_ID}/novnc-phase1").text
        )


@pytest.mark.parametrize(
    "path", ["/", "/kodi", "/runtimes", f"/instances/{INSTANCE_ID}"]
)
def test_shared_templates_tolerate_older_process_without_current_context(
    client: TestClient,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    login(client)
    selected(client, database, monkeypatch)
    response = client.get(path)
    assert response.status_code == 200
    context = dict(cast(Any, response).context)
    context.pop("current_kodi")
    if path == "/":
        # The new app also provides this value as a template global. Override
        # it explicitly to simulate an older process with no Add Kodi route.
        context["add_kodi_enabled"] = False
    rendered = cast(Any, response).template.render(**context)
    assert "/development-session" in rendered
    assert "No development Kodi is selected." not in rendered
    assert "This is Current Kodi." not in rendered
    assert "Not the currently selected Kodi." not in rendered
    if path == "/":
        assert 'href="/pair-kodi">Pair Kodi</a>' in rendered
        assert 'href="/add-kodi"' not in rendered
    if destination := os.getenv("KODI_LAB_CURRENT_UI_EVIDENCE"):
        output = Path(destination) / "legacy-context-pages.json"
        pages = json.loads(output.read_text()) if output.exists() else {}
        pages[path] = rendered
        output.write_text(json.dumps(pages))

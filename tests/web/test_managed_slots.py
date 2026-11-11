"""Administrator workflow composes real lifecycle with isolated runtimes."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER
from kodi_lab.storage.models import ManagedWebIntentRow, McpClientRow
from tests.managed_versions_support import Broker
from tests.runtime.test_recipe_installation import _ready
from tests.web.conftest import ORIGIN, login
from tests.web.test_managed_versions_mcp import broker as broker


def setup(
    client: TestClient, database: sessionmaker[Session], *, prepared: bool = True
) -> str:
    _ready(database, generation="omega")
    csrf = login(client)
    cast(
        FastAPI, client.app
    ).state.kodi_lab.managed_versions._rate = BoundedIntervalRateLimiter(0)
    if prepared:
        prepare_existing(client, database, "omega")
    return csrf


def prepare_existing(
    client: TestClient, database: sessionmaker[Session], version: str
) -> None:
    """Prepare a stopped native fixture representing a retained installation."""
    context = cast(FastAPI, client.app).state.kodi_lab
    versions = context.managed_versions
    # Prepare the retained fixture through the core coordinator. The Web empty
    # slot route is tested separately and cannot create a native-only profile.
    for action in ("start", "stop"):
        result = versions.invoke(
            client_id=WEB_ADMIN_OWNER,
            request_id=uuid4(),
            version=version,
            action=action,
            authorize=lambda _session, _target: None,
        )
        assert result.receipt_state == "complete", result.code
        wait(client)
    versions._lifecycle._broker.starts = versions._lifecycle._broker.stops = 0


def slot(client: TestClient, version: str = "omega") -> dict[str, Any]:
    response = client.get("/api/managed-kodi/slots")
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["slots"] if s["version"] == version)


def act(
    client: TestClient, csrf: str, action: str, *, payload: dict[str, Any] | None = None
) -> Response:
    body = payload or dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=action == "reset",
    )
    return client.post(
        f"/api/managed-kodi/omega/{action}",
        json=body,
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )


def wait(client: TestClient) -> None:
    tasks = cast(FastAPI, client.app).state.kodi_lab.readiness
    with tasks._lock:
        futures = [entry.future for entry in tasks._entries.values() if entry.future]
    for future in futures:
        future.result(timeout=5)


def test_four_version_cards_and_cached_empty_start_without_mcp_identity(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database, prepared=False)
    page = client.get("/managed-kodi")
    assert page.status_code == 200
    for version in ("matrix", "nexus", "omega", "piers"):
        assert f'data-slot="{version}"' in page.text
    assert "Only one managed Kodi can run" in page.text
    assert "Piers (preview)" in page.text and "Set up for add-on testing" in page.text
    before = slot(client)
    assert before["state"] == "empty" and not before["start"]
    assert "Create Managed Kodi" in before["start_reason"]
    refused = act(client, csrf, "start")
    assert refused.status_code == 409
    assert broker.provisions == broker.starts == 0
    prepare_existing(client, database, "omega")
    result = act(client, csrf, "start")
    assert result.status_code == 200, result.text
    assert result.json()["phase"] == "complete", result.text
    wait(client)
    current = slot(client)
    assert current["state"] == "running" and current["native"] == "Ready"
    assert broker.provisions == broker.starts == 1
    with database() as session:
        assert session.get(McpClientRow, str(WEB_ADMIN_OWNER)) is None
        assert session.scalar(select(ManagedWebIntentRow)) is not None


def test_stop_retains_and_confirmed_reset_replaces_only_the_profile(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    original = broker.records[0]
    profile = broker.root / original.managed_instance_id / "settings.xml"
    profile.write_text("user settings and addons")
    assert act(client, csrf, "stop").status_code == 200
    assert profile.read_text() == "user settings and addons"
    assert slot(client)["state"] == "stopped"
    payload = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=True,
    )
    first = act(client, csrf, "reset", payload=payload)
    assert first.status_code == 200 and first.json()["phase"] == "complete", first.text
    replay = act(client, csrf, "reset", payload=payload)
    assert (
        replay.status_code == 200
        and replay.json()["operation_id"] == first.json()["operation_id"]
    )
    assert broker.removals == 1 and broker.provisions == 2
    assert broker.records[0].container_id != original.container_id
    assert broker.records[0].runtime_state == "stopped"
    assert profile.read_text() != "user settings and addons"
    current = slot(client)
    assert current["reset_note"]
    guide = client.get(current["development_url"])
    assert (
        "previous Bridge trust" in guide.text and "fresh workspace files" in guide.text
    )
    assert "/development-session/preparation" in guide.text


def test_stale_reset_confirmation_and_missing_consent_do_not_remove(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    initial = slot(client)
    no = act(
        client,
        csrf,
        "reset",
        payload=dict(
            request_id=str(uuid4()),
            expected_snapshot=initial["snapshot"],
            confirm_reset=False,
        ),
    )
    assert no.status_code == 409
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    stale = act(
        client,
        csrf,
        "reset",
        payload=dict(
            request_id=str(uuid4()),
            expected_snapshot=initial["snapshot"],
            confirm_reset=True,
        ),
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_browser"
    assert broker.removals == 0


@pytest.mark.parametrize("defect", ["anonymous", "csrf", "origin"])
@pytest.mark.parametrize("action", ["start", "stop", "reset"])
def test_mutations_preserve_web_authorization_csrf_and_origin(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    defect: str,
    action: str,
) -> None:
    csrf = setup(client, database)
    payload = dict(request_id=str(uuid4()), expected_snapshot=slot(client)["snapshot"])
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    if defect == "anonymous":
        client.cookies.clear()
    if defect == "csrf":
        headers["X-Kodi-Lab-CSRF"] = "invalid"
    if defect == "origin":
        headers["Origin"] = "https://untrusted.invalid"
    response = client.post(
        f"/api/managed-kodi/omega/{action}", json=payload, headers=headers
    )
    assert response.status_code in {401, 403}
    assert broker.starts == broker.removals == 0 and broker.provisions == 1


@pytest.mark.parametrize("blocker", ["client", "viewer", "input"])
def test_active_client_viewer_or_input_disables_and_blocks_stop_and_reset(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    blocker: str,
    request: pytest.FixtureRequest,
) -> None:
    from datetime import timedelta
    from uuid import UUID

    from kodi_lab.application.browser_remote_coordination import (
        acquire_browser_remote,
        release_browser_remote,
    )
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedInstanceRow, ManagedInstanceUseRow

    csrf = setup(client, database)
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    record = broker.records[0]
    target = UUID(record.managed_instance_id)
    context = cast(FastAPI, client.app).state.kodi_lab
    if blocker == "client":
        with database.begin() as session:
            session.add(
                ManagedInstanceUseRow(
                    managed_instance_id=str(target),
                    actor_key="mcp:" + str(uuid4()),
                    last_activity_at=timestamp(context.managed_versions._clock()),
                    hold_until=timestamp(
                        context.managed_versions._clock() + timedelta(minutes=10)
                    ),
                )
            )
    elif blocker == "input":
        handle = acquire_browser_remote(database, target)
        request.addfinalizer(lambda: release_browser_remote(handle))
    else:
        with database() as session:
            instance = session.get(ManagedInstanceRow, str(target))
            assert instance is not None
            epoch = instance.runtime_state_revision
        HumanViewerOwnership(database).create(
            target,
            uuid4(),
            "f" * 64,
            ViewBinding(
                record.container_id,
                record.image_id,
                "fixture",
                (str(record.runtime_revision_id), epoch),
            ),
            "view-only",
        )
    state = slot(client)
    assert not state["stop"] and not state["reset"]
    assert state["stop_reason"] and state["reset_reason"]
    for action in ("stop", "reset"):
        reply = act(client, csrf, action)
        assert reply.status_code == 409 or reply.json()["phase"] == "blocked", (
            reply.text
        )
    # Automatic stopping uses the same viewer/input/explicit-hold safeguards.
    from kodi_lab.application.managed_resources import ResourcePolicyInput

    resources = context.managed_resources
    resources.configure(ResourcePolicyInput(max_running=1, idle_minutes=1))
    resources.enroll(target, enabled=True)
    later = resources._clock() + timedelta(minutes=2)
    resources._clock = lambda: later
    assert resources.sweep_idle() == 0
    assert broker.removals == broker.stops == 0


def test_refresh_and_replay_after_uncertain_provision_never_repeat_create(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database, prepared=False)
    original = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=False,
    )
    broker.fault = "lost_provision_response"
    reply = act(client, csrf, "start", payload=original)
    assert reply.status_code == 409, reply.text
    assert reply.json()["code"] == "managed_create_preparation_required"
    counts = broker.provisions, broker.starts
    for _ in range(3):
        assert client.get("/managed-kodi").status_code == 200
        assert client.get("/api/managed-kodi/slots").status_code == 200
        receipt = client.get("/api/managed-kodi/requests/" + original["request_id"])
        assert receipt.status_code == 404
    assert (broker.provisions, broker.starts) == counts
    broker.fault = ""
    replay = act(client, csrf, "start", payload=original)
    assert replay.status_code == 409
    assert broker.provisions == 0 and broker.starts == 0
    # Original request cannot be rebound to another action or profile confirmation.
    conflict = act(client, csrf, "reset", payload={**original, "confirm_reset": True})
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "managed_create_preparation_required"
    )


def test_uncertain_reset_remains_visible_and_reading_never_replays_destruction(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    assert act(client, csrf, "stop").status_code == 200
    original = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=True,
    )
    broker.fault = "lost_remove_response"
    result = act(client, csrf, "reset", payload=original)
    assert result.status_code == 200 and result.json()["phase"] == "uncertain", (
        result.text
    )
    assert broker.removals == 1 and broker.provisions == 1
    current = slot(client)
    assert (
        current["state"] == "blocked"
        and current["pending_request"] == original["request_id"]
    )
    assert current["recovery_url"].endswith("/history")
    for _ in range(2):
        receipt = client.get("/api/managed-kodi/requests/" + original["request_id"])
        assert receipt.json()["can_resume"] is False
        assert client.get("/managed-kodi").status_code == 200
    replay = act(client, csrf, "reset", payload=original)
    assert replay.status_code == 200 and replay.json()["phase"] == "uncertain"
    assert broker.removals == 1 and broker.provisions == 1


def test_concurrent_confirmation_fences_other_start_and_reset_and_reuses_request(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    csrf = setup(client, database)
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    assert act(client, csrf, "stop").status_code == 200
    original = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=True,
    )
    entered, proceed = Event(), Event()
    remove = broker.deprovision_managed_container

    def paused(**kwargs: Any) -> Any:
        entered.set()
        assert proceed.wait(5)
        return remove(**kwargs)

    monkeypatch.setattr(broker, "deprovision_managed_container", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(act, client, csrf, "reset", payload=original)
        try:
            assert entered.wait(5)
            for action in ("start", "reset"):
                response = act(client, csrf, action)
                assert response.status_code == 409, response.text
            current = slot(client)
            assert (
                current["state"] == "blocked"
                and current["pending_request"] == original["request_id"]
            )
            assert client.get("/managed-kodi").status_code == 200
            assert broker.removals == 0
        finally:
            proceed.set()
        response = future.result(timeout=10)
    assert response.status_code == 200 and response.json()["phase"] == "complete", (
        response.text
    )
    assert broker.removals == 1 and broker.provisions == 2


def test_live_admin_session_is_rechecked_before_acceptance(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import delete

    from kodi_lab.storage.models import WebSessionRow

    csrf = setup(client, database)
    adapter = cast(FastAPI, client.app).state.managed_slots
    invoke = adapter.v.invoke

    def revoke(**kwargs: Any) -> Any:
        with database.begin() as session:
            session.execute(delete(WebSessionRow))
        return invoke(**kwargs)

    monkeypatch.setattr(adapter.v, "invoke", revoke)
    response = act(client, csrf, "start")
    assert response.status_code == 401
    assert broker.provisions == 1 and broker.starts == 0


def test_start_never_switches_another_running_version_and_bridge_is_separate(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    _ready(database, generation="nexus")
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    other = slot(client, "nexus")
    assert not other["start"] and "Create Managed Kodi" in other["start_reason"]
    assert slot(client)["technical"]["agent_readiness"] == "not_assessed"
    response = client.post(
        "/api/managed-kodi/nexus/start",
        json=dict(request_id=str(uuid4()), expected_snapshot=other["snapshot"]),
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert (
        response.status_code == 409
        and response.json()["code"] == "managed_create_preparation_required"
    ), response.text
    assert broker.starts == broker.provisions == 1 and broker.stops == 0


def test_missing_cached_files_and_duplicate_profile_have_clear_recovery(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    from dataclasses import replace

    from tests.web.conftest import INSTANCE_ID

    csrf = setup(client, database)
    missing = slot(client, "matrix")
    assert missing["preparation"] == "Installation needed" and not missing["start"]
    assert missing["install_url"] == "/create-kodi?generation=matrix"
    assert "Create Managed Kodi" in missing["start_reason"]
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    broker.records.append(
        replace(
            broker.records[0],
            managed_instance_id=str(INSTANCE_ID),
            container_id="f" * 64,
        )
    )
    blocked = slot(client)
    assert blocked["state"] == "blocked" and blocked["recovery_url"]
    assert not blocked["start"] and not blocked["reset"]
    assert broker.removals == 0


def test_reset_note_survives_start_but_old_receipt_remains_historical(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    csrf = setup(client, database)
    original = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=True,
    )
    reply = act(client, csrf, "reset", payload=original)
    assert reply.status_code == 200 and reply.json()["phase"] == "complete", reply.text
    assert broker.starts == 0 and broker.removals == 1
    assert slot(client)["state"] == "stopped"
    from kodi_lab.storage.models import ManagedBridgePreparationRow

    with database() as session:
        assert list(session.scalars(select(ManagedBridgePreparationRow))) == []
    page = client.get("/managed-kodi").text
    assert "Narrow Bridge preparation is incomplete" in page
    assert '#bridge-preparation">Prepare/Repair Bridge for this Kodi</a>' in page
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert slot(client)["reset_note"] and slot(client)["state"] == "running"
    receipt = client.get("/api/managed-kodi/requests/" + original["request_id"])
    assert "prepared a stopped profile" in receipt.json()["message"]
    assert "Check current status" in receipt.json()["message"]


@pytest.fixture
def slots_screen_client(
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> TestClient:
    from pathlib import Path
    from uuid import UUID

    from kodi_lab.application.live_display import LiveDisplaySessionRegistry
    from kodi_lab.application.managed_versions import ManagedVersionLifecycleService
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.models import ManagedInstanceRow
    from kodi_lab.web.app import create_app
    from kodi_lab.web.novnc import PrototypeViewer
    from tests.web import conftest

    target = ManagedVersionLifecycleService.slot("omega").canonical_target_id

    def binding(identity: UUID) -> ViewBinding:
        record = next(
            r for r in broker.records if r.managed_instance_id == str(identity)
        )
        with database() as session:
            instance = session.get(ManagedInstanceRow, str(identity))
            assert instance is not None
            epoch = instance.runtime_state_revision
        return ViewBinding(
            record.container_id,
            record.image_id,
            "fixture",
            (str(record.runtime_revision_id), epoch),
        )

    screen = PrototypeViewer(
        Path("/tmp/unused-fixture-view-socket"),
        binding,
        registry=LiveDisplaySessionRegistry(),
        ownership=HumanViewerOwnership(database),
        enabled_instances=frozenset({target}),
    )
    create = create_app

    def configured(**kwargs: Any) -> FastAPI:
        kwargs["novnc_viewer"] = screen
        app = create(**kwargs)
        app.state.slots_screen_fixture = screen
        return app

    monkeypatch.setattr(conftest, "create_app", configured)
    return cast(TestClient, request.getfixturevalue("client"))


def test_screen_entry_requires_no_workspace_and_old_attachment_cannot_follow_reset(
    slots_screen_client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    from uuid import UUID

    from kodi_lab.storage.models import DevelopmentSessionRow
    from kodi_lab.web.novnc import StartView
    from tests.web.view_attempt_fixture import view_attempt

    client = slots_screen_client
    csrf = setup(client, database)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    current = slot(client)
    assert current["screen_url"].endswith("/novnc-phase1")
    page = client.get(current["screen_url"])
    assert page.status_code == 200 and "Start Viewing" in page.text
    with database() as session:
        assert session.scalar(select(DevelopmentSessionRow)) is None
    screen = cast(FastAPI, client.app).state.slots_screen_fixture
    target = UUID(broker.records[0].managed_instance_id)
    old = screen.start(target, "f" * 64, StartView(attempt=view_attempt()))
    assert old.mode == "view-only"
    screen.end(old)
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    assert act(client, csrf, "reset").json()["phase"] == "complete"
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    with pytest.raises(ValueError):
        screen.consume(target, "f" * 64, old.ticket)
    assert screen.authority(target).container != old.binding.container


def test_failed_native_readiness_never_offers_another_version_as_a_switch(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from kodi_lab.kodi.readiness import UsabilityState
    from tests.application.test_managed_start import NativeReady

    csrf = setup(client, database)
    _ready(database, generation="nexus")
    adapter = cast(FastAPI, client.app).state.managed_slots
    failed = replace(
        NativeReady().wait_until_usable(), usability_state=UsabilityState.NOT_USABLE
    )
    monkeypatch.setattr(
        adapter.v._lifecycle,
        "_coordinator_factory",
        lambda *_: SimpleNamespace(wait_until_usable=lambda *_a, **_k: failed),
    )
    assert act(client, csrf, "start").status_code == 200
    wait(client)
    current, other = slot(client), slot(client, "nexus")
    assert current["state"] == "blocked" and current["native"] != "Ready"
    assert not current["screen_url"] and current["recovery_url"].endswith("/history")
    assert not other["start"] and "Create Managed Kodi" in other["start_reason"]
    assert client.get("/api/managed-kodi/slots").json()["running"] == ["Omega"]
    assert broker.starts == 1 and broker.stops == 0


@pytest.mark.parametrize("action", ["stop", "reset"])
@pytest.mark.parametrize(
    ("playback", "code"), [(True, "playback_active"), (None, "playback_unknown")]
)
def test_playback_refusal_explains_the_next_action_without_stopping(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    action: str,
    playback: bool | None,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = setup(client, database)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    broker.playback_active = playback
    # Status must not turn this presentation change into native playback polling.
    resources = cast(FastAPI, client.app).state.managed_slots.v._resources
    with monkeypatch.context() as patch:
        patch.setattr(
            resources,
            "_playback_factory",
            lambda *_: pytest.fail("Ordinary status queried playback"),
        )
        current = slot(client)
        assert current["stop"] and current["reset"]
        assert client.get("/managed-kodi").status_code == 200
    response = act(client, csrf, action)
    assert response.status_code == 200 and response.json()["phase"] == "blocked", (
        response.text
    )
    assert response.json()["code"] == code
    if playback:
        assert "Stop playback" in response.json()["message"]
    else:
        assert "could not be checked" in response.json()["message"]
        assert "cannot safely stop or reset" in response.json()["message"]
        assert "refresh status" in response.json()["message"]
    original = response.json()["request"]["request_id"]
    assert slot(client)["state"] == "running"
    receipt = client.get("/api/managed-kodi/requests/" + original).json()
    assert receipt["code"] == code and receipt["phase"] == "blocked"
    assert receipt["message"] == response.json()["message"]
    assert broker.stops == broker.removals == 0


@pytest.mark.parametrize("action", ["stop", "reset"])
@pytest.mark.parametrize("unavailable", [False, True])
def test_late_playback_refusal_preserves_original_recovery(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    unavailable: bool,
) -> None:
    from types import SimpleNamespace

    csrf = setup(client, database)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    probes = 0

    def playback() -> bool:
        nonlocal probes
        probes += 1
        if probes == 1:
            return False
        if unavailable:
            raise RuntimeError("Playback became unavailable before dispatch")
        return True

    resources = cast(FastAPI, client.app).state.managed_slots.v._resources
    monkeypatch.setattr(
        resources,
        "_playback_factory",
        lambda *_: SimpleNamespace(playback_active=playback),
    )
    result = act(client, csrf, action).json()
    assert probes == 2 and broker.stops == broker.removals == 0
    assert result["phase"] in {"blocked", "uncertain"}
    assert result["code"] in {
        "lifecycle_failed",
        "operation_requires_reconciliation",
        "reconciled_new_request_required",
    }
    assert "original operation" in result["message"]
    assert result["recovery_url"].endswith("/history")
    original = result["request"]["request_id"]
    receipt = client.get("/api/managed-kodi/requests/" + original).json()
    assert receipt["request"] == result["request"]
    assert receipt["code"] == result["code"]
    assert probes == 2 and broker.stops == broker.removals == 0


@pytest.mark.parametrize("action", ["stop", "reset"])
def test_runtime_not_ready_refusal_has_actionable_guidance(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    from kodi_lab.application.managed_resources import ManagedResourceProtected

    csrf = setup(client, database)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ManagedResourceProtected("runtime_not_ready")

    resources = cast(FastAPI, client.app).state.managed_slots.v._resources
    monkeypatch.setattr(resources, "_playback_evidence", refuse)
    result = act(client, csrf, action).json()
    assert result["phase"] == "blocked" and result["code"] == "runtime_not_ready"
    message = result["message"]
    assert "not ready for a safe Stop or Reset" in message
    assert "refresh status" in message
    assert "playback" not in message.lower()
    assert broker.stops == broker.removals == 0

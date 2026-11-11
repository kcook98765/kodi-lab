"""Web administration, saved receipt recovery and accessible progress."""

import subprocess
from pathlib import Path
from uuid import UUID, uuid4

from tests.web.conftest import ORIGIN
from tests.web.test_create_kodi_api import approval
from tests.web.test_create_kodi_continuation import Web, reach, step
from tests.web.test_create_kodi_continuation import (
    factory as factory,
)
from tests.web.test_create_kodi_continuation import (
    schema as schema,
)
from tests.web.test_create_kodi_continuation import (
    web as web,
)


def test_fresh_web_create_defaults_to_bridge_prepared_and_stopped(web: Web) -> None:
    env, client, headers = web
    body = approval()
    body.pop("start_after_setup")
    response = client.post("/api/create-kodi/operations", json=body, headers=headers)
    assert response.status_code == 202
    status = response.json()
    env.broker.parent = UUID(status["operation_id"])
    for _ in range(24):
        if status["complete"]:
            break
        status = step(web, status)
    assert status["complete"] and status["phase"] == "Ready"
    assert status["instance"]["state"] == "stopped"
    assert env.broker.provisions == 1 and env.broker.starts == env.broker.stops == 2
    assert status["preparation"] == {
        "installation": "installed",
        "native_readiness": "verified_during_setup",
        "narrow_bridge": "verified_during_setup",
        "bridge_trust": "stored_active",
        "recovery": "none",
    }
    assert client.get(status["status_url"]).json() == status
    assert (
        client.post("/api/create-kodi/operations", json=body, headers=headers).json()[
            "operation_id"
        ]
        == status["operation_id"]
    )
    assert env.broker.starts == 2


def test_maintenance_is_admin_csrf_protected_and_cannot_rebind_saved_target(
    web: Web,
) -> None:
    env, client, headers = web
    target = uuid4()
    base = f"/api/instances/{target}/bridge-preparation"
    payload = {"request_id": str(uuid4())}
    cookies = dict(client.cookies)
    client.cookies.clear()
    assert (
        client.post(
            base, json=payload, headers={"Authorization": "Bearer fixture"}
        ).status_code
        == 401
    )
    assert client.get(base).status_code == 401
    client.cookies.update(cookies)
    assert client.post(base, json=payload, headers=ORIGIN).status_code == 403
    assert (
        client.post(base, json={**payload, "force": True}, headers=headers).status_code
        == 422
    )
    assert client.post(base, json=payload, headers=headers).status_code == 409
    assert env.broker.provisions == env.broker.starts == 0


def test_native_success_requires_bridge_and_original_receipt_replays(
    web: Web,
) -> None:
    from tests.web.test_create_kodi_continuation import accept

    env, client, headers = web
    accept(web)
    status = reach(web, "readiness", "planned")
    native = step(web, status)
    assert native["phase"] == "Preparing Bridge" and not native["complete"]
    accepted = step(web, native)
    base = f"/api/instances/{native['instance']['target_id']}/bridge-preparation"
    row = client.get(base).json()
    assert row["checkpoint"] == "pending"
    before = env.broker.starts, env.broker.stops
    assert env.broker.parent is not None
    original = env.repo.get(env.broker.parent)
    assert original is not None
    replay = client.post(
        "/api/create-kodi/operations",
        json=approval(request_id=original.request_id),
        headers=headers,
    )
    assert replay.status_code == 200 and replay.json()["operation_id"] == str(
        original.id
    )
    assert client.get(base).json()["operation_id"] == row["operation_id"]
    assert (env.broker.starts, env.broker.stops) == before
    assert client.get(accepted["status_url"]).json() == accepted
    wrong = (
        f"/api/instances/{uuid4()}/bridge-preparation/{row['operation_id']}/continue"
    )
    assert (
        client.post(
            wrong, json={"version": row["version"]}, headers=headers
        ).status_code
        == 404
    )
    assert (env.broker.starts, env.broker.stops) == before


def test_bridge_browser_keeps_request_and_passive_gets() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("bridge_preparation_browser.cjs")),
            str(root / "src/kodi_lab/web/static/bridge-preparation.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_setup_progress_and_errors_keep_accessible_responsive_markup(web: Web) -> None:
    _env, client, _headers = web
    page = client.get("/create-kodi").text
    assert 'data-create-kodi aria-busy="false"' in page
    assert 'data-create-phase tabindex="-1"' in page
    assert 'role="status" aria-live="polite" aria-atomic="true"' in page
    assert 'data-create-error role="alert" hidden' in page
    for name in (
        "installation",
        "native_readiness",
        "narrow_bridge",
        "bridge_trust",
        "recovery",
    ):
        assert f"data-preparation-{name}" in page
    css = client.get("/static/create-kodi.css").text
    assert "@media (max-width: 40rem)" in css
    assert "flex-direction: column" in css and "min-height: 2.75rem" in css
    assert "overflow-wrap: anywhere" in css

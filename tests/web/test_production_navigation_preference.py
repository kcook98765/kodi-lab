import json
import os
import subprocess
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.production_navigation import (
    ProductionNavigationPreferenceService,
)
from tests.application.test_production_navigation import _authority_environment

from .conftest import ORIGIN, login


def _register(
    client: TestClient, csrf: str, name: str, *, kind: str = "production"
) -> str:
    response = client.post(
        f"/api/{kind}-targets",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={
            "logical_name": name,
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.40" if name == "First" else "192.168.50.41",
            "port": 8080,
            "authentication_mode": "none",
            "configured_capabilities": ["target_inspect"],
        },
    )
    assert response.status_code == 201
    return cast(str, response.json()["target_id"])


def test_navigation_preference_is_authenticated_csrf_checked_and_production_only(
    client: TestClient,
) -> None:
    missing = f"/api/production-targets/{uuid4()}/navigation-preference"
    assert client.get(missing).status_code == 401
    assert (
        client.put(missing, json={"enabled": False, "expected_revision": 1}).status_code
        == 401
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    first, second = _register(client, csrf, "First"), _register(client, csrf, "Second")
    path = f"/api/production-targets/{first}/navigation-preference"
    saved = client.get(path).json()
    assert not saved["enabled"] and saved["revision"] == 1
    page = client.get(f"/kodi/{first}")
    assert page.status_code == 200
    assert f'data-navigation-preference="{first}"' in page.text
    assert f'for="navigation-{first}"' in page.text
    assert "/static/navigation-preference.js" in page.text
    payload = {"enabled": False, "expected_revision": 1}
    assert client.put(path, json=payload).status_code == 403
    assert (
        client.put(
            path, json=payload, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert client.put(path, json=payload, headers=headers).status_code == 200
    # An unverified registration cannot silently opt into a different connection.
    assert (
        client.put(path, json={**payload, "enabled": True}, headers=headers).status_code
        == 409
    )
    assert (
        client.put(
            path, json={**payload, "enabled": "true"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.put(
            path, json={**payload, "expected_revision": 2}, headers=headers
        ).status_code
        == 409
    )
    assert not client.get(
        f"/api/production-targets/{second}/navigation-preference"
    ).json()["enabled"]
    development = _register(client, csrf, "Development", kind="external")
    assert (
        client.get(
            f"/api/production-targets/{development}/navigation-preference"
        ).status_code
        == 404
    )
    assert client.get(missing).status_code == 404
    assert (
        'data-action="grant-production-navigate-scope"'
        not in client.get("/mcp-clients").text
    )
    assert (
        "Emergency Block: stop navigation and higher-risk mutations"
        in client.get("/production-targets").text
    )


def test_verified_navigation_api_saves_reload_and_rejects_stale_revision(
    client: TestClient, tmp_path: Path
) -> None:
    env = _authority_environment(
        tmp_path, acknowledge_http=False, navigation_enabled=False
    )
    app = cast(FastAPI, client.app)
    object.__setattr__(
        app.state.kodi_lab,
        "navigation_preferences",
        ProductionNavigationPreferenceService(env.factory),
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    url = f"/api/production-targets/{env.target_id}/navigation-preference"
    before = client.get(url).json()
    on = client.put(
        url,
        headers=headers,
        json={"enabled": True, "expected_revision": before["revision"]},
    )
    assert on.status_code == 200 and on.json()["enabled"] is True
    assert client.get(url).json() == on.json()
    assert (
        client.put(
            url,
            headers=headers,
            json={"enabled": False, "expected_revision": before["revision"]},
        ).status_code
        == 409
    )
    off = client.put(
        url,
        headers=headers,
        json={"enabled": False, "expected_revision": on.json()["revision"]},
    )
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert off.json()["revision"] > on.json()["revision"]


@pytest.mark.parametrize("block", ["global", "target"])
def test_navigation_api_explains_active_blocks_and_rejects_on(
    client: TestClient, tmp_path: Path, block: str
) -> None:
    from tests.application.test_production_navigation_opt_in import (
        _block_code,
        _set_block,
    )

    env = _authority_environment(
        tmp_path, acknowledge_http=False, navigation_enabled=False
    )
    app = cast(FastAPI, client.app)
    object.__setattr__(
        app.state.kodi_lab,
        "navigation_preferences",
        ProductionNavigationPreferenceService(env.factory),
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    url = f"/api/production-targets/{env.target_id}/navigation-preference"
    _set_block(env, block)
    before = client.get(url).json()
    assert before["blocked_reason"] == _block_code(block)
    response = client.put(
        url,
        headers=headers,
        json={"enabled": True, "expected_revision": before["revision"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == _block_code(block)
    assert client.get(url).json() == before
    off = client.put(
        url,
        headers=headers,
        json={"enabled": False, "expected_revision": before["revision"]},
    )
    assert off.status_code == 200 and off.json()["enabled"] is False


@pytest.mark.skipif(
    not os.getenv("KODI_LAB_BROWSER_TOOLS"), reason="Bounded Chromium opt-in"
)
def test_navigation_toggle_in_chromium(client: TestClient, tmp_path: Path) -> None:
    csrf = login(client)
    first, second = _register(client, csrf, "First"), _register(client, csrf, "Second")
    pages = {target: client.get(f"/kodi/{target}").text for target in (first, second)}
    fixture = tmp_path / "navigation-pages.json"
    fixture.write_text(json.dumps({"pages": pages, "first": first, "second": second}))
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("navigation_preference_browser.cjs")),
            str(fixture),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "CHROMIUM PASS" in result.stdout


def test_inspection_mcp_discovery_exposes_action_aware_remote_without_control(
    client: TestClient,
) -> None:
    from kodi_lab.application.production_inspection import (
        PRODUCTION_INSPECTION_PRESET_ID,
    )
    from tests.web.test_mcp import _post

    csrf = login(client)
    target_id = _register(client, csrf, "First")
    app = cast(FastAPI, client.app)
    admin = app.state.kodi_lab.mcp_administration
    agent = admin.create_client(
        display_name="Navigation inspector", preset_id=PRODUCTION_INSPECTION_PRESET_ID
    )
    credential = admin.issue_credential(agent.id)
    admin.assign_target_preset(UUID(target_id), PRODUCTION_INSPECTION_PRESET_ID)
    admin.grant_target(agent.id, UUID(target_id))
    names = {
        tool["name"]
        for tool in _post(client, credential.token, "tools/list").json()["result"][
            "tools"
        ]
    }
    assert "remote_action" in names
    assert agent.preset.permissions == ("target.inspect",)
    result = _post(
        client,
        credential.token,
        "tools/call",
        params={
            "name": "remote_action",
            "arguments": {
                "target_id": target_id,
                "request_id": str(uuid4()),
                "action": "home",
            },
        },
    ).json()["result"]
    assert result["isError"] is True  # Unverified, OFF registration never dispatches.
